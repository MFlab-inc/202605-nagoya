"""FRED → Google Sheets fetcher for the FX day-trade morning rate distribution.

This module covers the *data-source layer* of the JST 10:30 distribution
(stage 1): it pulls **previous-day confirmed** macro/rates data from the FRED
API and upserts it into a Google Sheet. It does **not** use any intraday /
current-day value — FRED only publishes confirmed observations, which matches
the "前日確定データのみ使用" requirement.

Series collected (FX day-trade rate panel):

    US2Y          -> DGS2          2-Year Treasury constant maturity yield
    US5Y          -> DGS5          5-Year Treasury constant maturity yield
    US10Y         -> DGS10         10-Year Treasury constant maturity yield
    US30Y         -> DGS30         30-Year Treasury constant maturity yield
    10Y real yld  -> DFII10        10-Year TIPS (real) yield
    HY OAS        -> BAMLH0A0HYM2  ICE BofA US High Yield OAS
    SOFR          -> SOFR          Secured Overnight Financing Rate
    IORB          -> IORB          Interest on Reserve Balances rate
    RRP           -> RRPONTSYD     ON RRP: total amount accepted (USD bn)
    準備金 / Reserves -> WRESBAL    Reserve balances at the Fed (weekly, USD bn)

For each series the following row is written (one row per series_id +
observation_date), matching the requested schema:

    series_id, observation_date, value, previous_value,
    change_abs, change_pct, source, fetched_at_utc, data_status

Behaviour requirements implemented:
  * Append to Google Sheets, **upsert** keyed on (series_id, observation_date).
  * On any fetch/parse failure the run does NOT stop; the affected series is
    written with data_status = "error" (value/previous left blank).
  * The FRED API key is read from the FRED_API_KEY environment variable.
  * A lightweight, network-free self-test is provided (run_tests / __main__).

Environment variables
---------------------
  FRED_API_KEY                  (required for live runs) FRED API key
  GOOGLE_SHEETS_ID              (required for live runs) target spreadsheet id
  GOOGLE_SHEETS_WORKSHEET       worksheet/tab name (default: "fred_daily")
  GOOGLE_SERVICE_ACCOUNT_JSON   inline service-account JSON, OR
  GOOGLE_APPLICATION_CREDENTIALS  path to a service-account JSON file
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

try:  # requests is only needed for live fetches; tests run without it.
    import requests
except Exception:  # pragma: no cover - optional at import time
    requests = None  # type: ignore


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
SOURCE = "FRED"

# Display label -> FRED series_id. Edit here to swap a series.
SERIES_MAP: Dict[str, str] = {
    "US2Y": "DGS2",
    "US5Y": "DGS5",
    "US10Y": "DGS10",
    "US30Y": "DGS30",
    "US10Y_REAL": "DFII10",
    "HY_OAS": "BAMLH0A0HYM2",
    "SOFR": "SOFR",
    "IORB": "IORB",
    "RRP": "RRPONTSYD",
    "RESERVES": "WRESBAL",
}

# The header row written to (and expected in) the sheet. Order matters.
COLUMNS: Sequence[str] = (
    "series_id",
    "observation_date",
    "value",
    "previous_value",
    "change_abs",
    "change_pct",
    "source",
    "fetched_at_utc",
    "data_status",
)

# data_status values
STATUS_OK = "ok"
STATUS_ERROR = "error"


# --------------------------------------------------------------------------- #
# Record model
# --------------------------------------------------------------------------- #

@dataclass
class RateRecord:
    series_id: str
    observation_date: str = ""           # YYYY-MM-DD of the latest confirmed obs
    value: Optional[float] = None
    previous_value: Optional[float] = None
    change_abs: Optional[float] = None
    change_pct: Optional[float] = None
    source: str = SOURCE
    fetched_at_utc: str = ""
    data_status: str = STATUS_OK
    error: str = field(default="", repr=False)  # internal note, not a sheet column

    def to_row(self) -> List[str]:
        """Render the record as a list of strings in COLUMNS order."""
        d = asdict(self)

        def fmt(v: Optional[float]) -> str:
            return "" if v is None else repr(v) if isinstance(v, float) else str(v)

        return [fmt(d[col]) for col in COLUMNS]

    @property
    def key(self) -> tuple:
        return (self.series_id, self.observation_date)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_changes(value: float, previous: Optional[float]) -> tuple:
    """Return (change_abs, change_pct). change_pct is None if previous == 0."""
    if previous is None:
        return None, None
    change_abs = round(value - previous, 6)
    if previous == 0:
        return change_abs, None
    change_pct = round((value - previous) / abs(previous) * 100.0, 6)
    return change_abs, change_pct


# --------------------------------------------------------------------------- #
# FRED client
# --------------------------------------------------------------------------- #

class FredClient:
    """Thin FRED observations client. Returns parsed observation dicts."""

    def __init__(self, api_key: Optional[str] = None, timeout: int = 20):
        self.api_key = api_key or os.environ.get("FRED_API_KEY")
        self.timeout = timeout

    def _get_observations(self, series_id: str, limit: int = 5) -> List[dict]:
        """Fetch the most recent `limit` observations (newest first).

        Raises on transport / HTTP / API errors so the caller can mark the
        series as data_status = error.
        """
        if requests is None:
            raise RuntimeError("the 'requests' package is required for live fetches")
        if not self.api_key:
            raise RuntimeError("FRED_API_KEY is not set")

        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",   # newest first
            "limit": limit,
        }
        resp = requests.get(FRED_BASE_URL, params=params, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        if "error_message" in payload:
            raise RuntimeError(f"FRED error: {payload['error_message']}")
        return payload.get("observations", [])

    def fetch_record(self, series_id: str, limit: int = 10) -> RateRecord:
        """Fetch one series and build a RateRecord.

        Never raises: on any failure it returns a record with
        data_status = "error". A FRED missing value is encoded as ".".
        """
        rec = RateRecord(series_id=series_id, fetched_at_utc=_utc_now_iso())
        try:
            observations = self._get_observations(series_id, limit=limit)
            confirmed = parse_confirmed_observations(observations)
            if not confirmed:
                rec.data_status = STATUS_ERROR
                rec.error = "no valid (non-missing) observations returned"
                return rec

            latest_date, latest_value = confirmed[0]
            previous_value = confirmed[1][1] if len(confirmed) > 1 else None

            rec.observation_date = latest_date
            rec.value = latest_value
            rec.previous_value = previous_value
            rec.change_abs, rec.change_pct = _compute_changes(latest_value, previous_value)
            rec.data_status = STATUS_OK
        except Exception as exc:  # noqa: BLE001 - requirement: never stop the run
            rec.data_status = STATUS_ERROR
            rec.error = f"{type(exc).__name__}: {exc}"
        return rec


def parse_confirmed_observations(observations: List[dict]) -> List[tuple]:
    """Normalise FRED observations -> [(date, value), ...] newest first.

    FRED encodes a missing value as the string ".". Those are dropped, so the
    first element is always the latest *confirmed* (previous-day or earlier)
    value and the second is the prior confirmed value used for the delta.
    Input may be in any sort order; output is sorted newest-first.
    """
    parsed: List[tuple] = []
    for obs in observations:
        raw = obs.get("value", ".")
        date = obs.get("date", "")
        if raw is None or str(raw).strip() in (".", ""):
            continue
        try:
            parsed.append((date, float(raw)))
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda dv: dv[0], reverse=True)
    return parsed


def fetch_all(series_map: Optional[Dict[str, str]] = None,
              client: Optional[FredClient] = None) -> List[RateRecord]:
    """Fetch every configured series. Failures become data_status = error."""
    series_map = series_map or SERIES_MAP
    client = client or FredClient()
    records: List[RateRecord] = []
    for label, series_id in series_map.items():
        rec = client.fetch_record(series_id)
        records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# Google Sheets upsert
# --------------------------------------------------------------------------- #

def upsert_rows(existing: List[List[str]],
                records: Sequence[RateRecord]) -> List[List[str]]:
    """Pure upsert logic (no I/O), so it is unit-testable.

    `existing` is the full sheet grid including the header row. Rows are
    matched/overwritten on (series_id, observation_date); new keys are
    appended. Returns the new full grid (header + data rows).
    """
    header = list(COLUMNS)
    body = existing[1:] if existing and existing[0] == header else \
        [r for r in existing if r and r[0] != COLUMNS[0]]

    index: Dict[tuple, int] = {}
    for i, row in enumerate(body):
        if len(row) >= 2:
            index[(row[0], row[1])] = i

    for rec in records:
        row = rec.to_row()
        key = (rec.series_id, rec.observation_date)
        if key in index:
            body[index[key]] = row       # overwrite duplicate
        else:
            index[key] = len(body)
            body.append(row)

    return [header] + body


class SheetsWriter:
    """Writes RateRecords to a Google Sheet with (series_id, date) upsert."""

    def __init__(self,
                 spreadsheet_id: Optional[str] = None,
                 worksheet_name: Optional[str] = None):
        self.spreadsheet_id = spreadsheet_id or os.environ.get("GOOGLE_SHEETS_ID")
        self.worksheet_name = (
            worksheet_name
            or os.environ.get("GOOGLE_SHEETS_WORKSHEET", "fred_daily")
        )

    def _open_worksheet(self):
        import gspread  # imported lazily so tests don't need the dependency
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        inline = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if inline:
            info = json.loads(inline)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
        else:
            path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if not path:
                raise RuntimeError(
                    "set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS"
                )
            creds = Credentials.from_service_account_file(path, scopes=scopes)

        if not self.spreadsheet_id:
            raise RuntimeError("GOOGLE_SHEETS_ID is not set")

        client = gspread.authorize(creds)
        sheet = client.open_by_key(self.spreadsheet_id)
        try:
            return sheet.worksheet(self.worksheet_name)
        except gspread.WorksheetNotFound:
            return sheet.add_worksheet(
                title=self.worksheet_name, rows=100, cols=len(COLUMNS)
            )

    def write(self, records: Sequence[RateRecord]) -> List[List[str]]:
        """Upsert records into the worksheet and return the new grid."""
        ws = self._open_worksheet()
        existing = ws.get_all_values()
        grid = upsert_rows(existing, records)
        ws.clear()
        ws.update(range_name="A1", values=grid)
        return grid


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run() -> List[RateRecord]:
    """Live entry point: fetch all series and upsert into Google Sheets."""
    records = fetch_all()
    ok = sum(1 for r in records if r.data_status == STATUS_OK)
    err = [r for r in records if r.data_status == STATUS_ERROR]
    SheetsWriter().write(records)
    print(f"[fred_fetcher] upserted {len(records)} series (ok={ok}, error={len(err)})")
    for r in err:
        print(f"  - {r.series_id}: {r.error}")
    return records


# --------------------------------------------------------------------------- #
# Tests (network-free)
# --------------------------------------------------------------------------- #

class _FakeFredClient(FredClient):
    """A FredClient stub returning canned observations for tests."""

    def __init__(self, table: Dict[str, List[dict]]):
        super().__init__(api_key="test")
        self._table = table

    def _get_observations(self, series_id: str, limit: int = 5) -> List[dict]:
        if series_id not in self._table:
            raise RuntimeError("simulated network/API failure")
        return self._table[series_id]


def run_tests() -> None:
    """Lightweight self-test. Raises AssertionError on failure."""

    # 1. change computation -------------------------------------------------
    assert _compute_changes(4.50, 4.40) == (0.1, round(0.1 / 4.40 * 100, 6))
    assert _compute_changes(4.50, None) == (None, None)
    assert _compute_changes(1.0, 0.0) == (1.0, None)  # no divide-by-zero

    # 2. missing-value parsing (FRED "." dropped, newest first) -------------
    obs = [
        {"date": "2026-06-26", "value": "."},
        {"date": "2026-06-25", "value": "4.30"},
        {"date": "2026-06-24", "value": "4.25"},
    ]
    confirmed = parse_confirmed_observations(obs)
    assert confirmed == [("2026-06-25", 4.30), ("2026-06-24", 4.25)], confirmed

    # 3. successful fetch_record builds a full row --------------------------
    client = _FakeFredClient({
        "DGS10": [
            {"date": "2026-06-26", "value": "."},          # missing -> ignored
            {"date": "2026-06-25", "value": "4.30"},        # latest confirmed
            {"date": "2026-06-24", "value": "4.25"},        # previous
        ],
    })
    rec = client.fetch_record("DGS10")
    assert rec.data_status == STATUS_OK
    assert rec.observation_date == "2026-06-25"
    assert rec.value == 4.30 and rec.previous_value == 4.25
    assert rec.change_abs == 0.05
    assert rec.source == SOURCE and rec.fetched_at_utc.endswith("Z")
    assert rec.to_row()[0] == "DGS10"
    assert len(rec.to_row()) == len(COLUMNS)

    # 4. failure path never raises and is marked error ----------------------
    rec_err = client.fetch_record("DOES_NOT_EXIST")
    assert rec_err.data_status == STATUS_ERROR
    assert rec_err.value is None and rec_err.previous_value is None
    assert rec_err.error  # populated with the exception text

    # 5. all-missing observations -> error ----------------------------------
    client2 = _FakeFredClient({"X": [{"date": "2026-06-25", "value": "."}]})
    assert client2.fetch_record("X").data_status == STATUS_ERROR

    # 6. upsert: insert then overwrite on (series_id, observation_date) ------
    grid = [list(COLUMNS)]
    r1 = RateRecord("DGS10", "2026-06-25", 4.30, 4.25, 0.05, 1.18,
                    SOURCE, "2026-06-26T01:30:00Z", STATUS_OK)
    grid = upsert_rows(grid, [r1])
    assert len(grid) == 2 and grid[1][0] == "DGS10"

    # same key, new value -> overwrite (no new row)
    r1b = RateRecord("DGS10", "2026-06-25", 4.31, 4.25, 0.06, 1.41,
                     SOURCE, "2026-06-26T01:35:00Z", STATUS_OK)
    grid = upsert_rows(grid, [r1b])
    assert len(grid) == 2, "duplicate key must overwrite, not append"
    assert grid[1][2] == repr(4.31)

    # different date -> append
    r2 = RateRecord("DGS10", "2026-06-26", 4.35, 4.31, 0.04, 0.93,
                    SOURCE, "2026-06-27T01:30:00Z", STATUS_OK)
    grid = upsert_rows(grid, [r2])
    assert len(grid) == 3, "new observation_date must append"

    # 7. fetch_all keeps going past a failing series ------------------------
    multi = _FakeFredClient({
        "DGS2": [{"date": "2026-06-25", "value": "4.10"},
                 {"date": "2026-06-24", "value": "4.05"}],
        # "MISSING" intentionally absent -> error, must not abort the loop
    })
    recs = fetch_all({"US2Y": "DGS2", "BAD": "MISSING"}, client=multi)
    assert len(recs) == 2
    by_id = {r.series_id: r for r in recs}
    assert by_id["DGS2"].data_status == STATUS_OK
    assert by_id["MISSING"].data_status == STATUS_ERROR

    print("All fred_fetcher tests passed.")


if __name__ == "__main__":
    import sys

    if "--run" in sys.argv:
        run()
    else:
        run_tests()
