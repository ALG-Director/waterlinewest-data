#!/usr/bin/env python3
"""
ONE-TIME (re-runnable) BACKFILL: seed Lake Mohave and Lake Havasu history into
docs/history-stats.json so they appear on the Water Scout detail/master-graph
page, then keep growing via update_history_append.py.

Source: USGS NWIS daily values (waterservices.usgs.gov), full available record.
  Lake Mohave  09422500  param 62614 (elevation, NGVD 1929)      offset 0
  Lake Havasu  09427500  param 00065 (gage height) + 402.85 ft   -> elevation (NAVD 1988)

For each indicator we write, in the schema the detail page expects:
  indicators[key] = {
    "doy_stats": { "<calendar_doy 1..366>": {"min":x,"med":y,"max":z}, ... },
    "recent":    [ {"d":"YYYY-MM-DD","v":n}, ... ],   # last ~400 days, ascending
    "years_of_record": int, "record_start": "YYYY-MM-DD", "record_end": "YYYY-MM-DD"
  }

Existing indicators and any other top-level keys in history-stats.json are
preserved; only mohave_elevation / havasu_elevation are (re)written.

Run this once locally or as a manual GitHub Action, commit docs/history-stats.json,
then add the two keys to update_history_append.py TRACKED for daily growth.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from statistics import median

HISTORY_PATH = Path("docs/history-stats.json")
START_DT = "1990-01-01"            # 30+ yrs is plenty for a day-of-year envelope
RECENT_DAYS = 400                  # enough to contain the full current water year

# (key, site, param, offset)
RESERVOIRS = [
    ("mohave_elevation", "09422500", "62614",   0.00),
    ("havasu_elevation", "09427500", "00065", 402.85),
]


def day_of_year_key(d: date) -> int:
    """Identical to update_history_append.day_of_year_key: leap-folded 1..365."""
    doy = d.timetuple().tm_yday
    if (d.month, d.day) == (2, 29):
        return 59
    if d.month > 2 and (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        doy -= 1
    return min(max(doy, 1), 365)


def dv_url(site: str, param: str) -> str:
    end = datetime.now().date().isoformat()
    return ("https://waterservices.usgs.gov/nwis/dv/?format=json"
            f"&sites={site}&parameterCd={param}&startDT={START_DT}&endDT={end}")


def fetch_history(site: str, param: str, offset: float) -> list:
    """Return ascending [(date_obj, elevation_float), ...] for the full record."""
    url = dv_url(site, param)
    delay = 3.0
    payload = None
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WaterLineWest/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            last = exc
            if i < 2:
                time.sleep(delay); delay *= 2; continue
            raise
    if payload is None:
        raise last or RuntimeError("no payload")

    series = payload.get("value", {}).get("timeSeries", [])
    if not series:
        return []
    raw = series[0].get("values", [{}])[0].get("value", [])
    out = []
    for v in raw:
        s = v.get("value")
        day = str(v.get("dateTime", ""))[:10]
        if s in (None, "") or not day:
            continue
        try:
            fv = float(s)
        except (TypeError, ValueError):
            continue
        if fv <= -999998:
            continue
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        out.append((d, round(fv + offset, 2)))
    out.sort(key=lambda p: p[0])
    return out


def build_indicator(points: list) -> dict | None:
    if not points:
        return None
    # day-of-year envelope (leap-folded calendar doy 1..365, matching the appender)
    buckets: dict[int, list] = {}
    for d, v in points:
        buckets.setdefault(day_of_year_key(d), []).append(v)
    doy_stats = {}
    for doy, vals in buckets.items():
        doy_stats[str(doy)] = {
            "min": round(min(vals), 2),
            "med": round(median(vals), 2),
            "max": round(max(vals), 2),
        }

    cutoff = points[-1][0] - timedelta(days=RECENT_DAYS)
    recent = [{"d": d.isoformat(), "v": v} for d, v in points if d >= cutoff]

    start, end = points[0][0], points[-1][0]
    return {
        "doy_stats": doy_stats,
        "recent": recent,
        "years_of_record": end.year - start.year + 1,
        "record_start": start.isoformat(),
        "record_end": end.isoformat(),
    }


def main() -> int:
    if HISTORY_PATH.exists():
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    else:
        print(f"NOTE: {HISTORY_PATH} not found; creating a new file.", file=sys.stderr)
        history = {}
    indicators = history.setdefault("indicators", {})

    wrote = False
    for key, site, param, offset in RESERVOIRS:
        try:
            pts = fetch_history(site, param, offset)
        except Exception as exc:
            print(f"WARNING: {key} history fetch failed ({exc}); left unchanged.", file=sys.stderr)
            continue
        block = build_indicator(pts)
        if block is None:
            print(f"WARNING: {key} returned no usable daily history "
                  f"(site {site} param {param}); skipped. Chart will show "
                  f"'history not available' until a source is found.", file=sys.stderr)
            continue
        indicators[key] = block
        wrote = True
        print(f"{key}: {len(pts):,} daily points, {block['years_of_record']} yr record "
              f"({block['record_start']} -> {block['record_end']}), "
              f"{len(block['doy_stats'])} day-of-year buckets, "
              f"{len(block['recent'])} recent points.")

    if wrote:
        HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
        print(f"Wrote {HISTORY_PATH} (Mohave / Havasu history merged; other indicators preserved).")
    else:
        print("Nothing written.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
