#!/usr/bin/env python3
"""
Phase 2B: Update the Lake Powell pool elevation value in docs/snowpack-status.json.

Source:
  U.S. Bureau of Reclamation — Upper Colorado HydroData portal
  Reservoir: 919 — Lake Powell (Glen Canyon Dam)
  Datatype: 49 — Pool Elevation, feet above mean sea level

Hardened behavior:
  * Retries the Reclamation feed a few times before giving up.
  * If Reclamation is unreachable after the retries, this script SOFT-FAILS:
    it prints a warning, leaves the existing Powell value untouched, and exits
    0 (success). That way a slow Reclamation server never stops the workflow,
    so the Lees Ferry update and the commit step still run normally.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

USBR_ELEVATION_URL = (
    "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/49.json"
)
STATUS_PATH = Path("docs/snowpack-status.json")
AZ = ZoneInfo("America/Phoenix")


def pretty_datetime(dt: datetime) -> str:
    dt_az = dt.astimezone(AZ)
    month = dt_az.strftime("%B")
    day = dt_az.day
    year = dt_az.year
    hour = dt_az.strftime("%I").lstrip("0") or "0"
    minute = dt_az.strftime("%M")
    ampm = dt_az.strftime("%p")
    return f"{month} {day}, {year}, {hour}:{minute} {ampm} Arizona time"


def pretty_date(d: datetime) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def load_json_from_url(url: str, attempts: int = 4, timeout: int = 60) -> dict:
    """Fetch JSON with retries. Raises only after all attempts fail."""
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = RuntimeError(
                f"USBR request failed with HTTP {exc.code}: {exc.reason}"
            )
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"USBR request failed: {reason}")
        if attempt < attempts:
            time.sleep(5 * attempt)  # 5s, 10s, 15s between tries
    raise last_error


def extract_latest_elevation(payload: dict):
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

    # Soft-fail boundary: if Reclamation can't be reached after retries, warn
    # and exit successfully so the rest of the workflow (commit) still runs.
    try:
        payload = load_json_from_url(USBR_ELEVATION_URL)
        elevation_ft, observed_date = extract_latest_elevation(payload)
    except Exception as exc:
        print(
            f"WARNING: Lake Powell update skipped this run ({exc}). "
            "Keeping the previous value.",
            file=sys.stderr,
        )
        return 0

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
