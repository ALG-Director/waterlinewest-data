#!/usr/bin/env python3
"""
DAILY UPDATER: publish Lake Mohave and Lake Havasu live elevations into
docs/snowpack-status.json. Sibling of update_lake_mead.py, sourced from the USGS
NWIS web services (the same family the tributary / Lees Ferry updaters use).

  Lake Mohave at Davis Dam     USGS 09422500
      -> param 62614 (lake/reservoir water surface elevation, NGVD 1929, ft),
         published directly. status key "mohave_elevation".

  Lake Havasu near Parker Dam  USGS 09427500
      -> this gauge does NOT publish 62614. It publishes GAGE HEIGHT (param
         00065). USGS converts gage height to water-surface elevation (NAVD 1988)
         by ADDING the site datum offset of 402.85 ft. status key
         "havasu_elevation".

So each reservoir has its own (param, datum_offset). For each, we try the daily-
values (DV) service first, then fall back to instantaneous values (IV) resampled
to one reading per day. We take the latest as the current reading and the
preceding 7 daily points as the trend window (matching the powell/mead blocks).

NOTE on datums: Mohave is reported on NGVD 1929 and Havasu (derived) on NAVD 1988;
the two differ by a couple of feet, which is immaterial for a public lake-level
display but is recorded in each block's "datum" field.

Soft-fail: any fetch/parse problem logs and leaves the prior value; the script
exits 0 so the rest of the workflow still commits. The observed date and value
are logged so a stale or wrong feed is obvious on the first run.
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

LOOKBACK_DAYS = 45
BAND_FT = 0.5          # within +/- this many feet of the 7-day avg reads as "steady"

# (status_key, usgs_site, label, dam, param, datum_offset_ft, datum_name)
RESERVOIRS = [
    ("mohave_elevation", "09422500", "Lake Mohave Elevation", "Davis Dam", "62614",   0.00, "NGVD 1929"),
    ("havasu_elevation", "09427500", "Lake Havasu Elevation", "Parker Dam", "00065", 402.85, "NAVD 1988"),
]


def dv_url(site: str, param: str) -> str:
    end = datetime.now(ARIZONA).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return ("https://waterservices.usgs.gov/nwis/dv/?format=json"
            f"&sites={site}&parameterCd={param}"
            f"&startDT={start.isoformat()}&endDT={end.isoformat()}")


def iv_url(site: str, param: str) -> str:
    return ("https://waterservices.usgs.gov/nwis/iv/?format=json"
            f"&sites={site}&parameterCd={param}&period=P10D")


def site_page(site: str) -> str:
    return f"https://waterdata.usgs.gov/monitoring-location/USGS-{site}/"


def _parse(payload: dict) -> list:
    """value.timeSeries[0].values[0].value[] -> [(date, float)], sentinels dropped."""
    series = payload.get("value", {}).get("timeSeries", [])
    if not series:
        return []
    raw_values = series[0].get("values", [{}])[0].get("value", [])
    out = []
    for v in raw_values:
        raw = v.get("value")
        day = str(v.get("dateTime", ""))[:10]
        if raw in (None, "") or not day:
            continue
        try:
            fv = float(raw)
        except (TypeError, ValueError):
            continue
        if fv <= -999998:          # USGS no-data sentinel
            continue
        out.append((day, fv))
    return out


def _get(url: str, attempts: int = 3) -> list:
    delay = 2.0
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WaterLineWest/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return _parse(payload)
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


def _resample_daily(points: list) -> list:
    """Collapse sub-daily (instantaneous) points to the latest reading per day."""
    by_day = {}
    for day, val in points:        # ascending -> last write per day wins
        by_day[day] = val
    return sorted(by_day.items(), key=lambda p: p[0])


def fetch_series(site: str, param: str, offset: float) -> tuple:
    """Return (points, source_kind). Try DV, then IV resampled to daily.

    Applies the datum offset so every value is a water-surface elevation in feet.
    """
    pts = _get(dv_url(site, param))
    kind = "dv"
    if not pts:
        pts = _resample_daily(_get(iv_url(site, param)))
        kind = "iv"
    pts = sorted(((d, round(v + offset, 2)) for d, v in pts), key=lambda p: p[0])
    return pts, kind


def fmt(value: float) -> str:
    return f"{value:,.2f}"


def build_block(key, site, label, dam, param, datum, kind, points) -> dict | None:
    if not points:
        print(f"WARNING: {key} series empty (param {param}); skipped.", file=sys.stderr)
        return None

    obs_date, current = points[-1]
    current = round(current, 2)
    window = points[-8:-1] if len(points) >= 8 else points[:-1]

    block = {
        "label": label,
        "display_value": fmt(current),
        "note": "ft above sea level",
        "timestamp": f"Observed {obs_date}",
        "source_name": "USGS National Water Information System",
        "source_url": site_page(site),
        "api_url": (dv_url(site, param) if kind == "dv" else iv_url(site, param)),
        "update_cadence": "Daily",
        "provisional": True,
        "usgs_site_id": site,
        "usgs_param_id": param,
        "usgs_value_kind": kind,
        "usgs_observed_date": obs_date,
        "datum": datum,
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
    for key, site, label, dam, param, offset, datum in RESERVOIRS:
        try:
            points, kind = fetch_series(site, param, offset)
            block = build_block(key, site, label, dam, param, datum, kind, points)
        except Exception as exc:
            print(f"WARNING: {key} update failed ({exc}); leaving prior value.", file=sys.stderr)
            continue
        if block is None:
            continue
        indicators[key] = block
        changed = True
        print(f"{key}: {block['display_value']} {block['note']} "
              f"({block.get('trend', '?')}) @ {block['usgs_observed_date']} "
              f"[{kind}, param {param}]")

    if changed:
        now = datetime.now(ARIZONA)
        auto = status.setdefault("automation", {})
        auto["script_lower_reservoirs"] = "scripts/update_lower_reservoirs.py"
        auto["lower_reservoirs_source"] = (
            "USGS NWIS (09422500 Mohave param 62614; 09427500 Havasu param 00065 +402.85 ft datum)"
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
