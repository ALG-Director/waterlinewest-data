#!/usr/bin/env python3
"""
DAILY UPDATER: publish Lake Mohave and Lake Havasu live elevations into
docs/snowpack-status.json. Sibling of update_lake_mead.py, but the source is the
USGS NWIS daily-values web service (the same service the tributary / Lees Ferry /
Grand Canyon updaters use), not USBR.

  Lake Mohave at Davis Dam     USGS 09422500  param 62614 -> "mohave_elevation"
  Lake Havasu near Parker Dam  USGS 09427500  param 62614 -> "havasu_elevation"

param 62614 = "Lake or reservoir water surface elevation above NGVD 1929, feet".

We pull the last ~45 days of daily values, take the latest as the current
reading and the preceding 7 points as the trend window (matching the
powell_elevation / mead_elevation blocks, so the module pills behave the same).

Soft-fail: any fetch/parse problem logs and leaves the prior value in place; the
script exits 0 so the rest of the workflow still commits whatever else succeeded.
The observed date is logged so a stale feed is obvious on the first run.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

STATUS_PATH = Path("docs/snowpack-status.json")
ARIZONA = ZoneInfo("America/Phoenix")

PARAM = "62614"        # lake/reservoir water surface elevation above NGVD 1929, ft
LOOKBACK_DAYS = 45
BAND_FT = 0.5          # within +/- this many feet of the 7-day avg reads as "steady"

# (status_key, usgs_site, label, dam)
RESERVOIRS = [
    ("mohave_elevation", "09422500", "Lake Mohave Elevation", "Davis Dam"),
    ("havasu_elevation", "09427500", "Lake Havasu Elevation", "Parker Dam"),
]


def api_url(site: str) -> str:
    end = datetime.now(ARIZONA).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return ("https://waterservices.usgs.gov/nwis/dv/?format=json"
            f"&sites={site}&parameterCd={PARAM}"
            f"&startDT={start.isoformat()}&endDT={end.isoformat()}")


def site_page(site: str) -> str:
    return f"https://waterdata.usgs.gov/monitoring-location/USGS-{site}/"


def fetch_series(url: str, attempts: int = 3) -> list:
    """Return ascending [(date, value), ...] from a USGS NWIS daily-values JSON.

    Shape: value.timeSeries[0].values[0].value[] = [{"value": "...", "dateTime": "..."}].
    Skips USGS no-data sentinels (-999999) and any non-numeric flags (e.g. 'Ice').
    """
    delay = 2.0
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WaterLineWest/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            series = payload.get("value", {}).get("timeSeries", [])
            if not series:
                return []
            raw_values = series[0].get("values", [{}])[0].get("value", [])
            out = []
            for v in raw_values:
                raw = v.get("value")
                day = str(v.get("dateTime", ""))[:10]
                if raw in (None, ""):
                    continue
                try:
                    fv = float(raw)
                except (TypeError, ValueError):
                    continue
                if fv <= -999998:   # USGS no-data sentinel
                    continue
                if day:
                    out.append((day, fv))
            out.sort(key=lambda p: p[0])
            return out
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last:
        raise last
    return []


def fmt(value: float) -> str:
    """Thousands-separated, 2 decimals: 642.31 -> '642.31'; 1052.4 -> '1,052.40'."""
    return f"{value:,.2f}"


def build_block(key: str, site: str, label: str, dam: str, points: list) -> dict | None:
    if not points:
        print(f"WARNING: {key} series empty; skipped.", file=sys.stderr)
        return None

    obs_date, current = points[-1]
    current = round(current, 2)

    # Trend window = the 7 daily points immediately before the latest obs.
    window = points[-8:-1] if len(points) >= 8 else points[:-1]
    block = {
        "label": label,
        "display_value": fmt(current),
        "note": "ft above sea level",
        "timestamp": f"Observed {obs_date}",
        "source_name": "USGS National Water Information System",
        "source_url": site_page(site),
        "api_url": api_url(site),
        "update_cadence": "Daily",
        "provisional": True,
        "usgs_site_id": site,
        "usgs_param_id": PARAM,
        "usgs_observed_date": obs_date,
        "dam": dam,
    }

    if not window:
        block["trend"] = "steady"
        block["trend_note"] = "no recent window"
        return block

    avg = round(mean(v for _, v in window), 2)
    diff = round(current - avg, 2)
    if abs(diff) <= BAND_FT:
        trend, word = "steady", "near"
    elif diff > 0:
        trend, word = "rising", "above"
    else:
        trend, word = "falling", "below"

    block["trend_band_ft"] = BAND_FT
    block["avg_7day"] = avg
    block["avg_7day_display"] = fmt(avg)
    block["trend_window_days"] = 7
    block["trend_window_start"] = window[0][0]
    block["trend_window_end"] = window[-1][0]
    block["ft_vs_avg"] = diff
    block["trend"] = trend
    block["trend_note"] = f"{abs(diff):.2f} ft {word} 7-day avg"
    return block


def main() -> int:
    if not STATUS_PATH.exists():
        print(f"WARNING: {STATUS_PATH} missing — skipping lower-reservoir update.", file=sys.stderr)
        return 0

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    indicators = status.setdefault("indicators", {})

    changed = False
    for key, site, label, dam in RESERVOIRS:
        try:
            points = fetch_series(api_url(site))
            block = build_block(key, site, label, dam, points)
        except Exception as exc:
            print(f"WARNING: {key} update failed ({exc}); leaving prior value.", file=sys.stderr)
            continue
        if block is None:
            continue
        indicators[key] = block
        changed = True
        print(f"{key}: {block['display_value']} {block['note']} "
              f"({block.get('trend', '?')}) @ {block['usgs_observed_date']}")

    if changed:
        now = datetime.now(ARIZONA)
        auto = status.setdefault("automation", {})
        auto["script_lower_reservoirs"] = "scripts/update_lower_reservoirs.py"
        auto["lower_reservoirs_source"] = (
            "USGS NWIS daily values (sites 09422500 Mohave, 09427500 Havasu; parameter 62614)"
        )
        auto["lower_reservoirs_last_run_display"] = now.strftime("%B %-d, %Y, %-I:%M %p Arizona time")
        STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        print("snowpack-status.json updated with Lake Mohave & Lake Havasu values.")
    else:
        print("No lower-reservoir changes written.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
