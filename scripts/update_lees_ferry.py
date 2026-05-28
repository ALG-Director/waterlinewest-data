#!/usr/bin/env python3
"""
Phase 2A: Update the Lees Ferry flow value in docs/snowpack-status.json.

Source:
  USGS Instantaneous Values service
  Site: 09380000 — Colorado River at Lees Ferry, AZ
  Parameter: 00060 — Discharge, cubic feet per second

This script intentionally updates only one indicator first. The rest of the
Snowpack JSON can remain manually curated until each source is tested.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

USGS_IV_URL = (
    "https://waterservices.usgs.gov/nwis/iv/"
    "?format=json&sites=09380000&parameterCd=00060&siteStatus=all"
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


def load_json_from_url(url: str) -> dict:
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"USGS request failed with HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"USGS request failed: {exc.reason}") from exc


def extract_latest_discharge(payload: dict) -> tuple[float, datetime]:
    """Extract the latest discharge value and timestamp from USGS IV JSON."""
    series = payload.get("value", {}).get("timeSeries", [])
    if not series:
        raise RuntimeError("USGS response did not include any timeSeries records.")

    chosen = None
    for item in series:
        variable_codes = item.get("variable", {}).get("variableCode", [])
        if any(code.get("value") == "00060" for code in variable_codes):
            chosen = item
            break

    if chosen is None:
        chosen = series[0]

    values_groups = chosen.get("values", [])
    if not values_groups or not values_groups[0].get("value"):
        raise RuntimeError("USGS response did not include discharge values.")

    latest = values_groups[0]["value"][-1]
    raw_value = latest.get("value")
    raw_time = latest.get("dateTime")
    if raw_value is None or raw_time is None:
        raise RuntimeError("Latest USGS value was missing value or dateTime.")

    try:
        discharge_cfs = float(raw_value)
        observed_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Could not parse USGS latest value/time: {latest}") from exc

    return discharge_cfs, observed_dt


def main() -> int:
    if not STATUS_PATH.exists():
        raise RuntimeError(f"Missing status file: {STATUS_PATH}")

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    payload = load_json_from_url(USGS_IV_URL)
    discharge_cfs, observed_dt = extract_latest_discharge(payload)

    now_az = datetime.now(tz=AZ)
    display_value = f"{discharge_cfs:,.0f}"
    observed_display = "Observed " + pretty_datetime(observed_dt)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)
    status.setdefault("automation", {})
    status["automation"].update({
        "phase": "2A",
        "updated_indicator": "lees_ferry_flow",
        "script": "scripts/update_lees_ferry.py",
        "source": "USGS Instantaneous Values service",
        "last_run_display": pretty_datetime(now_az),
    })

    indicators = status.setdefault("indicators", {})
    lees = indicators.setdefault("lees_ferry_flow", {})
    lees.update({
        "label": "Lees Ferry Flow",
        "display_value": display_value,
        "note": "cfs",
        "timestamp": observed_display,
        "source_name": "USGS Water Data",
        "source_url": "https://waterdata.usgs.gov/monitoring-location/09380000/",
        "api_url": USGS_IV_URL,
        "update_cadence": "Near real time where available",
        "provisional": True,
        "usgs_site_no": "09380000",
        "usgs_parameter_cd": "00060",
        "usgs_observed_datetime": observed_dt.isoformat(),
    })

    status["source_line"] = (
        "Sources: NRCS/CAP snowpack reporting · NOAA CBRFC · "
        "U.S. Bureau of Reclamation · USGS Water Data. "
        "Lees Ferry flow updates automatically from USGS; other values remain curated in Phase 2A. "
        "Provisional values may be revised."
    )

    STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated Lees Ferry flow to {display_value} cfs — {observed_display}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
