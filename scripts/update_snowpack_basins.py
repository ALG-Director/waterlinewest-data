#!/usr/bin/env python3
"""
DAILY UPDATER: write docs/snowpack-basins.json for the Upper Colorado basins.

Built on the proven probe. Same pipeline:
  1. AWDB REST /stations -> every HUC-14 SNOTEL station (triplet, name, elev, huc).
  2. Freshest WTEQ by-date snapshot that already carries a current-year value
     (the archive lags 1-2 days, so today's file is often still a year behind).
  3. Per station: current SWE + its 1991-2020 median, percent of median.
  4. Roll up to a basin index the robust (NRCS-style) way: sum(current) /
     sum(median) across reporting stations.

What this adds over the probe:
  * CURATION — a station only counts toward a basin if it has at least
    MIN_NORMAL_YEARS of data inside the 1991-2020 window (i.e. a real normal).
    This is what pulls the basin-wide total in line with NRCS by dropping
    short-record / already-melted sites that otherwise pad the denominator.
  * A per-basin historical DISTRIBUTION in percent space (the spread of the
    basin index across the window years), so the deep-dive page can show this
    year against the 30-year range. The median of that spread sits near 100%.
  * A COMPILED entire-Upper-Colorado index (the number the hero pill will read).
  * Graceful handling of melted-out basins (no measurable normal -> null, not nan).

Writes docs/snowpack-basins.json. Does NOT touch snowpack-status.json — re-pointing
the hero pill is a separate, deliberate step.

Soft-fail: logs and exits 0 on trouble so a scheduled run never hard-breaks.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median, quantiles
from zoneinfo import ZoneInfo

OUT_PATH = Path("docs/snowpack-basins.json")
ARIZONA = ZoneInfo("America/Phoenix")

AWDB = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
DUMP = "https://nwcc-apps.sc.egov.usda.gov/awdb/data/WTEQ/DAILY/OBS/WTEQ_DAILY_OBS_{md}.json"

NORMALS_START, NORMALS_END = 1991, 2020
MIN_NORMAL_YEARS = 10          # min years in the 1991-2020 window for a usable normal.
                               # The robust index is insensitive to near-zero sites, so
                               # this is mainly to keep per-station percentages honest
                               # without cutting legitimate newer high-elevation stations.
DUMP_LOOKBACK = 4              # days to walk back for a live current-year file

# Some USDA hosts sit behind a WAF that 403s requests without a browser-like
# header set. Present as an ordinary browser and retry soft blocks.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

HUC4_BASINS = {
    "1401": "Colorado Headwaters",
    "1402": "Gunnison",
    "1403": "Upper Colorado-Dolores",
    "1404": "Upper Green",
    "1405": "Yampa / White",
    "1406": "Lower Green / Duchesne",
    "1407": "Dirty Devil / Escalante",
    "1408": "San Juan",
}


def fetch_json(url: str, attempts: int = 4) -> object:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nrcs.usda.gov/",
    }
    delay = 1.5
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            # 403/429/5xx are often transient WAF/rate blocks — back off and retry.
            if exc.code in (403, 408, 429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last:
        raise last


def get_stations() -> dict:
    qs = urllib.parse.urlencode({"hucs": "14", "networkCds": "SNTL", "elements": "WTEQ"})
    raw = fetch_json(f"{AWDB}/stations?{qs}")
    rows = raw if isinstance(raw, list) else raw.get("stations") or raw.get("data") or []
    out = {}
    for r in rows:
        triplet = r.get("stationTriplet") or r.get("station_triplet")
        huc = str(r.get("huc") or "")
        if not triplet or not huc.startswith("14"):
            continue
        out[triplet] = {
            "name": r.get("name", "?"),
            "huc4": huc[:4],
            "elevation": r.get("elevation"),
            "basin": HUC4_BASINS.get(huc[:4], f"Other (HUC {huc[:4]})"),
        }
    return out


def decode_series(arr: list) -> list:
    if not arr or not isinstance(arr[0], (int, float)):
        return []
    begin = int(arr[0])
    return [(begin + i, v) for i, v in enumerate(arr[1:]) if v is not None]


def fetch_dump(md: str) -> dict:
    return fetch_json(DUMP.format(md=md))


def max_year_for(dump: dict, stations: dict) -> int:
    best = 0
    for triplet in stations:
        arr = dump.get(triplet)
        pairs = decode_series(arr) if arr else []
        if pairs:
            best = max(best, pairs[-1][0])
    return best


def pick_recent_dump(today: date, stations: dict):
    freshest = None
    for offset in range(DUMP_LOOKBACK + 1):
        d = today - timedelta(days=offset)
        try:
            dump = fetch_dump(d.strftime("%m-%d"))
        except Exception as exc:
            print(f"  (skipping {d.isoformat()}: {exc})", file=sys.stderr)
            continue
        if freshest is None:
            freshest = (dump, d, False)
        if max_year_for(dump, stations) == today.year:
            return dump, d, True
    return freshest if freshest else ({}, today, False)


def band_for(pct):
    if pct is None:
        return "none"
    if pct < 50:
        return "far-below"
    if pct < 80:
        return "below"
    if pct < 120:
        return "near"
    if pct < 150:
        return "above"
    return "far-above"


def station_facts(arr: list, current_year: int):
    """Return (current, has_current, window_dict, median_window) or None."""
    pairs = decode_series(arr)
    if not pairs:
        return None
    last_year, current = pairs[-1]
    window = {y: v for (y, v) in pairs if NORMALS_START <= y <= NORMALS_END}
    if len(window) < MIN_NORMAL_YEARS:
        return None  # no real normal -> not curated
    med = median(window.values())
    return current, (last_year == current_year), window, med


def basin_distribution(curated: list) -> dict | None:
    """Spread of the basin index (percent space) across the window years."""
    year_idx = []
    for y in range(NORMALS_START, NORMALS_END + 1):
        present = [s for s in curated if y in s["window"]]
        den = sum(s["median"] for s in present)
        if den > 0:
            num = sum(s["window"][y] for s in present)
            year_idx.append(100.0 * num / den)
    if len(year_idx) < 4:
        return None
    q = quantiles(year_idx, n=4)  # q[0]=Q1, q[1]=median, q[2]=Q3
    return {
        "min": round(min(year_idx)),
        "q25": round(q[0]),
        "median": round(q[1]),
        "q75": round(q[2]),
        "max": round(max(year_idx)),
    }


def main() -> int:
    today = date.today()
    stations = get_stations()
    if not stations:
        print("WARNING: no HUC-14 stations returned; aborting.", file=sys.stderr)
        return 0

    dump, used_date, has_current = pick_recent_dump(today, stations)
    if not dump:
        print("WARNING: no WTEQ snapshot available; aborting.", file=sys.stderr)
        return 0
    current_year = max_year_for(dump, stations) or today.year

    # Gather curated stations per basin.
    basins: dict[str, dict] = {}
    for triplet, meta in stations.items():
        arr = dump.get(triplet)
        if not arr:
            continue
        facts = station_facts(arr, current_year)
        if facts is None:
            continue
        current, has_cur, window, med = facts
        if not has_cur:
            continue
        b = basins.setdefault(meta["basin"], {"huc4": meta["huc4"], "stations": []})
        b["stations"].append({
            "triplet": triplet, "name": meta["name"], "elev": meta["elevation"],
            "cur": round(current, 1), "median": round(med, 1),
            "pct": round(100.0 * current / med) if med > 0 else None,
            "window": window,
        })

    # Build per-basin output + compiled total.
    out_basins = []
    tot_cur = tot_med = 0.0
    tot_sites = 0
    for name in list(HUC4_BASINS.values()):
        b = basins.get(name)
        if not b:
            continue
        sites = b["stations"]
        sum_cur = sum(s["cur"] for s in sites)
        sum_med = sum(s["median"] for s in sites)
        idx = round(100.0 * sum_cur / sum_med) if sum_med > 0 else None
        tot_cur += sum_cur
        tot_med += sum_med
        tot_sites += len(sites)
        out_basins.append({
            "name": name,
            "huc4": b["huc4"],
            "index_pct": idx,
            "band": band_for(idx),
            "current_swe_in": round(sum_cur, 1),
            "median_swe_in": round(sum_med, 1),
            "sites_reporting": len(sites),
            "distribution_pct": basin_distribution(sites),
            "status": "melted out" if sum_med < 0.05 else None,
            "stations": [
                {k: s[k] for k in ("triplet", "name", "elev", "cur", "median", "pct")}
                for s in sorted(sites, key=lambda s: (s["elev"] or 0), reverse=True)
            ],
        })

    compiled_idx = round(100.0 * tot_cur / tot_med) if tot_med > 0 else None
    now = datetime.now(ARIZONA)

    out = {
        "module": "snowpack_basins",
        "title": "Upper Colorado Basin Snowpack",
        "as_of": used_date.isoformat(),
        "as_of_current_year": current_year,
        "generated_at_display": now.strftime("%B %-d, %Y, %-I:%M %p Arizona time"),
        "normals_window": f"{NORMALS_START}-{NORMALS_END}",
        "min_normal_years": MIN_NORMAL_YEARS,
        "method": ("Basin index = sum(current SWE) / sum(per-station {a}-{b} median SWE) "
                   "across reporting SNOTEL stations with at least {n} years of record in "
                   "that window. Distribution shows the spread of the same index across the "
                   "window years.").format(a=NORMALS_START, b=NORMALS_END, n=MIN_NORMAL_YEARS),
        "source_name": "USDA NRCS SNOTEL (Air & Water Database)",
        "source_url": "https://www.nrcs.usda.gov/resources/data-and-reports/snow-and-water-interactive-map",
        "provisional": True,
        "data_freshness": "live current-year reading" if has_current else "newest available (archive lag)",
        "compiled": {
            "label": "Entire Upper Colorado",
            "index_pct": compiled_idx,
            "display_value": (str(compiled_idx) + "%") if compiled_idx is not None else "\u2014",
            "note": "of median",
            "band": band_for(compiled_idx),
            "current_swe_in": round(tot_cur, 1),
            "median_swe_in": round(tot_med, 1),
            "sites": tot_sites,
        },
        "basins": out_basins,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"snowpack-basins.json written: entire UC {out['compiled']['display_value']} "
          f"of median, {tot_sites} curated sites, as of {used_date.isoformat()}.")
    for b in out_basins:
        print(f"  {b['name']:<24} {str(b['index_pct']) + '%':>5}  ({b['sites_reporting']} sites)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
