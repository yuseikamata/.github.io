#!/usr/bin/env python3
"""毎朝のマーケット・ブリーフィング用データを集めて market/data.json に書き出す。

標準ライブラリのみで動く（GitHub Actions でそのまま実行できる）。

データソース（すべて無料・APIキー不要。e-Stat のみ任意で appId を使う）
  - Yahoo Finance chart API : 株価指数・先物・為替・商品の日足（最新値）
  - FRED (セントルイス連銀)    : 米国の物価・雇用・金利、株価指数のフォールバック
  - 財務省 国債金利情報        : 日本国債イールドカーブ（1〜40年）
  - 日本銀行 時系列統計 API     : 無担保コール翌日物、企業物価指数、ドル円(東京17時)
  - e-Stat API (任意)          : 日本の消費者物価指数（環境変数 ESTAT_APP_ID があれば）

個々のソースが落ちても全体は止めず、取れたものだけでページを作る。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data.json"
JST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(JST).date()
UA = "market-briefing/1.0 (+https://github.com/yuseikamata/.github.io)"
ERRORS: list[str] = []


# ---------------------------------------------------------------- HTTP helpers
def http_get(url: str, retries: int = 3, timeout: int = 40) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"GET failed: {url} ({last})")


def safe(label: str, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        msg = f"{label}: {e}"
        ERRORS.append(msg)
        print("WARN", msg, file=sys.stderr)
        return None


# ---------------------------------------------------------------- sources
def yahoo(symbol: str, rng: str = "2y") -> list[tuple[str, float]]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval=1d")
    time.sleep(1.5)  # 連続アクセスを避けて間隔をあける
    d = json.loads(http_get(url, retries=3))
    res = d["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0]["close"]
    out: dict[str, float] = {}
    for ts, c in zip(res.get("timestamp", []), closes):
        if c is None:
            continue
        day = dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date().isoformat()
        out[day] = float(c)
    if not out:
        raise RuntimeError("empty")
    return sorted(out.items())


def fred(series_id: str, years: int = 3) -> list[tuple[str, float]]:
    start = (TODAY - dt.timedelta(days=365 * years)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    text = http_get(url).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        try:
            out.append((r[0], float(r[1])))
        except ValueError:
            continue
    if not out:
        raise RuntimeError("empty")
    return out


def boj(db: str, code: str, start: str) -> list[tuple[str, float]]:
    url = ("https://www.stat-search.boj.or.jp/api/v1/getDataCode?"
           + urllib.parse.urlencode({"db": db, "code": code, "format": "json", "lang": "jp", "startDate": start}))
    d = json.loads(http_get(url).decode("utf-8"))
    v = d["RESULTSET"][0]["VALUES"]
    out = []
    for date, val in zip(v["SURVEY_DATES"], v["VALUES"]):
        if val is None:
            continue
        s = str(date)
        iso = f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else f"{s[:4]}-{s[4:6]}-01"
        out.append((iso, float(val)))
    if not out:
        raise RuntimeError("empty")
    return out


ERA = {"R": 2018, "H": 1988, "S": 1925}


def _jgb_date(s: str) -> str | None:
    m = re.match(r"([RHS])(\d+)\.(\d+)\.(\d+)", s.strip())
    if not m:
        return None
    y = ERA[m.group(1)] + int(m.group(2))
    return dt.date(y, int(m.group(3)), int(m.group(4))).isoformat()


def mof_jgb() -> dict:
    """財務省の国債金利（過去分＋当月分）を {date: {tenor: yield}} で返す。"""
    tenors = ["1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "15Y", "20Y", "25Y", "30Y", "40Y"]
    data: dict[str, dict[str, float]] = {}
    for url in ("https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv",
                "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"):
        try:
            text = http_get(url).decode("cp932", errors="replace")
        except Exception as e:  # noqa: BLE001
            ERRORS.append(f"MOF {url}: {e}")
            continue
        for r in csv.reader(io.StringIO(text)):
            if not r:
                continue
            d = _jgb_date(r[0])
            if not d or d < (TODAY - dt.timedelta(days=800)).isoformat():
                continue
            row = {}
            for t, v in zip(tenors, r[1:]):
                try:
                    row[t] = float(v)
                except ValueError:
                    pass
            if row:
                data[d] = row
    if not data:
        raise RuntimeError("no JGB data")
    return dict(sorted(data.items()))


def estat_cpi(app_id: str) -> dict:
    """e-Stat API で全国CPI（総合／生鮮除く総合／生鮮・エネルギー除く総合）の前年同月比を取る。

    基準改定で表IDが変わるため、統計コード 00200573（消費者物価指数）の中から
    「全国」「中分類」「月次」の最新の表を検索して使う。
    """
    base = "https://api.e-stat.go.jp/rest/3.0/app/json/"
    q = urllib.parse.urlencode({"appId": app_id, "statsCode": "00200573",
                                "searchWord": "全国 中分類指数", "limit": 100})
    lst = json.loads(http_get(base + "getStatsList?" + q))
    tables = lst["GET_STATS_LIST"]["DATALIST_INF"].get("TABLE_INF", [])
    if isinstance(tables, dict):
        tables = [tables]

    def title(t):
        tt = t.get("TITLE")
        return tt.get("$", "") if isinstance(tt, dict) else str(tt or "")

    cands = [t for t in tables if "月" in str(t.get("CYCLE", "")) and "全国" in title(t) + str(t.get("STATISTICS_NAME", ""))]
    if not cands:
        raise RuntimeError("CPI table not found")
    cands.sort(key=lambda t: (str(t.get("SURVEY_DATE", "")), str(t.get("OPEN_DATE", ""))), reverse=True)
    table_id = cands[0]["@id"]

    meta = json.loads(http_get(base + "getMetaInfo?" + urllib.parse.urlencode({"appId": app_id, "statsDataId": table_id})))
    objs = meta["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
    classes = {o["@id"]: (o["CLASS"] if isinstance(o["CLASS"], list) else [o["CLASS"]]) for o in objs}

    def find(cls, pred):
        for c in classes.get(cls, []):
            if pred(c.get("@name", "")):
                return c["@code"]
        return None

    want = {"all": "総合", "core": "生鮮食品を除く総合", "corecore": "生鮮食品及びエネルギーを除く総合"}
    # 分類名は "0001 総合" のようにコードが前置されることがあるので末尾の語で照合
    cat_codes = {k: find("cat01", lambda n, v=v: (n.split()[-1] if n.split() else "") == v) for k, v in want.items()}
    tab = find("tab", lambda n: "前年同月比" in n)
    area = find("area", lambda n: "全国" in n)
    params = {"appId": app_id, "statsDataId": table_id, "cdCat01": ",".join(c for c in cat_codes.values() if c)}
    if tab:
        params["cdTab"] = tab
    if area:
        params["cdArea"] = area
    data = json.loads(http_get(base + "getStatsData?" + urllib.parse.urlencode(params)))
    values = data["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
    out: dict[str, list] = {k: [] for k in want}
    rev = {v: k for k, v in cat_codes.items() if v}
    for v in values:
        k = rev.get(v.get("@cat01"))
        t = v.get("@time", "")  # e.g. 2026000909 -> YYYY00MMMM
        m = re.match(r"(\d{4})00(\d{2})\d{2}$", t)
        if not k or not m:
            continue
        try:
            out[k].append((f"{m.group(1)}-{m.group(2)}-01", float(v["$"])))
        except (ValueError, KeyError):
            continue
    for k in out:
        out[k].sort()
    if not any(out.values()):
        raise RuntimeError("CPI values empty")
    return out


# ---------------------------------------------------------------- analytics
def pct(a, b):
    return None if a is None or b in (None, 0) else (a / b - 1) * 100


def value_on_or_before(series, date_iso):
    v = None
    for d, x in series:
        if d <= date_iso:
            v = x
        else:
            break
    return v


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0))
        losses.append(max(a - b, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def streak(closes):
    """連騰・連落日数（+ なら上昇の連続、- なら下落の連続）。"""
    s = 0
    for a, b in zip(reversed(closes[:-1]), reversed(closes[1:])):
        d = 1 if b > a else -1 if b < a else 0
        if d == 0 or (s and (d > 0) != (s > 0)):
            break
        s += d
    return s


def price_stats(series):
    if not series:
        return None
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    last_d, last = series[-1]
    prev = closes[-2] if len(closes) > 1 else None
    last_date = dt.date.fromisoformat(last_d)
    year_start = value_on_or_before(series, f"{last_date.year - 1}-12-31")
    w52 = [c for d, c in series if d > (last_date - dt.timedelta(days=365)).isoformat()]

    def ma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else None

    def ago(days):
        return value_on_or_before(series, (last_date - dt.timedelta(days=days)).isoformat())

    m25, m50, m200 = ma(25), ma(50), ma(200)
    return {
        "date": last_d,
        "last": last,
        "chg": None if prev is None else last - prev,
        "chg_pct": pct(last, prev),
        "w1_pct": pct(last, ago(7)),
        "m1_pct": pct(last, ago(30)),
        "m3_pct": pct(last, ago(91)),
        "ytd_pct": pct(last, year_start),
        "y1_pct": pct(last, ago(365)),
        "high52": max(w52) if w52 else None,
        "low52": min(w52) if w52 else None,
        "from_high_pct": pct(last, max(w52)) if w52 else None,
        "ma25_dev": pct(last, m25),
        "ma50_dev": pct(last, m50),
        "ma200_dev": pct(last, m200),
        "rsi14": rsi(closes[-120:]),
        "streak": streak(closes[-30:]),
        "spark": [[d, round(c, 4)] for d, c in series if d >= (last_date - dt.timedelta(days=183)).isoformat()],
    }


def level_stats(series, scale=1.0):
    """金利など水準系列（差分は bp 換算用に scale を掛ける）。"""
    if not series:
        return None
    last_d, last = series[-1]
    last_date = dt.date.fromisoformat(last_d)

    def ago(days):
        return value_on_or_before(series, (last_date - dt.timedelta(days=days)).isoformat())

    def diff(x):
        return None if x is None else (last - x) * scale

    prev = series[-2][1] if len(series) > 1 else None
    return {
        "date": last_d,
        "last": last,
        "chg": diff(prev),
        "w1": diff(ago(7)),
        "m1": diff(ago(30)),
        "ytd": diff(value_on_or_before(series, f"{last_date.year - 1}-12-31")),
        "y1": diff(ago(365)),
        "spark": [[d, round(c, 4)] for d, c in series if d >= (last_date - dt.timedelta(days=183)).isoformat()],
    }


def yoy_series(series, periods=12):
    out = []
    for i in range(periods, len(series)):
        d, v = series[i]
        _, b = series[i - periods]
        out.append((d, (v / b - 1) * 100))
    return out


def monthly_stats(index_series, name):
    """月次物価指数 → 前年比・前月比・3ヵ月年率。"""
    if not index_series or len(index_series) < 14:
        return None
    yoy = yoy_series(index_series)
    d, v = index_series[-1]
    mom = pct(v, index_series[-2][1])
    ann3 = ((v / index_series[-4][1]) ** 4 - 1) * 100
    return {
        "name": name,
        "date": d,
        "yoy": yoy[-1][1],
        "yoy_prev": yoy[-2][1] if len(yoy) > 1 else None,
        "yoy_3m_ago": yoy[-4][1] if len(yoy) > 3 else None,
        "mom": mom,
        "ann3m": ann3,
        "history": [[a, round(b, 2)] for a, b in yoy[-24:]],
    }


# ---------------------------------------------------------------- build
EQUITIES = [
    # key, 表示名, 地域, Yahoo, FRED フォールバック
    ("n225", "日経平均", "JP", "^N225", "NIKKEI225"),
    ("niy", "日経225先物 (CME・円建)", "JP", "NIY=F", None),
    ("spx", "S&P 500", "US", "^GSPC", "SP500"),
    ("dji", "NYダウ", "US", "^DJI", "DJIA"),
    ("ixic", "NASDAQ総合", "US", "^IXIC", "NASDAQCOM"),
    ("sox", "フィラデルフィア半導体 (SOX)", "US", "^SOX", None),
    ("rut", "ラッセル2000", "US", "^RUT", None),
]
MACRO_MARKETS = [
    # key, 表示名, 単位, Yahoo, FRED, BoJ(db,code)
    ("vix", "VIX 恐怖指数", "pt", "^VIX", "VIXCLS", None),
    ("usdjpy", "ドル円", "円", "JPY=X", "DEXJPUS", ("FM08", "FXERD04")),
    ("eurjpy", "ユーロ円", "円", "EURJPY=X", None, None),
    ("dxy", "ドルインデックス", "pt", "DX-Y.NYB", None, None),
    ("wti", "WTI原油", "$/bbl", "CL=F", "DCOILWTICO", None),
    ("gold", "金先物", "$/oz", "GC=F", None, None),
    ("btc", "ビットコイン", "$", "BTC-USD", "CBBTCUSD", None),
]


def market_series(yahoo_sym, fred_id=None, boj_code=None):
    """Yahoo を優先し、取れなければ FRED / 日銀にフォールバック。より新しい方を採用。"""
    cands = []
    if yahoo_sym:
        s = safe(f"Yahoo {yahoo_sym}", yahoo, yahoo_sym)
        if s:
            cands.append(("Yahoo Finance", s))
    if fred_id:
        s = safe(f"FRED {fred_id}", fred, fred_id)
        if s:
            cands.append(("FRED", s))
    if boj_code:
        s = safe(f"BoJ {boj_code[1]}", boj, boj_code[0], boj_code[1],
                 (TODAY - dt.timedelta(days=800)).strftime("%Y%m"))
        if s:
            cands.append(("日本銀行", s))
    if not cands:
        return None, None
    # 最新日付が新しいもの、同日なら先に並んだもの（Yahoo）を採用
    cands.sort(key=lambda c: c[1][-1][0], reverse=True)
    return cands[0]


def build():
    out: dict = {"generated_at": dt.datetime.now(JST).isoformat(timespec="minutes"), "sections": {}}

    # 株価指数
    eq = []
    for key, name, region, ysym, fid in EQUITIES:
        src, s = market_series(ysym, fid)
        st = price_stats(s) if s else None
        if st:
            eq.append({"key": key, "name": name, "region": region, "source": src, **st})
    out["equities"] = eq

    # 為替・商品・VIX
    mm = []
    for key, name, unit, ysym, fid, bcode in MACRO_MARKETS:
        src, s = market_series(ysym, fid, bcode)
        st = price_stats(s) if s else None
        if st:
            mm.append({"key": key, "name": name, "unit": unit, "source": src, **st})
    out["markets"] = mm

    # 米国金利
    us_rates = []
    for key, name, fid in [("ff_upper", "FF金利 誘導目標(上限)", "DFEDTARU"),
                           ("ff_lower", "FF金利 誘導目標(下限)", "DFEDTARL"),
                           ("effr", "実効FF金利", "EFFR"),
                           ("us3m", "米3ヵ月債", "DGS3MO"),
                           ("us2y", "米2年債", "DGS2"),
                           ("us10y", "米10年債", "DGS10"),
                           ("us30y", "米30年債", "DGS30"),
                           ("us2s10s", "2年-10年スプレッド", "T10Y2Y"),
                           ("us10real", "米10年実質金利 (TIPS)", "DFII10"),
                           ("us10bei", "10年ブレークイーブン・インフレ率", "T10YIE"),
                           ("hy_oas", "ハイイールド社債スプレッド", "BAMLH0A0HYM2")]:
        s = safe(f"FRED {fid}", fred, fid)
        st = level_stats(s, scale=100) if s else None
        if st:
            us_rates.append({"key": key, "name": name, "source": "FRED", **st})
    # 10年債は Yahoo(^TNX) の方が新しければ差し替え（当日の引け値）
    tnx = safe("Yahoo ^TNX", yahoo, "^TNX")
    if tnx:
        for r in us_rates:
            if r["key"] == "us10y" and tnx[-1][0] > r["date"]:
                r.update(level_stats(tnx, scale=100), source="Yahoo Finance")
    out["us_rates"] = us_rates

    # 米国債イールドカーブ（最新日）
    curve_ids = [("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"), ("1Y", "DGS1"), ("2Y", "DGS2"),
                 ("3Y", "DGS3"), ("5Y", "DGS5"), ("7Y", "DGS7"), ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]
    us_curve, us_curve_prev = {}, {}
    for tenor, fid in curve_ids:
        s = safe(f"FRED {fid}", fred, fid, 2)
        if s:
            us_curve[tenor] = s[-1]
            last_date = dt.date.fromisoformat(s[-1][0])
            us_curve_prev[tenor] = value_on_or_before(s, (last_date - dt.timedelta(days=30)).isoformat())
    out["us_curve"] = {
        "date": max((v[0] for v in us_curve.values()), default=None),
        "points": [[t, us_curve[t][1], us_curve_prev.get(t)] for t, _ in curve_ids if t in us_curve],
    }

    # 日本国債（財務省）
    jgb = safe("MOF JGB", mof_jgb)
    jp_rates = []
    if jgb:
        for tenor, name in [("2Y", "日本2年国債"), ("5Y", "日本5年国債"), ("10Y", "日本10年国債"),
                            ("20Y", "日本20年国債"), ("30Y", "日本30年国債"), ("40Y", "日本40年国債")]:
            s = [(d, row[tenor]) for d, row in jgb.items() if tenor in row]
            st = level_stats(s, scale=100)
            if st:
                jp_rates.append({"key": "jgb" + tenor.lower(), "name": name, "source": "財務省", **st})
        last_d = list(jgb)[-1]
        prev_d = max((d for d in jgb if d <= (dt.date.fromisoformat(last_d) - dt.timedelta(days=30)).isoformat()), default=None)
        out["jp_curve"] = {
            "date": last_d,
            "points": [[t, v, jgb[prev_d].get(t) if prev_d else None] for t, v in jgb[last_d].items()],
        }
    call = safe("BoJ call rate", boj, "FM01", "STRDCLUCON", (TODAY - dt.timedelta(days=800)).strftime("%Y%m"))
    if call:
        jp_rates.insert(0, {"key": "call", "name": "無担保コール翌日物 (政策金利の実効値)", "source": "日本銀行",
                            **level_stats(call, scale=100)})
    out["jp_rates"] = jp_rates

    # 日米金利差（10年）
    us10 = next((r for r in us_rates if r["key"] == "us10y"), None)
    jp10 = next((r for r in jp_rates if r["key"] == "jgb10y"), None)
    if us10 and jp10:
        out["spread_us_jp_10y"] = {"value": us10["last"] - jp10["last"], "us_date": us10["date"], "jp_date": jp10["date"]}

    # 物価・景気（米国）
    us_infl = []
    for fid, name in [("CPIAUCSL", "CPI 総合"), ("CPILFESL", "CPI コア (除く食品・エネルギー)"),
                      ("PCEPI", "PCE 総合"), ("PCEPILFE", "PCE コア (FRBの重視指標)"),
                      ("PPIFIS", "PPI 最終需要")]:
        s = safe(f"FRED {fid}", fred, fid, 4)
        st = monthly_stats(s, name) if s else None
        if st:
            us_infl.append({"key": fid, "source": "FRED", **st})
    out["us_inflation"] = us_infl

    labor = {}
    un = safe("FRED UNRATE", fred, "UNRATE", 3)
    if un:
        labor["unrate"] = {"date": un[-1][0], "last": un[-1][1], "prev": un[-2][1],
                           "low12m": min(v for _, v in un[-12:]),
                           "history": [[d, v] for d, v in un[-24:]]}
    nfp = safe("FRED PAYEMS", fred, "PAYEMS", 3)
    if nfp and len(nfp) > 4:
        ch = [(nfp[i][0], nfp[i][1] - nfp[i - 1][1]) for i in range(1, len(nfp))]
        labor["nfp"] = {"date": ch[-1][0], "last": ch[-1][1], "avg3m": sum(v for _, v in ch[-3:]) / 3,
                        "history": [[d, v] for d, v in ch[-24:]]}
    out["us_labor"] = labor

    # 物価（日本）
    jp_infl = []
    app_id = os.environ.get("ESTAT_APP_ID", "").strip()
    if app_id:
        cpi = safe("e-Stat CPI", estat_cpi, app_id)
        if cpi:
            for k, name in [("all", "CPI 総合"), ("core", "CPI コア (生鮮食品を除く)"),
                            ("corecore", "CPI コアコア (生鮮食品・エネルギーを除く)")]:
                s = cpi.get(k)
                if s:
                    jp_infl.append({"key": "jp_cpi_" + k, "name": name, "source": "総務省 (e-Stat)",
                                    "date": s[-1][0], "yoy": s[-1][1],
                                    "yoy_prev": s[-2][1] if len(s) > 1 else None,
                                    "yoy_3m_ago": s[-4][1] if len(s) > 3 else None,
                                    "history": [[d, v] for d, v in s[-24:]]})
    cgpi = safe("BoJ CGPI", boj, "PR01", "PRCG20_2200000000", (TODAY - dt.timedelta(days=365 * 4)).strftime("%Y%m"))
    if cgpi:
        st = monthly_stats(cgpi, "企業物価指数 (国内・総平均)")
        if st:
            jp_infl.append({"key": "jp_cgpi", "source": "日本銀行", **st})
    out["jp_inflation"] = jp_infl
    out["jp_cpi_configured"] = bool(app_id)

    out["commentary"] = commentary(out)
    out["errors"] = ERRORS
    return out


# ---------------------------------------------------------------- commentary
def _get(lst, key):
    return next((x for x in lst if x.get("key") == key), None)


def _sgn(x, digits=2, unit="%"):
    return f"{x:+.{digits}f}{unit}"


def _md(iso):
    return f"{int(iso[5:7])}/{int(iso[8:10])}"


def _move_word(st):
    s, c = st.get("streak") or 0, st.get("chg_pct") or 0
    if s >= 3:
        return f"{s}連騰"
    if s <= -3:
        return f"{-s}日続落"
    if s == 2:
        return "続伸"
    if s == -2:
        return "続落"
    return "上昇" if c > 0 else "下落" if c < 0 else "横ばい"


def commentary(d) -> dict:
    eq, mm = d["equities"], d["markets"]
    points: list[dict] = []  # {tag, text, tone: pos/neg/neu}
    risk = 0

    spx, dji, ixic, sox = (_get(eq, k) for k in ("spx", "dji", "ixic", "sox"))
    us_eq = [x for x in (spx, dji, ixic, sox) if x]
    us_date = max((x["date"] for x in us_eq), default=None)
    if spx:
        parts = []
        for x, label in ((spx, "S&P500"), (dji, "ダウ"), (ixic, "NASDAQ"), (sox, "SOX")):
            if x:
                stale = f"※{_md(x['date'])}時点" if x["date"] < us_date else ""
                parts.append(f"{label} {_sgn(x['chg_pct'])}{stale}")
        tone = "pos" if spx["chg_pct"] > 0 else "neg"
        risk += 1 if spx["chg_pct"] > 0.3 else -1 if spx["chg_pct"] < -0.3 else 0
        txt = f"{_md(spx['date'])}の米国株：S&P500は{_move_word(spx)}（{', '.join(parts)}）。"
        if spx.get("from_high_pct") is not None:
            fh = spx["from_high_pct"]
            txt += "S&P500は52週高値圏。" if fh > -1 else f"S&P500は52週高値から{fh:.1f}%。"
            if fh <= -10:
                txt += "調整局面（-10%超）入り。"
        points.append({"tag": "米国株", "text": txt, "tone": tone})
        if ixic and ixic["date"] == spx["date"] and abs(ixic["chg_pct"] - spx["chg_pct"]) > 0.7:
            lead = "ハイテク主導" if ixic["chg_pct"] > spx["chg_pct"] else "ハイテク劣後"
            points.append({"tag": "物色", "text": f"{lead}（NASDAQ−S&P500 = {ixic['chg_pct'] - spx['chg_pct']:+.2f}pt）。", "tone": "neu"})

    vix = _get(mm, "vix")
    if vix:
        v = vix["last"]
        lvl = ("極めて楽観的（低ボラ）" if v < 13 else "落ち着いている" if v < 17 else "やや警戒" if v < 22
               else "警戒水準" if v < 30 else "パニック的なリスクオフ")
        risk += 1 if v < 17 else -1 if v >= 22 else 0
        risk += -1 if (vix["chg_pct"] or 0) > 15 else 0
        points.append({"tag": "VIX", "text": f"VIXは{v:.1f}（前日比{_sgn(vix['chg_pct'], 1)}）で{lvl}。",
                       "tone": "pos" if v < 17 else "neg" if v >= 22 else "neu"})

    us10 = _get(d["us_rates"], "us10y")
    us2 = _get(d["us_rates"], "us2y")
    s210 = _get(d["us_rates"], "us2s10s")
    if us10:
        txt = f"米10年債利回り {us10['last']:.2f}%（前日比{_sgn(us10['chg'] or 0, 0, 'bp')}、1ヵ月{_sgn(us10['m1'] or 0, 0, 'bp')}）。"
        if us2:
            txt += f"2年債 {us2['last']:.2f}%。"
        if s210:
            sp = s210["last"]
            txt += "2-10年は逆イールド（景気後退シグナル）。" if sp < 0 else f"2-10年スプレッドは{sp*100:+.0f}bp。"
        points.append({"tag": "米金利", "text": txt, "tone": "neg" if (us10["chg"] or 0) > 5 else "neu"})

    ff = _get(d["us_rates"], "ff_upper")
    ffl = _get(d["us_rates"], "ff_lower")
    pce = next((x for x in d["us_inflation"] if x["key"] == "PCEPILFE"), None)
    cpi = next((x for x in d["us_inflation"] if x["key"] == "CPIAUCSL"), None)
    if ff and ffl:
        txt = f"FF金利誘導目標 {ffl['last']:.2f}–{ff['last']:.2f}%。"
        if pce:
            real = ff["last"] - pce["yoy"]
            txt += f"コアPCE {pce['yoy']:.1f}%（{pce['date'][:7]}）を差し引いた実質政策金利は約{real:+.1f}%"
            txt += ("で明確に引き締め的。" if real > 1.5 else "でやや引き締め的。" if real > 0.5
                    else "で中立圏。" if real > -0.5 else "で緩和的。")
        if cpi:
            trend = cpi["yoy"] - (cpi["yoy_3m_ago"] or cpi["yoy"])
            txt += f"CPI総合 {cpi['yoy']:.1f}%（3ヵ月前比{trend:+.1f}pt）。"
        points.append({"tag": "FRB/物価", "text": txt, "tone": "neu"})

    n225, niy = _get(eq, "n225"), _get(eq, "niy")
    if n225:
        txt = f"{_md(n225['date'])}の日経平均 {n225['last']:,.0f}円（{_sgn(n225['chg_pct'])}、{_move_word(n225)}）。"
        if n225.get("ma25_dev") is not None:
            dev = n225["ma25_dev"]
            txt += f"25日線乖離 {dev:+.1f}%"
            txt += "（過熱圏）。" if dev > 5 else "（売られすぎ圏）。" if dev < -5 else "。"
        if niy and niy["date"] >= n225["date"]:
            gap = niy["last"] - n225["last"]
            txt += f"CME日経先物は{niy['last']:,.0f}円で、現物終値比{gap:+,.0f}円 → 寄り付きは{'高く' if gap > 0 else '安く'}始まる可能性。"
        points.append({"tag": "日本株", "text": txt, "tone": "pos" if n225["chg_pct"] > 0 else "neg"})

    usdjpy = _get(mm, "usdjpy")
    if usdjpy:
        txt = f"ドル円 {usdjpy['last']:.2f}円（前日比{usdjpy['chg']:+.2f}円、1ヵ月{_sgn(usdjpy['m1_pct'] or 0, 1)}）。"
        if d.get("spread_us_jp_10y"):
            txt += f"日米10年金利差 {d['spread_us_jp_10y']['value']:.2f}%。"
        if usdjpy["last"] >= 155:
            txt += "介入警戒が意識されやすい水準。"
        points.append({"tag": "為替", "text": txt, "tone": "neu"})

    jgb10 = _get(d["jp_rates"], "jgb10y")
    call = _get(d["jp_rates"], "call")
    if jgb10:
        txt = f"日本10年国債 {jgb10['last']:.3f}%（前日比{_sgn(jgb10['chg'] or 0, 1, 'bp')}、年初来{_sgn(jgb10['ytd'] or 0, 0, 'bp')}）。"
        if call:
            txt += f"無担保コール翌日物 {call['last']:.3f}%。"
        points.append({"tag": "日本金利", "text": txt, "tone": "neg" if (jgb10["chg"] or 0) > 3 else "neu"})

    wti, gold = _get(mm, "wti"), _get(mm, "gold")
    if wti or gold:
        parts = []
        if wti:
            parts.append(f"WTI {wti['last']:.2f}ドル（{_sgn(wti['chg_pct'])}）")
        if gold:
            parts.append(f"金 {gold['last']:,.0f}ドル（{_sgn(gold['chg_pct'])}）")
        points.append({"tag": "商品", "text": "、".join(parts) + "。", "tone": "neu"})

    # 急変動（直近営業日に ±3% 以上動いた指数・商品。古い日付のデータは除外）
    latest = max((x["date"] for x in eq + mm), default="")
    movers = []
    for st in eq + [x for x in mm if x["key"] in ("wti", "gold", "btc")]:
        c = st.get("chg_pct")
        lim = 5 if st["key"] == "btc" else 3
        fresh = (dt.date.fromisoformat(latest) - dt.date.fromisoformat(st["date"])).days <= 1 if latest else True
        if c is not None and abs(c) >= lim and fresh:
            movers.append(st)
    if movers:
        movers.sort(key=lambda x: -abs(x["chg_pct"]))
        downs = sum(1 for x in movers if x["chg_pct"] < 0 and x in eq)
        ups = sum(1 for x in movers if x["chg_pct"] > 0 and x in eq)
        risk += -1 if downs >= 2 else 1 if ups >= 2 else 0
        points.insert(0, {"tag": "急変", "text": "大きく動いた資産：" + "、".join(
            f"{x['name']} {_sgn(x['chg_pct'])}（{_md(x['date'])}）" for x in movers) + "。",
            "tone": "neg" if downs > ups else "pos" if ups > downs else "neu"})

    # テクニカル警告
    for st in eq:
        r = st.get("rsi14")
        if r is not None and (r >= 70 or r <= 30):
            points.append({"tag": "テクニカル", "text": f"{st['name']} RSI(14)={r:.0f}：{'買われすぎ' if r >= 70 else '売られすぎ'}。",
                           "tone": "neg" if r >= 70 else "pos"})

    mood = ("リスクオン" if risk >= 2 else "ややリスクオン" if risk == 1 else "中立" if risk == 0
            else "ややリスクオフ" if risk == -1 else "リスクオフ")
    headline_bits = []
    if spx:
        headline_bits.append(f"米株{_move_word(spx)}")
    if n225:
        headline_bits.append(f"日経平均{_sgn(n225['chg_pct'])}")
    if usdjpy:
        headline_bits.append(f"ドル円{usdjpy['last']:.1f}円")
    if us10:
        headline_bits.append(f"米10年{us10['last']:.2f}%")
    return {"mood": mood, "risk_score": risk, "headline": "／".join(headline_bits), "points": points}


def _clean(o):
    """NaN/inf を JSON の null に、浮動小数を適度に丸める。"""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else round(o, 4)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def main():
    data = _clean(build())
    have = len(data["equities"]) + len(data["markets"]) + len(data["us_rates"]) + len(data["jp_rates"])
    if have == 0:
        print("ERROR: no data fetched; keeping previous data.json", file=sys.stderr)
        sys.exit(1)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT} ({have} series, {len(ERRORS)} warnings)")


if __name__ == "__main__":
    main()
