#!/usr/bin/env python3
"""
PROBE: Upper Colorado basin snowpack index from NRCS SNOTEL.

Goal: prove the full pipeline end-to-end before we design any UI.
  1. Pull every Upper Colorado (HUC region 14) SNOTEL station's metadata from
     the AWDB REST API  ->  station triplet, name, elevation, HUC.
  2. Pull today's daily Snow-Water-Equivalent (WTEQ) snapshot, which gives, per
     station, that calendar date's value for every year of record.
  3. Group stations into the sub-basins that drain toward Lake Powell (by HUC4),
     compute each station's current SWE vs its 1991-2020 median, and roll up to
     a basin index two ways so we can see which is robust in shoulder season.

Sanity targets (from today's NRCS Snow-Precipitation Update):
  ENTIRE UPPER COLORADO ~ 14% of median; Yampa/White ~ 26; Upper Green ~ 14;
  Duchesne ~ 2. If our sum-ratio index lands near these, the join is correct.

No third-party deps (urllib only). Runs where the network can reach usda.gov
(GitHub Actions / your laptop). Read-only: prints a report, writes nothing.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import date
from statistics import median, mean

AWDB = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
DUMP = "https://nwcc-apps.sc.egov.usda.gov/awdb/data/WTEQ/DAILY/OBS/WTEQ_DAILY_OBS_{md}.json"

# Upper Colorado sub-basins above Lake Powell, keyed by HUC4 accounting unit.
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

# NRCS standard normals window.
NORMALS_START, NORMALS_END = 1991, 2020


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "WaterLineWest/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_stations() -> dict:
    """All HUC-14* SNOTEL stations -> {triplet: {name, huc, elevation, basin}}."""
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
            "huc": huc,
            "elevation": r.get("elevation"),
            "basin": HUC4_BASINS.get(huc[:4], f"Other (HUC {huc[:4]})"),
        }
    return out


def fetch_dump(md: str) -> dict:
    """Today's WTEQ snapshot: {triplet: [begin_year, v_yr1, v_yr2, ... v_latest]}."""
    return fetch_json(DUMP.format(md=md))


def decode_series(arr: list) -> list:
    """[begin_year, v1, v2, ...] -> [(year, value), ...] dropping nulls."""
    if not arr or not isinstance(arr[0], (int, float)):
        return []
    begin = int(arr[0])
    return [(begin + i, v) for i, v in enumerate(arr[1:]) if v is not None]


def station_reading(arr: list, this_year: int) -> tuple | None:
    """Return (current_value, median_value, is_current) or None if unusable."""
    pairs = decode_series(arr)
    if not pairs:
        return None
    last_year, current = pairs[-1]
    window = [v for (y, v) in pairs if NORMALS_START <= y <= NORMALS_END]
    med = median(window) if window else median([v for _, v in pairs])
    return current, med, (last_year == this_year)


def main() -> int:
    today = date.today()
    md = today.strftime("%m-%d")
    this_year = today.year

    print(f"Upper Colorado SNOTEL probe  |  as of {today.isoformat()}  "
          f"(WTEQ daily snapshot {md})\n")

    stations = get_stations()
    print(f"Stations in HUC region 14 (SNOTEL, WTEQ): {len(stations)}")
    dump = fetch_dump(md)
    print(f"Stations in today's WTEQ snapshot:        {len(dump)}\n")

    # Show how we're decoding one real station, so the year-mapping is auditable.
    sample = next((t for t in stations if t in dump), None)
    if sample:
        pairs = decode_series(dump[sample])
        if pairs:
            yrs = f"{pairs[0][0]}-{pairs[-1][0]}"
            print(f"Decode check  {sample} ({stations[sample]['name']}): "
                  f"{len(pairs)} yrs {yrs}; latest {pairs[-1][0]}={pairs[-1][1]} in")
            print()

    basins: dict[str, dict] = {}
    stale = 0
    for triplet, meta in stations.items():
        arr = dump.get(triplet)
        if not arr:
            continue
        reading = station_reading(arr, this_year)
        if reading is None:
            continue
        current, med, is_current = reading
        if not is_current:
            stale += 1
            continue
        b = basins.setdefault(meta["basin"], {"cur": 0.0, "med": 0.0, "pcts": [], "n": 0})
        b["cur"] += current
        b["med"] += med
        b["n"] += 1
        if med > 0:
            b["pcts"].append(current / med * 100)

    # Report. sum-ratio = NRCS-style (robust); mean-of-pct = per-station avg (fragile).
    hdr = f"{'Basin':<26}{'sites':>6}{'curSWE':>9}{'medSWE':>9}{'idx%':>7}{'meanPct%':>10}"
    print(hdr)
    print("-" * len(hdr))
    tot_cur = tot_med = 0.0
    tot_n = 0
    all_pcts = []
    for name in list(HUC4_BASINS.values()) + sorted(
            k for k in basins if k not in HUC4_BASINS.values()):
        b = basins.get(name)
        if not b:
            continue
        idx = b["cur"] / b["med"] * 100 if b["med"] > 0 else float("nan")
        mpct = mean(b["pcts"]) if b["pcts"] else float("nan")
        print(f"{name:<26}{b['n']:>6}{b['cur']:>9.1f}{b['med']:>9.1f}{idx:>7.0f}{mpct:>10.0f}")
        tot_cur += b["cur"]; tot_med += b["med"]; tot_n += b["n"]
        all_pcts += b["pcts"]
    print("-" * len(hdr))
    tidx = tot_cur / tot_med * 100 if tot_med > 0 else float("nan")
    tmpct = mean(all_pcts) if all_pcts else float("nan")
    print(f"{'ENTIRE UPPER COLORADO':<26}{tot_n:>6}{tot_cur:>9.1f}{tot_med:>9.1f}"
          f"{tidx:>7.0f}{tmpct:>10.0f}")
    print(f"\n({stale} stations skipped as not-current / discontinued.)")
    print("idx% = sum(current)/sum(median) [NRCS-style, robust]; "
          "meanPct% = average of per-station % [fragile near zero].")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
