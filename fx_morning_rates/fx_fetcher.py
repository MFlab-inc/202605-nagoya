"""FX & CME currency-futures fetcher for the JST 16:10-16:30 evening
distribution (stage 2) of the FX day-trade rate distribution.

This covers the *price* layer of the evening distribution: spot FX rates
(USDJPY, EURUSD, ...) and the matching CME currency futures
(6E=Euro, 6B=Pound, 6A=Aussie, 6J=Yen) with current value, the day's
high/low, previous close, change, volume and open interest. Each row is
tagged with a `data_status` so the consumer can tell apart:

    ok          - a fresh spot FX quote
    preliminary - a futures session that has not published FINAL vol/OI yet
    final       - a settled futures session (FINAL volume / open interest)
    stale       - the latest observation is older than the freshness window
    error       - the fetch failed or returned no usable price

Data source: Yahoo Finance via the free `yfinance` package (no signup).
The *pure* logic (record building, status, sheet upsert) is unit-tested
offline; the live fetch runs wherever this script is scheduled (cron).

NOTE: the Saxo FX Options Analytics (0700 GMT) feed and Pin-Risk numbers
are a separate, access-gated data source and are intentionally NOT fetched
here — see README. This module is the price/volume/OI foundation they sit
on top of.

Environment variables (live runs)
---------------------------------
  GOOGLE_SHEETS_ID              target spreadsheet id
  GOOGLE_SHEETS_WORKSHEET_FX    worksheet/tab name (default: "fx_evening")
  GOOGLE_SERVICE_ACCOUNT_JSON   inline service-account JSON, OR
  GOOGLE_APPLICATION_CREDENTIALS  path to a service-account JSON file
  FX_STALE_AFTER_HOURS          freshness window in hours (default: 12)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Sequence


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SOURCE = "Yahoo Finance"
JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Instrument:
    symbol: str          # short label shown in the sheet (e.g. "USDJPY", "6J")
    name: str            # human description
    yahoo: str           # Yahoo Finance ticker
    kind: str            # "fx" or "future"


# Spot FX pairs + the four CME currency futures the evening distribution needs.
INSTRUMENTS: List[Instrument] = [
    # --- spot FX -----------------------------------------------------------
    Instrument("USDJPY", "US Dollar / Japanese Yen", "JPY=X", "fx"),
    Instrument("EURUSD", "Euro / US Dollar", "EURUSD=X", "fx"),
    Instrument("GBPUSD", "British Pound / US Dollar", "GBPUSD=X", "fx"),
    Instrument("AUDUSD", "Australian Dollar / US Dollar", "AUDUSD=X", "fx"),
    Instrument("EURJPY", "Euro / Japanese Yen", "EURJPY=X", "fx"),
    Instrument("GBPJPY", "British Pound / Japanese Yen", "GBPJPY=X", "fx"),
    Instrument("AUDJPY", "Australian Dollar / Japanese Yen", "AUDJPY=X", "fx"),
    Instrument("DXY", "US Dollar Index", "DX-Y.NYB", "fx"),
    # --- CME currency futures (front month) --------------------------------
    Instrument("6E", "CME Euro FX future", "6E=F", "future"),
    Instrument("6B", "CME British Pound future", "6B=F", "future"),
    Instrument("6A", "CME Australian Dollar future", "6A=F", "future"),
    Instrument("6J", "CME Japanese Yen future", "6J=F", "future"),
]

COLUMNS: Sequence[str] = (
    "symbol",
    "name",
    "observation_date",
    "last",
    "day_high",
    "day_low",
    "previous_close",
    "change_abs",
    "change_pct",
    "volume",
    "open_interest",
    "source",
    "fetched_at_utc",
    "data_status",
)

# data_status values
STATUS_OK = "ok"
STATUS_PRELIMINARY = "preliminary"
STATUS_FINAL = "final"
STATUS_STALE = "stale"
STATUS_ERROR = "error"


# --------------------------------------------------------------------------- #
# Record model
# --------------------------------------------------------------------------- #

@dataclass
class FxRecord:
    symbol: str
    name: str = ""
    observation_date: str = ""
    last: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    previous_close: Optional[float] = None
    change_abs: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    source: str = SOURCE
    fetched_at_utc: str = ""
    data_status: str = STATUS_OK
    error: str = field(default="", repr=False)  # internal note, not a column

    def to_row(self) -> List[str]:
        d = asdict(self)

        def fmt(v) -> str:
            if v is None:
                return ""
            if isinstance(v, float):
                return repr(v)
            return str(v)

        return [fmt(d[col]) for col in COLUMNS]

    @property
    def key(self) -> tuple:
        return (self.symbol, self.observation_date)


# --------------------------------------------------------------------------- #
# Pure helpers (fully unit-tested, no network)
# --------------------------------------------------------------------------- #

def _utc_now_iso(now_utc: Optional[datetime] = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_changes(last: Optional[float], prev: Optional[float]) -> tuple:
    """Return (change_abs, change_pct); pct is None if prev is 0/None."""
    if last is None or prev is None:
        return None, None
    change_abs = round(last - prev, 6)
    if prev == 0:
        return change_abs, None
    return change_abs, round((last - prev) / abs(prev) * 100.0, 6)


def decide_status(kind: str,
                  has_price: bool,
                  obs_age_hours: Optional[float],
                  stale_after_hours: float,
                  obs_date: str = "",
                  settled_through_date: str = "") -> str:
    """Decide the data_status for one instrument.

    * no usable price                       -> error
    * latest obs older than the window      -> stale
    * future whose session is not settled   -> preliminary
    * future whose session has settled       -> final
    * spot FX                                -> ok
    """
    if not has_price:
        return STATUS_ERROR
    if obs_age_hours is not None and obs_age_hours > stale_after_hours:
        return STATUS_STALE
    if kind == "future":
        # A bar dated after the last settled session = not yet final.
        if obs_date and settled_through_date and obs_date > settled_through_date:
            return STATUS_PRELIMINARY
        return STATUS_FINAL
    return STATUS_OK


def settled_through(now_jst: datetime, final_publish_hour_jst: int = 22) -> str:
    """Date (YYYY-MM-DD) through which CME futures FINAL vol/OI is available.

    Heuristic for the JST 16:10-16:30 run: the prior US session's FINAL
    figures are published overnight. Before the configured publish hour we
    only trust up to two days back; after it, up to yesterday.
    """
    base = now_jst - timedelta(days=1)
    if now_jst.hour < final_publish_hour_jst:
        base = now_jst - timedelta(days=2)
    return base.strftime("%Y-%m-%d")


def build_record(inst: Instrument,
                 bars: List[dict],
                 open_interest: Optional[float],
                 now_utc: datetime,
                 now_jst: datetime,
                 stale_after_hours: float,
                 settled_through_date: str) -> FxRecord:
    """Build an FxRecord from already-extracted daily bars.

    `bars` is a list of dicts {date, high, low, close, volume} ordered
    oldest -> newest. Never raises: empty/unusable input yields an error row.
    """
    rec = FxRecord(symbol=inst.symbol, name=inst.name,
                   fetched_at_utc=_utc_now_iso(now_utc))
    usable = [b for b in bars if b.get("close") is not None]
    if not usable:
        rec.data_status = STATUS_ERROR
        rec.error = "no usable price bars"
        return rec

    latest = usable[-1]
    prev = usable[-2] if len(usable) > 1 else None

    rec.observation_date = str(latest.get("date", ""))
    rec.last = _as_float(latest.get("close"))
    rec.day_high = _as_float(latest.get("high"))
    rec.day_low = _as_float(latest.get("low"))
    rec.volume = _as_float(latest.get("volume"))
    rec.previous_close = _as_float(prev.get("close")) if prev else None
    rec.open_interest = _as_float(open_interest)
    rec.change_abs, rec.change_pct = compute_changes(rec.last, rec.previous_close)

    obs_age = _obs_age_hours(rec.observation_date, now_jst)
    rec.data_status = decide_status(
        kind=inst.kind,
        has_price=rec.last is not None,
        obs_age_hours=obs_age,
        stale_after_hours=stale_after_hours,
        obs_date=rec.observation_date,
        settled_through_date=settled_through_date,
    )
    return rec


def _as_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # treat NaN as missing
    return None if f != f else f


def _obs_age_hours(obs_date: str, now_jst: datetime) -> Optional[float]:
    """Age (hours) of a YYYY-MM-DD daily bar, measured from end-of-that-day."""
    try:
        d = datetime.strptime(obs_date, "%Y-%m-%d").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None
    end_of_day = d + timedelta(days=1)
    return max(0.0, (now_jst - end_of_day).total_seconds() / 3600.0)


# --------------------------------------------------------------------------- #
# Live fetch (Yahoo Finance via yfinance)
# --------------------------------------------------------------------------- #

def fetch_live(inst: Instrument) -> FxRecord:
    """Fetch one instrument from Yahoo Finance. Never raises."""
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    stale_after = float(os.environ.get("FX_STALE_AFTER_HOURS", "12"))
    settled = settled_through(now_jst)
    try:
        import yfinance as yf

        ticker = yf.Ticker(inst.yahoo)
        hist = ticker.history(period="7d", interval="1d")
        bars: List[dict] = []
        for idx, row in hist.iterrows():
            bars.append({
                "date": idx.strftime("%Y-%m-%d"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "volume": row.get("Volume"),
            })
        open_interest = None
        if inst.kind == "future":
            try:
                open_interest = ticker.info.get("openInterest")
            except Exception:  # noqa: BLE001 - OI is best-effort
                open_interest = None
        return build_record(inst, bars, open_interest, now_utc, now_jst,
                             stale_after, settled)
    except Exception as exc:  # noqa: BLE001 - requirement: never stop the run
        rec = FxRecord(symbol=inst.symbol, name=inst.name,
                       fetched_at_utc=_utc_now_iso(now_utc),
                       data_status=STATUS_ERROR)
        rec.error = f"{type(exc).__name__}: {exc}"
        return rec


def fetch_all(instruments: Optional[List[Instrument]] = None) -> List[FxRecord]:
    instruments = instruments or INSTRUMENTS
    return [fetch_live(inst) for inst in instruments]


# --------------------------------------------------------------------------- #
# Google Sheets upsert (generic, keyed on the first two columns)
# --------------------------------------------------------------------------- #

def upsert_rows(existing: List[List[str]],
                records: Sequence[FxRecord]) -> List[List[str]]:
    """Pure upsert: overwrite on (symbol, observation_date), else append."""
    header = list(COLUMNS)
    sym_i = COLUMNS.index("symbol")
    date_i = COLUMNS.index("observation_date")
    if existing and existing[0] == header:
        body = existing[1:]
    else:
        body = [r for r in existing if r and r[0] != COLUMNS[0]]

    index: Dict[tuple, int] = {}
    for i, row in enumerate(body):
        if len(row) > date_i:
            index[(row[sym_i], row[date_i])] = i

    for rec in records:
        row = rec.to_row()
        key = (rec.symbol, rec.observation_date)
        if key in index:
            body[index[key]] = row
        else:
            index[key] = len(body)
            body.append(row)

    return [header] + body


class SheetsWriter:
    def __init__(self, spreadsheet_id=None, worksheet_name=None):
        self.spreadsheet_id = spreadsheet_id or os.environ.get("GOOGLE_SHEETS_ID")
        self.worksheet_name = (
            worksheet_name
            or os.environ.get("GOOGLE_SHEETS_WORKSHEET_FX", "fx_evening")
        )

    def _open_worksheet(self):
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        inline = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if inline:
            creds = Credentials.from_service_account_info(
                json.loads(inline), scopes=scopes)
        else:
            path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if not path:
                raise RuntimeError(
                    "set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS")
            creds = Credentials.from_service_account_file(path, scopes=scopes)
        if not self.spreadsheet_id:
            raise RuntimeError("GOOGLE_SHEETS_ID is not set")

        sheet = gspread.authorize(creds).open_by_key(self.spreadsheet_id)
        try:
            return sheet.worksheet(self.worksheet_name)
        except gspread.WorksheetNotFound:
            return sheet.add_worksheet(
                title=self.worksheet_name, rows=200, cols=len(COLUMNS))

    def write(self, records: Sequence[FxRecord]) -> List[List[str]]:
        ws = self._open_worksheet()
        grid = upsert_rows(ws.get_all_values(), records)
        ws.clear()
        ws.update(range_name="A1", values=grid)
        return grid


def run() -> List[FxRecord]:
    """Live entry point: fetch all instruments and upsert into Google Sheets."""
    try:
        from envload import load_default_env
        load_default_env()
    except Exception:  # noqa: BLE001 - .env is optional
        pass
    records = fetch_all()
    counts: Dict[str, int] = {}
    for r in records:
        counts[r.data_status] = counts.get(r.data_status, 0) + 1
    SheetsWriter().write(records)
    print(f"[fx_fetcher] upserted {len(records)} instruments: {counts}")
    for r in records:
        if r.data_status == STATUS_ERROR:
            print(f"  - {r.symbol}: {r.error}")
    return records


# --------------------------------------------------------------------------- #
# Tests (network-free)
# --------------------------------------------------------------------------- #

def run_tests() -> None:
    now_utc = datetime(2026, 6, 29, 7, 20, tzinfo=timezone.utc)   # 16:20 JST
    now_jst = now_utc.astimezone(JST)

    # 1. change computation -------------------------------------------------
    assert compute_changes(157.5, 157.0) == (0.5, round(0.5 / 157.0 * 100, 6))
    assert compute_changes(1.0, None) == (None, None)
    assert compute_changes(1.0, 0.0) == (1.0, None)

    # 2. status decisions ---------------------------------------------------
    assert decide_status("fx", True, 1.0, 12) == STATUS_OK
    assert decide_status("fx", False, 1.0, 12) == STATUS_ERROR
    assert decide_status("fx", True, 30.0, 12) == STATUS_STALE
    # future, latest bar newer than last settled session -> preliminary
    assert decide_status("future", True, 1.0, 48,
                         obs_date="2026-06-29",
                         settled_through_date="2026-06-27") == STATUS_PRELIMINARY
    # future, latest bar within settled range -> final
    assert decide_status("future", True, 1.0, 48,
                         obs_date="2026-06-26",
                         settled_through_date="2026-06-27") == STATUS_FINAL

    # 3. settled_through timing --------------------------------------------
    morning = datetime(2026, 6, 29, 8, 0, tzinfo=JST)   # before publish hour
    evening = datetime(2026, 6, 29, 23, 0, tzinfo=JST)  # after publish hour
    assert settled_through(morning) == "2026-06-27"
    assert settled_through(evening) == "2026-06-28"

    # 4. build_record happy path (spot FX) ---------------------------------
    fx = Instrument("USDJPY", "USD/JPY", "JPY=X", "fx")
    bars = [
        {"date": "2026-06-26", "high": 157.2, "low": 156.4, "close": 157.0, "volume": 0},
        {"date": "2026-06-29", "high": 158.1, "low": 157.0, "close": 157.8, "volume": 0},
    ]
    rec = build_record(fx, bars, None, now_utc, now_jst, 48, "2026-06-27")
    assert rec.data_status == STATUS_OK
    assert rec.last == 157.8 and rec.previous_close == 157.0
    assert rec.day_high == 158.1 and rec.day_low == 157.0
    assert rec.change_abs == 0.8
    assert rec.fetched_at_utc.endswith("Z")
    assert len(rec.to_row()) == len(COLUMNS)

    # 5. build_record for a future -> preliminary + OI ---------------------
    fut = Instrument("6J", "JPY future", "6J=F", "future")
    fbars = [
        {"date": "2026-06-26", "high": 0.00640, "low": 0.00631, "close": 0.00637, "volume": 120000},
        {"date": "2026-06-29", "high": 0.00642, "low": 0.00636, "close": 0.00639, "volume": 95000},
    ]
    frec = build_record(fut, fbars, 250000, now_utc, now_jst, 48, "2026-06-27")
    assert frec.data_status == STATUS_PRELIMINARY  # 06-29 > settled 06-27
    assert frec.open_interest == 250000.0
    assert frec.volume == 95000.0

    # 6. NaN / missing handling --------------------------------------------
    nan = float("nan")
    assert _as_float(nan) is None and _as_float("") is None and _as_float("1.5") == 1.5
    bad = build_record(fx, [{"date": "2026-06-29", "close": None}], None,
                       now_utc, now_jst, 48, "2026-06-27")
    assert bad.data_status == STATUS_ERROR

    # 7. empty bars -> error, never raises ---------------------------------
    assert build_record(fx, [], None, now_utc, now_jst, 48,
                        "2026-06-27").data_status == STATUS_ERROR

    # 8. upsert: append then overwrite on (symbol, observation_date) --------
    grid = upsert_rows([list(COLUMNS)], [rec])
    assert len(grid) == 2 and grid[1][0] == "USDJPY"
    rec2 = build_record(fx, [
        {"date": "2026-06-26", "high": 157.2, "low": 156.4, "close": 157.0, "volume": 0},
        {"date": "2026-06-29", "high": 158.5, "low": 157.0, "close": 158.3, "volume": 0},
    ], None, now_utc, now_jst, 48, "2026-06-27")
    grid = upsert_rows(grid, [rec2])
    assert len(grid) == 2, "same (symbol, date) must overwrite"
    assert grid[1][3] == repr(158.3)

    print("All fx_fetcher tests passed.")


if __name__ == "__main__":
    import sys

    if "--run" in sys.argv:
        run()
    else:
        run_tests()
