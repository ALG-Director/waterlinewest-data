#!/usr/bin/env python3
"""
Phase 2B: Update the Lake Powell pool elevation value in docs/snowpack-status.json.

Source:
  U.S. Bureau of Reclamation — Upper Colorado HydroData portal
  Reservoir: 919 — Lake Powell (Glen Canyon Dam)
  Datatype: 49 — Pool Elevation, feet above mean sea level

This mirrors scripts/update_lees_ferry.py. It updates only the
`powell_elevation` indicator and leaves every other value untouched, so the
manually curated indicators stay exactly as the editor left them.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# Lake Powell daily pool elevation (period of record), Reclamation Upper Colorado.
USBR_ELEVATION_URL = (
    "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/49.json"
)
STATUS_PATH = Path("docs/snowpack-status.json")
AZ = ZoneInfo("America/Phoenix")


def pretty_datetime(dt: datetime) -> str:
    """Return a readable Arizona-time timestamp without leading-zero day/hour."""
    dt_az = dt.astimezone(AZ)
    month = dt_az.strftime("%B")
    day = dt_az.day
    year = dt_az.year
    hour = dt_az.strftime("%I").lstrip("0") or "0"
    minute = dt_az.strftime("%M")
    ampm = dt_az.strftime("%p")
    return f"{month} {day}, {year}, {hour}:{minute} {ampm} Arizona time"


def pretty_date(d: datetime) -> str:
    """Return a readable date like 'May 27, 2026' (no leading-zero day)."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def load_json_from_url(url: str) -> dict:
    # A User-Agent header avoids occasional 403s from the Reclamation host.
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"USBR request failed with HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"USBR request failed: {exc.reason}") from exc


def extract_latest_elevation(payload: dict) -> tuple[float, datetime]:
    """Extract the most recent non-null pool elevation and its date.

    The feed looks like:
        {"columns": ["datetime", "pool elevation"],
         "data": [["1963-12-28", 3409.0], ..., ["2026-05-27", 3526.24]]}
    Rows are in chronological order, so we walk backward to the last real value.
    """
    rows = payload.get("data", [])
    if not rows:
        raise RuntimeError("USBR response did not include any data rows.")

    for row in reversed(rows):
        if not row or len(row) < 2:
            continue
        raw_date, raw_value = row[0], row[1]
        if raw_value is None or raw_date is None:
            continue
        try:
            elevation_ft = float(raw_value)
            observed_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        return elevation_ft, observed_date

    raise RuntimeError("USBR response had no usable elevation values.")


def main() -> int:
    if not STATUS_PATH.exists():
        raise RuntimeError(f"Missing status file: {STATUS_PATH}")

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    payload = load_json_from_url(USBR_ELEVATION_URL)
    elevation_ft, observed_date = extract_latest_elevation(payload)

    now_az = datetime.now(tz=AZ)
    display_value = f"{elevation_ft:,.2f}"
    observed_display = "Observed " + pretty_date(observed_date)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)
    status.setdefault("automation", {})
    status["automation"].update({
        "phase": "2B",
        "script_powell": "scripts/update_lake_powell.py",
        "powell_source": "USBR Upper Colorado HydroData (reservoir 919, datatype 49)",
        "powell_last_run_display": pretty_datetime(now_az),
    })

    indicators = status.setdefault("indicators", {})
    powell = indicators.setdefault("powell_elevation", {})
    powell.update({
        "label": "Lake Powell Elevation",
        "display_value": display_value,
        "note": "ft above sea level",
        "timestamp": observed_display,
        "source_name": "U.S. Bureau of Reclamation",
        "source_url": "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/dashboard.html",
        "api_url": USBR_ELEVATION_URL,
        "update_cadence": "Daily",
        "provisional": True,
        "usbr_site_id": "919",
        "usbr_datatype_id": "49",
        "usbr_observed_date": observed_date.date().isoformat(),
    })

    STATUS_PATH.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Updated Lake Powell elevation to {display_value} ft — {observed_display}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
