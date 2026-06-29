# FX デイトレ用 レート配信モジュール

FXデイトレ用「レート配信」自動化のデータ取得部分です。2つのスクリプトがあります。

| ファイル | 配信 | 内容 |
|----------|------|------|
| `fred_fetcher.py` | ステージ1（朝 JST 10:30） | FRED から **前日確定** の金利・マクロ系列を取得 |
| `fx_fetcher.py`   | ステージ2（夕方 JST 16:10〜16:30） | FX レート＋CME通貨先物(6E/6B/6A/6J)の値・高安・出来高・建玉を取得 |

どちらも Google Sheets に追記（`(キー, observation_date)` で上書き）し、`data_status` を付けます。

> 🟢 **はじめての方へ:** Googleスプレッドシート連携と実行までの手順は
> **[SETUP.md](SETUP.md)** にやさしくまとめてあります。まずはそちらから。

> ⚠️ 注意: 開発環境では外部サイトへの通信がブロックされているため、**実データ取得はあなたのPC/サーバー（cron）で実行**してください。計算ロジックはオフラインのテストで検証済みです。

---

## ステージ2: 夕方配信（`fx_fetcher.py`）

データ元は **Yahoo Finance（無料・登録不要）** です。

### 取得する銘柄

- FX: USDJPY, EURUSD, GBPUSD, AUDUSD, EURJPY, GBPJPY, AUDJPY, DXY(ドル指数)
- CME 通貨先物: 6E(ユーロ), 6B(ポンド), 6A(豪ドル), 6J(円) … 値・本日高安・出来高・建玉(OI)

### data_status の意味

| 値 | 意味 |
|----|------|
| `ok` | 新しいFXレート |
| `preliminary` | 先物のまだ確定していない（速報）出来高・建玉 |
| `final` | 確定済みセッションの出来高・建玉 |
| `stale` | 最新データが古い（更新が止まっている） |
| `error` | 取得失敗・データなし |

### まだ入っていないもの（要相談）

夕方配信で指定された **Saxo FX Options Analytics（0700 GMT）** と **Pin Risk** は、
Saxo のアカウント/データアクセスが必要な別ソースのため、このモジュールには含めていません。
本モジュールはその土台となる「価格・出来高・建玉」を提供します。Saxo データの入手方法が決まれば追加します。

### 使い方

```bash
pip install -r requirements.txt
python fx_fetcher.py          # テスト（通信不要）
python fx_fetcher.py --run    # 本番取得 + Sheets へ upsert
```

cron で JST 16:10 に `python fx_fetcher.py --run` を実行する想定です。

環境変数: `GOOGLE_SHEETS_ID`(必須), `GOOGLE_SHEETS_WORKSHEET_FX`(既定 `fx_evening`),
`GOOGLE_SERVICE_ACCOUNT_JSON` か `GOOGLE_APPLICATION_CREDENTIALS`(認証), `FX_STALE_AFTER_HOURS`(既定 12)。

---

## ステージ1: 朝配信（`fred_fetcher.py`）

FRED API から **前日確定データ** を取得して Google Sheets に追記（upsert）します。

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
