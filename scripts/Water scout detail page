#!/usr/bin/env python3
"""
ONE-TIME BACKFILL: build docs/history-stats.json for the Water Scout detail page.

Run this once (locally or via a manual workflow_dispatch) to seed deep history.
After that, scripts/update_history_append.py keeps it current on the daily run.

For each of the three live indicators it produces, in ONE file:
  * recent[]      -- daily values for the last ~RECENT_DAYS days (drives the
                     bold "this water year" line)
  * doy_stats{}   -- per day-of-year min / median / max across ALL years
                     (drives the shaded historical envelope + median line)
  * meta          -- record span, source, units, last build time

Why this shape: storing 20+ years of raw daily values for every gauge would be a
multi-megabyte file the browser has to parse on every visit. Instead we keep the
recent window verbatim (for the live line) and collapse the deep history into a
365-slot statistical envelope (for context). Small file, full 20-year story.

Indicators:
  tributary_inflow -> USGS 09180500 + 09315000 + 09379500 (composite sum, cfs)
  lees_ferry_flow  -> USGS 09380000 (cfs)
  powell_elevation -> USBR reservoir 919, datatype 49 (feet)
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OUT_PATH = Path("docs/history-stats.json")

# How many trailing days to keep as a verbatim daily series (the live line).
# ~2 years so the page can show the current water year plus the prior one.
RECENT_DAYS = 760

# How far back to pull for building the envelope statistics.
HISTORY_START = "2000-01-01"

TRIB_STATIONS = {
    "09180500": "Colorado River near Cisco",
    "09315000": "Green River at Green River",
    "09379500": "San Juan River near Bluff",
}
LEES_SITE = "09380000"
USBR_POWELL_URL = "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/49.json"

TODAY = date.today().isoformat()


def load_json_from_url(url: str, attempts: int = 4, timeout: int = 120) -> dict:
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = RuntimeError(f"Request failed with HTTP {exc.code}: {exc.reason}")
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"Request failed: {reason}")
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise last_error


def usgs_dv_url(sites: str) -> str:
    return (
        "https://waterservices.usgs.gov/nwis/dv/"
        f"?format=json&sites={sites}&parameterCd=00060&statCd=00003"
        f"&startDT={HISTORY_START}&endDT={TODAY}&siteStatus=all"
    )


def site_no(item: dict):
    codes = item.get("sourceInfo", {}).get("siteCode", [])
    return codes[0].get("value") if codes else None


def usgs_daily_map(payload: dict, want_site: str) -> dict:
    """Return {date_str: value} for one site from a USGS dv payload."""
    out = {}
    for item in payload.get("value", {}).get("timeSeries", []):
        if site_no(item) != want_site:
            continue
        groups = item.get("values", [])
        if not groups or not groups[0].get("value"):
            continue
        for pt in groups[0]["value"]:
            v, d = pt.get("value"), pt.get("dateTime")
            if v is None or d is None:
                continue
            try:
                val = float(v)
            except ValueError:
                continue
            if val < 0:  # -999999 no-data sentinel
                continue
            out[str(d)[:10]] = val
    return out


def fetch_tributary_series() -> dict:
    """Composite daily sum across the 3 arms, for dates present in ALL of them."""
    sites = ",".join(TRIB_STATIONS)
    payload = load_json_from_url(usgs_dv_url(sites))
    per_site = {s: usgs_daily_map(payload, s) for s in TRIB_STATIONS}
    common = set.intersection(*(set(m.keys()) for m in per_site.values()))
    return {d: sum(per_site[s][d] for s in TRIB_STATIONS) for d in common}


def fetch_lees_series() -> dict:
    payload = load_json_from_url(usgs_dv_url(LEES_SITE))
    return usgs_daily_map(payload, LEES_SITE)


def fetch_powell_series() -> dict:
    payload = load_json_from_url(USBR_POWELL_URL)
    out = {}
    for row in payload.get("data", []):
        if not row or len(row) < 2 or row[0] is None or row[1] is None:
            continue
        try:
            out[str(row[0])[:10]] = float(row[1])
        except (ValueError, TypeError):
            continue
    return out


def day_of_year_key(d: date) -> int:
    """Day-of-year normalized so Feb 29 folds into Feb 28 (1..365)."""
    doy = d.timetuple().tm_yday
    if (d.month, d.day) == (2, 29):
        return 59  # treat as Feb 28's slot
    if d.month > 2 and (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        doy -= 1  # shift post-leap-day so all years align to 365
    return min(max(doy, 1), 365)


def build_indicator(series: dict, units: str, round_to: int) -> dict:
    """Turn a {date_str: value} map into recent[] + doy_stats{}."""
    dates_sorted = sorted(series.keys())
    if not dates_sorted:
        raise RuntimeError("Empty series; nothing to build.")

    # Recent verbatim window
    recent_cutoff = dates_sorted[-1]
    recent = [{"d": d, "v": round(series[d], round_to)} for d in dates_sorted[-RECENT_DAYS:]]

    # Day-of-year envelope across ALL years
    buckets = defaultdict(list)
    for d in dates_sorted:
        dt = date.fromisoformat(d)
        buckets[day_of_year_key(dt)].append(series[d])
    doy_stats = {}
    for k in range(1, 366):
        vals = buckets.get(k)
        if not vals:
            continue
        doy_stats[str(k)] = {
            "min": round(min(vals), round_to),
            "med": round(median(vals), round_to),
            "max": round(max(vals), round_to),
        }

    return {
        "units": units,
        "record_start": dates_sorted[0],
        "record_end": dates_sorted[-1],
        "years_of_record": round(
            (date.fromisoformat(dates_sorted[-1]) - date.fromisoformat(dates_sorted[0])).days / 365.25, 1
        ),
        "recent": recent,
        "doy_stats": doy_stats,
    }


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    out = {
        "schema": "waterlinewest-history-1",
        "built": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "recent_days": RECENT_DAYS,
        "indicators": {},
    }

    builders = [
        ("tributary_inflow", fetch_tributary_series, "cfs", 0,
         "USGS composite: 09180500 + 09315000 + 09379500"),
        ("lees_ferry_flow", fetch_lees_series, "cfs", 0,
         "USGS 09380000 Colorado River at Lees Ferry"),
        ("powell_elevation", fetch_powell_series, "ft", 2,
         "USBR reservoir 919 datatype 49 Lake Powell pool elevation"),
    ]

    for key, fetch, units, rnd, source in builders:
        print(f"Fetching {key} ...", file=sys.stderr)
        try:
            series = fetch()
            ind = build_indicator(series, units, rnd)
            ind["source"] = source
            out["indicators"][key] = ind
            print(f"  {key}: {len(series):,} days, "
                  f"{ind['years_of_record']} yr ({ind['record_start']} -> {ind['record_end']})",
                  file=sys.stderr)
        except Exception as exc:
            print(f"  ERROR building {key}: {exc}", file=sys.stderr)
            # Keep going so a single failed source doesn't lose the others.
            continue

    if not out["indicators"]:
        raise RuntimeError("No indicators built; aborting without writing.")

    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH} with {len(out['indicators'])} indicators.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
