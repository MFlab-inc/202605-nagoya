/**
 * FX デイトレ用 夕方レート — スプレッドシート内「更新ボタン」版 (Google Apps Script)
 *
 * これはスプレッドシートに直接組み込んで動かすコードです。PC・Python・合鍵JSON は
 * 一切不要。シートを開ける人なら誰でも、メニュー「FXレート → 今すぐ更新」を押すだけで
 * 最新のFXレート＋CME通貨先物(6E/6B/6A/6J)を取得して書き込みます。
 *
 * データ元: Yahoo Finance の chart API (UrlFetchApp で取得)。
 *
 * 列(日本語ヘッダー):
 *   銘柄, 名称, 日付, 現在値, 本日高値, 本日安値, 前日終値, 前日比,
 *   変化率(%), 出来高, 建玉, 取得元, 取得時刻(UTC), 状態
 *
 * data_status(状態): ok / preliminary / final / stale / error
 *
 * ※ 建玉(open interest) は chart API には含まれないため、この「ボタン版」では空欄に
 *    なります(PC版の python では取得します)。価格・高安・出来高・状態は取得します。
 */

// ----- 設定 ---------------------------------------------------------------
var SHEET_NAME = 'fx_evening';          // 書き込み先タブ名
var SOURCE = 'Yahoo Finance';
var STALE_HOURS = 12;                    // これより古いと「stale」
var FINAL_PUBLISH_HOUR_JST = 22;         // 先物FINAL確定の目安時刻(JST)

// 取得する銘柄 (symbol, 名称, Yahooティッカー, 種類, 小数桁)
var INSTRUMENTS = [
  ['USDJPY', 'US Dollar / Japanese Yen',        'JPY=X',    'fx',     3],
  ['EURUSD', 'Euro / US Dollar',                'EURUSD=X', 'fx',     5],
  ['GBPUSD', 'British Pound / US Dollar',       'GBPUSD=X', 'fx',     5],
  ['AUDUSD', 'Australian Dollar / US Dollar',   'AUDUSD=X', 'fx',     5],
  ['EURJPY', 'Euro / Japanese Yen',             'EURJPY=X', 'fx',     3],
  ['GBPJPY', 'British Pound / Japanese Yen',    'GBPJPY=X', 'fx',     3],
  ['AUDJPY', 'Australian Dollar / Japanese Yen','AUDJPY=X', 'fx',     3],
  ['DXY',    'US Dollar Index',                 'DX-Y.NYB', 'fx',     3],
  ['6E',     'CME Euro FX future',              '6E=F',     'future', 5],
  ['6B',     'CME British Pound future',        '6B=F',     'future', 5],
  ['6A',     'CME Australian Dollar future',    '6A=F',     'future', 5],
  ['6J',     'CME Japanese Yen future',         '6J=F',     'future', 7]
];

var HEADERS_JA = ['銘柄', '名称', '日付', '現在値', '本日高値', '本日安値',
  '前日終値', '前日比', '変化率(%)', '出来高', '建玉', '取得元', '取得時刻(UTC)', '状態'];

// ----- メニュー(ボタン) ----------------------------------------------------
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('FXレート')
    .addItem('今すぐ更新', 'updateRates')
    .addToUi();
}

// ----- メインの更新処理 ----------------------------------------------------
function updateRates() {
  var now = new Date();
  var settled = settledThrough(now);
  var records = INSTRUMENTS.map(function (inst) {
    try {
      return buildRecord(inst, fetchBars(inst[2]), now, settled);
    } catch (e) {
      return errorRecord(inst, now, String(e));
    }
  });

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
  var existing = sheet.getDataRange().getValues();
  var grid = upsert(existing, records);

  sheet.clearContents();
  sheet.getRange(1, 1, grid.length, grid[0].length).setValues(grid);
  sheet.setFrozenRows(1);

  var counts = {};
  records.forEach(function (r) { counts[r[13]] = (counts[r[13]] || 0) + 1; });
  ss.toast('更新しました: ' + JSON.stringify(counts), 'FXレート', 6);
}

// ----- Yahoo Finance から日足を取得 ---------------------------------------
function fetchBars(yahoo) {
  var url = 'https://query1.finance.yahoo.com/v8/finance/chart/'
    + encodeURIComponent(yahoo) + '?interval=1d&range=10d';
  var resp = UrlFetchApp.fetch(url, {
    muteHttpExceptions: true,
    headers: { 'User-Agent': 'Mozilla/5.0' }
  });
  if (resp.getResponseCode() !== 200) {
    throw new Error('HTTP ' + resp.getResponseCode());
  }
  var data = JSON.parse(resp.getContentText());
  var result = data.chart && data.chart.result && data.chart.result[0];
  if (!result || !result.timestamp) throw new Error('no data');

  var ts = result.timestamp;
  var q = (result.indicators && result.indicators.quote && result.indicators.quote[0]) || {};
  var bars = [];
  for (var i = 0; i < ts.length; i++) {
    var c = q.close ? q.close[i] : null;
    if (c === null || c === undefined) continue;
    bars.push({
      date: Utilities.formatDate(new Date(ts[i] * 1000), 'Asia/Tokyo', 'yyyy-MM-dd'),
      high: q.high ? q.high[i] : null,
      low: q.low ? q.low[i] : null,
      close: c,
      volume: q.volume ? q.volume[i] : null
    });
  }
  return bars;
}

