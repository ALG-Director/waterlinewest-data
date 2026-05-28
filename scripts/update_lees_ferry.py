#!/usr/bin/env python3
"""
WaterLineWest Phase 2B

Updates docs/snowpack-status.json with:
- Lees Ferry flow from USGS site 09380000, parameter 00060
- Lake Powell elevation, storage, inflow, and total outflow/release from
  Bureau of Reclamation Lake Powell / Glen Canyon daily time-series JSON.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

STATUS_PATH = Path("docs/snowpack-status.json")
AZ = ZoneInfo("America/Phoenix")

USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/?" + urlencode(
    {
        "format": "json",
        "sites": "09380000",
        "parameterCd": "00060",
        "siteStatus": "all",
    }
)

POWELL_DASHBOARD_URL = "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/dashboard.html"

POWELL_SERIES = {
    "lake_powell_elevation": {
        "label": "Lake Powell Elevation",
        "json_url": "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/49.json",
        "source_url": "https://data.usbr.gov/catalog/2362/item/508",
        "source_name": "Bureau of Reclamation RISE / Upper Colorado Hydrologic Database",
        "note": "ft elevation",
        "display_kind": "feet_2",
        "raw_key": "elevation_ft",
        "rise_item_id": "508",
    },
    "lake_powell_storage": {
        "label": "Lake Powell Storage",
        "json_url": "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/17.json",
        "source_url": "https://data.usbr.gov/catalog/2362/item/509",
        "source_name": "Bureau of Reclamation RISE / Upper Colorado Hydrologic Database",
        "note": "million acre-feet",
        "display_kind": "acre_feet_to_maf",
        "raw_key": "storage_acre_feet",
        "rise_item_id": "509",
    },
    "lake_powell_inflow": {
        "label": "Lake Powell Inflow",
        "json_url": "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/29.json",
        "source_url": "https://data.usbr.gov/catalog/2362/item/511",
        "source_name": "Bureau of Reclamation RISE / Upper Colorado Hydrologic Database",
        "note": "cfs",
        "display_kind": "whole_number",
        "raw_key": "inflow_cfs",
        "rise_item_id": "511",
    },
    "lake_powell_outflow": {
        "label": "Lake Powell Outflow",
        "json_url": "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/42.json",
        "source_url": "https://data.usbr.gov/catalog/2362/item/4315",
        "source_name": "Bureau of Reclamation RISE / Upper Colorado Hydrologic Database",
        "note": "cfs total release",
        "display_kind": "whole_number",
        "raw_key": "outflow_cfs",
        "rise_item_id": "4315",
    },
}


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


def pretty_date(dt: datetime) -> str:
    dt_az = dt.astimezone(AZ)
    return f"{dt_az.strftime('%B')} {dt_az.day}, {dt_az.year}"


def load_json_from_url(url: str) -> Any:
    req = Request(
        url,
        headers={
            "User-Agent": "WaterLineWest data updater (GitHub Actions)",
            "Accept": "application/json, application/vnd.api+json;q=0.9, */*;q=0.8",
        },
    )
    try:
        with urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Request failed with HTTP {exc.code}: {exc.reason} — {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Request failed: {exc.reason} — {url}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Response was not valid JSON — {url}") from exc


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned in {"", "--", "NA", "N/A", "null"}:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None

    if not math.isfinite(number):
        return None
    return number


def parse_date_like(value: Any) -> datetime | None:
    """Parse common date formats plus JavaScript epoch-millisecond values."""
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000, tz=AZ)
        if 1_000_000_000 <= number <= 10_000_000_000:
            return datetime.fromtimestamp(number, tz=AZ)
        return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt if dt.tzinfo else dt.replace(tzinfo=AZ)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%d-%b-%Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=AZ)
        except ValueError:
            continue

    return None


def collect_dated_values(node: Any, out: list[tuple[datetime, float]]) -> None:
    """
    Recursively collect likely (date, value) pairs from flexible JSON shapes.

    This handles common API/table/chart structures such as:
    - {"date": "2026-05-27", "value": 3527.99}
    - {"x": 1779859200000, "y": 3527.99}
    - ["2026-05-27", 3527.99]
    - [1779859200000, 3527.99]
    """
    if isinstance(node, dict):
        date_values: list[datetime] = []
        number_values: list[float] = []

        for key, value in node.items():
            key_l = str(key).lower()
            if any(token in key_l for token in ("date", "time", "timestamp", "datetime")) or key_l in {"x", "period"}:
                parsed_dt = parse_date_like(value)
                if parsed_dt:
                    date_values.append(parsed_dt)
            elif key_l in {"value", "val", "y"} or any(
                token in key_l
                for token in (
                    "elevation",
                    "storage",
                    "release",
                    "outflow",
                    "inflow",
                    "content",
                )
            ):
                parsed_number = parse_number(value)
                if parsed_number is not None:
                    number_values.append(parsed_number)

        for dt in date_values:
            for number in number_values:
                out.append((dt, number))

        for value in node.values():
            collect_dated_values(value, out)
        return

    if isinstance(node, list):
        if len(node) >= 2:
            dt = parse_date_like(node[0])
            number = parse_number(node[1])
            if dt and number is not None:
                out.append((dt, number))

            dt = parse_date_like(node[1])
            number = parse_number(node[0])
            if dt and number is not None:
                out.append((dt, number))

        for item in node:
            collect_dated_values(item, out)


def extract_latest_dated_value(payload: Any, label: str) -> tuple[float, datetime]:
    candidates: list[tuple[datetime, float]] = []
    collect_dated_values(payload, candidates)

    if not candidates:
        raise RuntimeError(f"Could not find any dated numeric values for {label}.")

    candidates.sort(key=lambda pair: pair[0])
    observed_dt, value = candidates[-1]
    return value, observed_dt


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

    discharge_cfs = parse_number(raw_value)
    observed_dt = parse_date_like(raw_time)

    if discharge_cfs is None or observed_dt is None:
        raise RuntimeError(f"Could not parse USGS latest value/time: {latest}")

    return discharge_cfs, observed_dt


def display_value(value: float, kind: str) -> str:
    if kind == "feet_2":
        return f"{value:,.2f}"
    if kind == "acre_feet_to_maf":
        return f"{value / 1_000_000:,.2f}"
    if kind == "whole_number":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def update_lees_ferry(status: dict) -> None:
    payload = load_json_from_url(USGS_IV_URL)
    discharge_cfs, observed_dt = extract_latest_discharge(payload)

    indicators = status.setdefault("indicators", {})
    lees = indicators.setdefault("lees_ferry_flow", {})
    lees.update(
        {
            "label": "Lees Ferry Flow",
            "display_value": f"{discharge_cfs:,.0f}",
            "note": "cfs",
            "timestamp": "Observed " + pretty_datetime(observed_dt),
            "source_name": "USGS Water Data",
            "source_url": "https://waterdata.usgs.gov/monitoring-location/09380000/",
            "api_url": USGS_IV_URL,
            "update_cadence": "Near real time where available",
            "provisional": True,
            "usgs_site_no": "09380000",
            "usgs_parameter_cd": "00060",
            "usgs_observed_datetime": observed_dt.isoformat(),
            "raw_value_cfs": discharge_cfs,
        }
    )

    print(f"Updated Lees Ferry flow to {discharge_cfs:,.0f} cfs — Observed {pretty_datetime(observed_dt)}")


def update_lake_powell(status: dict) -> None:
    indicators = status.setdefault("indicators", {})
    reservoirs = status.setdefault("reservoirs", {})
    lake_powell = reservoirs.setdefault("lake_powell", {})

    lake_powell.update(
        {
            "label": "Lake Powell / Glen Canyon Dam",
            "source_name": "Bureau of Reclamation RISE / Upper Colorado Hydrologic Database",
            "dashboard_url": POWELL_DASHBOARD_URL,
            "provisional": True,
        }
    )

    for indicator_id, config in POWELL_SERIES.items():
        payload = load_json_from_url(config["json_url"])
        value, observed_dt = extract_latest_dated_value(payload, config["label"])

        indicator = indicators.setdefault(indicator_id, {})
        indicator.update(
            {
                "label": config["label"],
                "display_value": display_value(value, config["display_kind"]),
                "note": config["note"],
                "timestamp": "Observed " + pretty_date(observed_dt),
                "source_name": config["source_name"],
                "source_url": config["source_url"],
                "api_url": config["json_url"],
                "dashboard_url": POWELL_DASHBOARD_URL,
                "update_cadence": "Daily where available",
                "provisional": True,
                "rise_catalog_record_id": "2362",
                "rise_item_id": config["rise_item_id"],
                "observed_datetime": observed_dt.isoformat(),
                "raw_value": value,
            }
        )

        lake_powell[config["raw_key"]] = value
        lake_powell[config["raw_key"] + "_display"] = indicator["display_value"]
        lake_powell[config["raw_key"] + "_observed"] = observed_dt.isoformat()

        print(f"Updated {config['label']} to {indicator['display_value']} {config['note']} — Observed {pretty_date(observed_dt)}")


def main() -> int:
    if not STATUS_PATH.exists():
        raise RuntimeError(f"Missing status file: {STATUS_PATH}")

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    now_az = datetime.now(tz=AZ)

    update_lees_ferry(status)
    update_lake_powell(status)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)

    status.setdefault("automation", {})
    status["automation"].update(
        {
            "phase": "2B",
            "updated_indicators": [
                "lees_ferry_flow",
                "lake_powell_elevation",
                "lake_powell_storage",
                "lake_powell_inflow",
                "lake_powell_outflow",
            ],
            "script": "scripts/update_lees_ferry.py",
            "sources": [
                "USGS Instantaneous Values service",
                "Bureau of Reclamation RISE / Upper Colorado Hydrologic Database",
            ],
            "last_run_display": pretty_datetime(now_az),
        }
    )

    status["source_line"] = (
        "Sources: NRCS/CAP snowpack reporting · NOAA CBRFC · "
        "U.S. Bureau of Reclamation RISE / Upper Colorado Hydrologic Database · "
        "USGS Water Data. Lees Ferry flow and Lake Powell daily reservoir indicators "
        "update automatically; other values remain curated until each source is tested. "
        "Provisional values may be revised."
    )

    STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Phase 2B update complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
