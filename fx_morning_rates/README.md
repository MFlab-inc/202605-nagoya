# FX デイトレ用 朝レート配信 — FRED 取得モジュール

FXデイトレ用「朝レート配信」自動化の **データソース層（ステージ1: JST 10:30 配信）** のうち、
FRED API から **前日確定データ** を取得して Google Sheets に追記（upsert）する部分です。

当日現在値は使いません。FRED は確定済み観測値のみを返すため、要件「前日確定データのみ使用」と一致します。

## 取得シリーズ

| ラベル        | FRED series_id | 内容 |
|---------------|----------------|------|
| US2Y          | DGS2           | 米2年国債利回り |
| US5Y          | DGS5           | 米5年国債利回り |
| US10Y         | DGS10          | 米10年国債利回り |
| US30Y         | DGS30          | 米30年国債利回り |
| US10Y_REAL    | DFII10         | 米10年 実質利回り（TIPS） |
| HY_OAS        | BAMLH0A0HYM2   | 米ハイイールド OAS |
| SOFR          | SOFR           | SOFR |
| IORB          | IORB           | 準備預金付利金利 |
| RRP           | RRPONTSYD      | ON RRP 受入総額（USD bn） |
| RESERVES      | WRESBAL        | 準備預金残高（週次, USD bn） |

シリーズの差し替えは `fred_fetcher.py` の `SERIES_MAP` を編集してください。

## 保存スキーマ（Google Sheets の列）

```
series_id, observation_date, value, previous_value,
change_abs, change_pct, source, fetched_at_utc, data_status
```

- 重複は `(series_id, observation_date)` で **上書き**（upsert）。
- 取得失敗・欠損時は **停止せず** `data_status = error`（value/previous は空欄）。
- `data_status` は `ok` / `error`。

## 環境変数

| 変数 | 必須 | 説明 |
|------|------|------|
| `FRED_API_KEY` | ◯ | FRED API キー |
| `GOOGLE_SHEETS_ID` | ◯ | 出力先スプレッドシート ID |
| `GOOGLE_SHEETS_WORKSHEET` | | タブ名（既定: `fred_daily`） |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | △ | サービスアカウント JSON（インライン）|
| `GOOGLE_APPLICATION_CREDENTIALS` | △ | サービスアカウント JSON ファイルのパス |

`GOOGLE_SERVICE_ACCOUNT_JSON` か `GOOGLE_APPLICATION_CREDENTIALS` のどちらかを設定します。
対象スプレッドシートはサービスアカウントのメールアドレスに編集権限を共有してください。

## 使い方

```bash
pip install -r requirements.txt

# テスト（ネットワーク不要）
python fred_fetcher.py

# 本番取得 + Google Sheets へ upsert
python fred_fetcher.py --run
```

`cron` で JST 10:30 に `python fred_fetcher.py --run` を実行する想定です
（前日確定データのため、当日の市場オープン前でも安定して取得できます）。

## テスト

`run_tests()` は外部通信なしで以下を検証します。

- 変化量・変化率の計算（ゼロ除算回避を含む）
- FRED の欠損値 `"."` の除外と新しい順ソート
- 正常取得時の行生成
- 取得失敗時に例外を投げず `data_status = error` になること
- 全欠損時に `error` になること
- `(series_id, observation_date)` による upsert（上書き / 追記）
- 1シリーズが失敗してもループが止まらないこと
