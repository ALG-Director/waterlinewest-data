#!/usr/bin/env python3
"""
DAILY UPDATER (REST edition): write docs/snowpack-basins.json for the Upper
Colorado basins, sourced entirely from the AWDB REST API.

Why this rewrite: the static WTEQ dump host (nwcc-apps.sc.egov.usda.gov) firewalls
GitHub Actions IPs intermittently (403). The REST host (wcc.sc.egov.usda.gov) —
the one our /stations call already reaches reliably — also serves the daily values
through its /data endpoint, so we get everything from the host that answers.

Pipeline (same math as before, new data source):
  1. /stations  -> every HUC-14 SNOTEL station (triplet, name, elev, huc).
  2. /data (recent window) -> each station's latest current SWE + its date.
  3. /data (1991-2020 window) -> each station's full history; we keep the value on
     the SAME calendar date as the current reading, across years, and take the
     MEDIAN ourselves (the transparent self-computed normal).
  4. Curate to stations with >= MIN_NORMAL_YEARS in window, roll up to the robust
     basin index = sum(current)/sum(median), with per-basin distribution + a
     compiled Upper Colorado total. Melted/zero-median basins are labeled honestly.

Self-computed median, reliable host, no static dump. Soft-fails to exit 0 so a bad
snowpack day never breaks the workflow (it is also continue-on-error in the YAML).
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

NORMALS_START, NORMALS_END = 1991, 2020
MIN_NORMAL_YEARS = 10
CURRENT_LOOKBACK_DAYS = 12     # recent window to find each station's latest value
HIST_BATCH = 8                 # stations per /data call for the 30-yr history pull
CUR_BATCH = 60                 # stations per /data call for the recent-value pull

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
    }
    delay = 1.5
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (403, 408, 429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(delay); delay *= 2; continue
            raise
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(delay); delay *= 2; continue
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


def _values_from_entry(entry: dict) -> list:
    """Pull the [{date,value}, ...] list out of one data entry, shape-tolerant."""
    vals = entry.get("values") or entry.get("data") or entry.get("elementValues") or []
    out = []
    for v in vals:
        if not isinstance(v, dict):
            continue
        d = v.get("date") or v.get("dateTime") or v.get("datetime")
        val = v.get("value")
        if val is None:
            val = v.get("average")  # some shapes name it differently
        if d is not None and val is not None:
            out.append((str(d)[:10], float(val)))
    return out


def parse_series(resp: object) -> dict:
    """AWDB /data response -> {triplet: [(YYYY-MM-DD, value), ...]} (shape-tolerant)."""
    rows = resp if isinstance(resp, list) else resp.get("data") or resp.get("stationData") or []
    series: dict[str, list] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        triplet = (row.get("stationTriplet") or row.get("station_triplet")
                   or row.get("stationTripletId") or "")
        pts = []
        data = row.get("data") or row.get("stationElements") or row.get("elements") or []
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    pts.extend(_values_from_entry(entry))
        # some shapes put values directly on the row
        if not pts:
            pts.extend(_values_from_entry(row))
        if triplet:
            series.setdefault(triplet, []).extend(pts)
    return series


def fetch_series(triplets: list, begin: str, end: str, batch: int) -> dict:
    """Batched /data pull -> {triplet: [(date, value), ...]}, sorted ascending."""
    merged: dict[str, list] = {}
    for i in range(0, len(triplets), batch):
        chunk = triplets[i:i + batch]
        qs = urllib.parse.urlencode({
            "stationTriplets": ",".join(chunk),
            "elements": "WTEQ",
            "duration": "DAILY",
            "beginDate": begin,
            "endDate": end,
        })
        try:
            resp = fetch_json(f"{AWDB}/data?{qs}")
        except Exception as exc:
            print(f"  (data batch {i//batch} failed: {exc})", file=sys.stderr)
            continue
        for t, pts in parse_series(resp).items():
            merged.setdefault(t, []).extend(pts)
        time.sleep(0.1)  # be polite
    for t in merged:
        merged[t] = sorted(set(merged[t]))
    return merged


def band_for(pct):
    if pct is None:      return "none"
    if pct < 50:         return "far-below"
    if pct < 80:         return "below"
    if pct < 120:        return "near"
    if pct < 150:        return "above"
    return "far-above"


def basin_distribution(curated: list) -> dict | None:
    year_idx = []
    for y in range(NORMALS_START, NORMALS_END + 1):
        present = [s for s in curated if y in s["window"]]
        den = sum(s["median"] for s in present)
        if den > 0:
            num = sum(s["window"][y] for s in present)
            year_idx.append(100.0 * num / den)
    if len(year_idx) < 4:
        return None
    q = quantiles(year_idx, n=4)
    return {"min": round(min(year_idx)), "q25": round(q[0]), "median": round(q[1]),
            "q75": round(q[2]), "max": round(max(year_idx))}


def main() -> int:
    today = date.today()
    stations = get_stations()
    if not stations:
        print("WARNING: no HUC-14 stations returned; aborting.", file=sys.stderr)
        return 0
    triplets = list(stations)
    print(f"HUC-14 SNOTEL stations: {len(triplets)}")

    # 1) Recent values -> each station's latest reading + date.
    cur_begin = (today - timedelta(days=CURRENT_LOOKBACK_DAYS)).isoformat()
    current_series = fetch_series(triplets, cur_begin, today.isoformat(), CUR_BATCH)
    current = {}
    for t, pts in current_series.items():
        if pts:
            d, v = pts[-1]          # latest available (handles the 1-2 day lag)
            current[t] = (d, v)
    print(f"Stations with a current value: {len(current)}")
    if not current:
        print("WARNING: no current values from /data; aborting.", file=sys.stderr)
        return 0

    # 2) History 1991-2020 -> per-station full daily series (we self-compute medians).
    hist = fetch_series(triplets, f"{NORMALS_START-1}-10-01",
                        f"{NORMALS_END}-09-30", HIST_BATCH)
    print(f"Stations with history returned: {len(hist)}")

    # one-run shape check so any parse mismatch is obvious and one-line to fix
    sample_t = next(iter(current), None)
    if sample_t:
        cd, cv = current[sample_t]
        print(f"Decode check {sample_t} ({stations[sample_t]['name']}): "
              f"current {cv} on {cd}; history points {len(hist.get(sample_t, []))}")

    # 3) Build curated per-station facts.
    basins: dict[str, dict] = {}
    for t, meta in stations.items():
        if t not in current:
            continue
        cur_date, cur_val = current[t]
        target_md = cur_date[5:]   # MM-DD of the current reading
        window = {}
        for d, v in hist.get(t, []):
            if d[5:] == target_md:
                yr = int(d[:4])
                if NORMALS_START <= yr <= NORMALS_END:
                    window[yr] = v
        if len(window) < MIN_NORMAL_YEARS:
            continue
        med = median(window.values())
        b = basins.setdefault(meta["basin"], {"huc4": meta["huc4"], "stations": []})
        b["stations"].append({
            "triplet": t, "name": meta["name"], "elev": meta["elevation"],
            "cur": round(cur_val, 1), "median": round(med, 1),
            "pct": round(100.0 * cur_val / med) if med > 0 else None,
            "window": window,
        })

    # 4) Roll up.
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
        tot_cur += sum_cur; tot_med += sum_med; tot_sites += len(sites)
        out_basins.append({
            "name": name, "huc4": b["huc4"], "index_pct": idx, "band": band_for(idx),
            "current_swe_in": round(sum_cur, 1), "median_swe_in": round(sum_med, 1),
            "sites_reporting": len(sites),
            "distribution_pct": basin_distribution(sites),
            "status": ("normally snow-free by this date" if sum_med < 0.05 else None),
            "stations": [
                {k: s[k] for k in ("triplet", "name", "elev", "cur", "median", "pct")}
                for s in sorted(sites, key=lambda s: (s["elev"] or 0), reverse=True)
            ],
        })

    compiled_idx = round(100.0 * tot_cur / tot_med) if tot_med > 0 else None
    now = datetime.now(ARIZONA)
    as_of = max((current[t][0] for t in current), default=today.isoformat())

    out = {
        "module": "snowpack_basins",
        "title": "Upper Colorado Basin Snowpack",
        "as_of": as_of,
        "generated_at_display": now.strftime("%B %-d, %Y, %-I:%M %p Arizona time"),
        "normals_window": f"{NORMALS_START}-{NORMALS_END}",
        "min_normal_years": MIN_NORMAL_YEARS,
        "method": ("Basin index = sum(current SWE) / sum(per-station {a}-{b} median SWE) "
                   "across reporting SNOTEL stations with at least {n} years of record in "
                   "that window. Current values and history are pulled from the NRCS AWDB "
                   "REST /data endpoint; medians are computed by WaterLineWest."
                   ).format(a=NORMALS_START, b=NORMALS_END, n=MIN_NORMAL_YEARS),
        "source_name": "USDA NRCS SNOTEL (AWDB REST API)",
        "source_url": "https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html",
        "provisional": True,
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
          f"of median, {tot_sites} curated sites, as of {as_of}.")
    for b in out_basins:
        print(f"  {b['name']:<24} {str(b['index_pct']) + '%':>6}  ({b['sites_reporting']} sites)"
              + (f"  [{b['status']}]" if b["status"] else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