// ----- 1銘柄ぶんの行を組み立てる ------------------------------------------
function buildRecord(inst, bars, now, settled) {
  var symbol = inst[0], name = inst[1], kind = inst[3], dp = inst[4];
  var usable = bars.filter(function (b) { return b.close !== null && b.close !== undefined; });
  if (usable.length === 0) return errorRecord(inst, now, 'no usable bars');

  var latest = usable[usable.length - 1];
  var prev = usable.length > 1 ? usable[usable.length - 2] : null;

  var last = latest.close;
  var prevClose = prev ? prev.close : null;
  var changeAbs = (prevClose === null) ? null : last - prevClose;
  var changePct = (prevClose === null || prevClose === 0)
    ? null : (last - prevClose) / Math.abs(prevClose) * 100;

  var obsDate = latest.date;
  var age = obsAgeHours(obsDate, now);
  var status = decideStatus(kind, last !== null, age, STALE_HOURS, obsDate, settled);

  return [
    symbol,
    name,
    obsDate,
    round(last, dp),
    round(latest.high, dp),
    round(latest.low, dp),
    round(prevClose, dp),
    round(changeAbs, dp),
    round(changePct, 3),
    (latest.volume === null || latest.volume === undefined) ? '' : Math.round(latest.volume),
    '',                                  // 建玉(OI)はボタン版では空欄
    SOURCE,
    Utilities.formatDate(now, 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'"),
    status
  ];
}

function errorRecord(inst, now, msg) {
  return [
    inst[0], inst[1], '', '', '', '', '', '', '', '', '',
    SOURCE, Utilities.formatDate(now, 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'"), 'error'
  ];
}

// ----- 状態(ok/preliminary/final/stale/error)を決める --------------------
function decideStatus(kind, hasPrice, ageHours, staleAfter, obsDate, settledDate) {
  if (!hasPrice) return 'error';
  if (ageHours !== null && ageHours > staleAfter) return 'stale';
  if (kind === 'future') {
    if (obsDate && settledDate && obsDate > settledDate) return 'preliminary';
    return 'final';
  }
  return 'ok';
}

function settledThrough(now) {
  var hour = Number(Utilities.formatDate(now, 'Asia/Tokyo', 'H'));
  var days = (hour < FINAL_PUBLISH_HOUR_JST) ? 2 : 1;
  var base = new Date(now.getTime() - days * 24 * 3600 * 1000);
  return Utilities.formatDate(base, 'Asia/Tokyo', 'yyyy-MM-dd');
}

function obsAgeHours(obsDate, now) {
  var p = obsDate.split('-');
  if (p.length !== 3) return null;
  // JST 00:00 of the day AFTER obsDate == UTC obsDate 15:00
  var endUtcMs = Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2]), 15, 0, 0);
  return Math.max(0, (now.getTime() - endUtcMs) / 3600000);
}

// ----- (銘柄, 日付)で上書き、無ければ追記 ----------------------------------
function upsert(existing, rows) {
  var headerSet = {};
  headerSet['symbol'] = true;
  headerSet[HEADERS_JA[0]] = true;

  var body;
  if (existing.length && existing[0].length && headerSet[existing[0][0]]) {
    body = existing.slice(1);
  } else {
    body = existing.filter(function (r) { return r.length && !headerSet[r[0]]; });
  }

  var index = {};
  body.forEach(function (r, i) {
    if (r.length > 2) index[r[0] + '|' + r[2]] = i;
  });

  rows.forEach(function (row) {
    var key = row[0] + '|' + row[2];
    if (key in index) {
      body[index[key]] = row;
    } else {
      index[key] = body.length;
      body.push(row);
    }
  });

  return [HEADERS_JA].concat(body);
}

// ----- 小数の丸め(空安全) --------------------------------------------------
function round(v, decimals) {
  if (v === null || v === undefined || v === '') return '';
  var f = Math.pow(10, decimals);
  return Math.round(v * f) / f;
}

// ----- (任意)毎日自動更新のトリガーを作る ---------------------------------
// この関数を一度だけ実行すると、毎日16時台に自動で updateRates が走ります。
function createDailyTrigger() {
  // 既存の同名トリガーを消してから作り直す
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'updateRates') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateRates')
    .timeBased()
    .atHour(16)
    .nearMinute(15)
    .everyDays(1)
    .inTimezone('Asia/Tokyo')
    .create();
}
