# Morning Market Brief

日米の株価・金利・為替・物価を毎朝自動で集約し、アナリスト目線の指標と要点コメントを
1 ページにまとめるダッシュボードです。

- ページ: `market/index.html`（`market/data.json` を読み込んで表示）
- データ取得: `market/fetch_data.py`（Python 標準ライブラリのみ）
- 自動更新: `.github/workflows/market-brief.yml`（毎日 06:15 / 07:30 JST）

## 掲載内容

| 区分 | 内容 | 主なソース |
|---|---|---|
| 今朝のポイント | 地合い判定（リスクオン/オフ）、急変動、米株・VIX・金利・FRB・日本株・為替・JGB・商品・テクニカルの要点 | 下記データから自動生成 |
| 株価指数 | 日経平均、CME日経先物、S&P500、NYダウ、NASDAQ、SOX、ラッセル2000 — 前日比・1週/1ヵ月/年初来・52週高値比・25日/200日線乖離・RSI(14)・6ヵ月推移 | Yahoo Finance（失敗時 FRED） |
| 為替・商品 | VIX、ドル円、ユーロ円、ドル指数、WTI、金、ビットコイン | Yahoo Finance / FRED / 日本銀行 |
| 米国金利 | FF金利誘導目標、実効FF、3M/2Y/10Y/30Y、2-10年スプレッド、実質金利、BEI、ハイイールドスプレッド、イールドカーブ | FRED（10年は Yahoo の当日値を優先） |
| 日本金利 | 無担保コール O/N、国債 2〜40年、イールドカーブ、日米10年金利差 | 日本銀行 / 財務省 |
| 物価・雇用 | 米 CPI/コアCPI/PCE/コアPCE/PPI（前年比・前月比・3ヵ月年率）、失業率、雇用者数、日本の企業物価指数、日本CPI（任意） | FRED / 日本銀行 / e-Stat |

## 初回セットアップ

1. このブランチを `master` にマージする（スケジュール実行はデフォルトブランチでのみ動きます）。
2. GitHub の **Settings → Pages** で `master` ブランチ / `(root)` を公開元に設定する。
   公開 URL は `https://<ユーザー名>.github.io/<リポジトリ名>/market/` になります。
3. **Settings → Actions → General → Workflow permissions** を「Read and write permissions」にする
   （ワークフローが `data.json` をコミットするため）。
4. **Actions → Morning market brief → Run workflow** で一度手動実行して動作確認する。

### 日本の消費者物価指数を表示する（任意）

日本の CPI（総合・コア・コアコア）は総務省 e-Stat API から取得します。無料のアプリケーション ID が必要です。

1. <https://www.e-stat.go.jp/api/> でユーザー登録し、アプリケーション ID を発行する。
2. リポジトリの **Settings → Secrets and variables → Actions** に `ESTAT_APP_ID` として登録する。

未設定の場合、日本 CPI の欄には統計局へのリンクが表示されます（企業物価指数は常に表示）。

## ローカルで確認する

```bash
python3 market/fetch_data.py          # market/data.json を更新
python3 -m http.server 8000           # http://localhost:8000/market/ を開く
```

## 注意

- 各値の横に表示される日付が、その値の基準日です。祝日や公表ラグ（日銀・FRED は 1〜数営業日遅れ）により日付が揃わない場合があります。
- Yahoo Finance が一時的に応答しない場合は FRED / 日本銀行の値に自動で切り替わります（その場合、米国株は 1 日前の終値になることがあります）。取得できなかった項目はページ下部に表示されます。
- 本ページは情報提供のみを目的とした自動生成資料です。投資判断はご自身の責任で行ってください。
