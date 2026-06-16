#!/usr/bin/env python3
"""
update_cap_diversion.py  --  WaterLineWest / Colorado River Water Scout

Publishes the `cap_diversion` indicator into docs/snowpack-status.json from the
Bureau of Reclamation Lower Colorado "Lower Colorado River Daily Report" feed.

Source (human page):  https://www.usbr.gov/lc/region/g4000/hourly/levels.html
Source (machine feed): https://www.usbr.gov/lc/region/g4000/riverops/webreports/accumweb.json

The feed is a {"Series":[ {SDI, SiteName, DataTypeName, DataTypeUnit, Data:[{t,v}...]}, ... ]}
structure of daily values, 365 days back. The C.A.P. diversion series gives us
everything the 6A module needs from one place:
    - latest non-empty daily value  -> headline (display_value, AF/day)
    - sum of the current calendar month -> month-to-date (fixes the static 26,634 cell)
    - 7-day trailing average vs latest  -> trend (rising / falling / steady)

DISCOVERY: we do NOT hard-code an SDI. On every run the script lists every series
in the feed (SDI | SiteName | DataTypeName) so you can see exactly what's there,
then auto-selects the CAP series by keyword. If you want to pin it, set SDI_OVERRIDE.
A sanity line prints the chosen series + latest value so you can eyeball it against
the report (e.g. June 5 2026 should read 5,323 AF).

Stdlib only (urllib, json, datetime, argparse). No third-party deps.
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime

# ----------------------------------------------------------------------------- config
FEED_URL = "https://www.usbr.gov/lc/region/g4000/riverops/webreports/accumweb.json"
SOURCE_PAGE = "https://www.usbr.gov/lc/region/g4000/hourly/levels.html"
SOURCE_NAME = "U.S. Bureau of Reclamation \u2014 Lower Colorado River Operations"
INDICATOR_KEY = "cap_diversion"
DEFAULT_STATUS_FILE = "docs/snowpack-status.json"

# Auto-discovery: a series matches if ANY phrase appears (case-insensitive) in its
# SiteName OR DataTypeName. The CAP intake report line is the target; the MWD
# "diversion at intake" line is explicitly excluded so we don't grab the wrong one.
MATCH_PHRASES = ["central arizona", "c.a.p", "cap diversion", "mark wilmer", "arizona project"]
EXCLUDE_PHRASES = ["metropolitan", "mwd"]

# Set to a specific SDI string (e.g. "2099") to skip auto-discovery and pin the series.
SDI_OVERRIDE = "3413"

# Trend logic, mirroring the Mead/Havasu indicators (7-day trailing window).
TREND_WINDOW_DAYS = 7
TREND_BAND_AF = 150          # within +/- this many AF/day of the 7-day avg => "steady"

ENSURE_ASCII = False         # match whatever your other update_*.py scripts use
# -----------------------------------------------------------------------------


def fetch_feed(url=FEED_URL, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "waterlinewest-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_series(feed):
    """Return [(sdi, sitename, datatypename, unit), ...] for diagnostics."""
    out = []
    for s in feed.get("Series", []):
        out.append((s.get("SDI", ""), s.get("SiteName", ""),
                    s.get("DataTypeName", ""), s.get("DataTypeUnit", "")))
    return out


def _matches(site, dtype):
    hay = (site + " " + dtype).lower()
    if any(x in hay for x in EXCLUDE_PHRASES):
        return False
    return any(p in hay for p in MATCH_PHRASES)


def select_series(feed, sdi_override=SDI_OVERRIDE):
    """Pick the CAP series. Returns (series_dict, how_selected). Raises on ambiguity."""
    series = feed.get("Series", [])
    if sdi_override:
        for s in series:
            if str(s.get("SDI", "")) == str(sdi_override):
                return s, f"SDI override {sdi_override}"
        raise ValueError(f"SDI_OVERRIDE={sdi_override} not found in feed.")
    hits = [s for s in series if _matches(s.get("SiteName", ""), s.get("DataTypeName", ""))]
    if len(hits) == 1:
        return hits[0], "keyword auto-match"
    if not hits:
        raise ValueError("No CAP series matched. Inspect the printed list and set SDI_OVERRIDE.")
    names = ", ".join(f'{h.get("SDI")}:{h.get("SiteName")}/{h.get("DataTypeName")}' for h in hits)
    raise ValueError(f"Ambiguous match ({len(hits)} series): {names}. Set SDI_OVERRIDE to one SDI.")


def clean_points(series):
    """[(date, float)] sorted ascending, skipping empty/non-numeric 'v'."""
    pts = []
    for d in series.get("Data", []):
        raw = (d.get("v") or "").strip().replace(",", "")
        if raw == "":
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        try:
            dt = datetime.strptime(d["t"], "%m/%d/%Y %I:%M:%S %p")
        except (KeyError, ValueError):
            continue
        pts.append((dt, val))
    pts.sort(key=lambda p: p[0])
    return pts


def compute_fields(points):
    """Derive latest, month-to-date, and 7-day trend from cleaned points."""
    if not points:
        raise ValueError("CAP series had no usable (non-empty) data points.")
    latest_dt, latest_val = points[-1]

    # Month-to-date: sum of this calendar month's dailies up to and incl. latest.
    mtd = sum(v for (dt, v) in points
              if dt.year == latest_dt.year and dt.month == latest_dt.month and dt <= latest_dt)

    # 7-day trailing window = the N days strictly before the latest reading.
    prior = [v for (dt, v) in points if dt < latest_dt][-TREND_WINDOW_DAYS:]
    if prior:
        avg7 = sum(prior) / len(prior)
        delta = latest_val - avg7
        if delta > TREND_BAND_AF:
            trend = "rising"
        elif delta < -TREND_BAND_AF:
            trend = "falling"
        else:
            trend = "steady"
        window_start = [dt for (dt, v) in points if dt < latest_dt][-len(prior)]
        window_end = max(dt for (dt, v) in points if dt < latest_dt)
    else:
        avg7, delta, trend = latest_val, 0.0, "steady"
        window_start = window_end = latest_dt

    return {
        "latest_dt": latest_dt,
        "latest_val": latest_val,
        "mtd": mtd,
        "avg7": avg7,
        "delta": delta,
        "trend": trend,
        "window_start": window_start,
        "window_end": window_end,
        "n_window": len(prior),
    }


def _af(x):
    return f"{round(x):,}"


def _trend_note(f):
    d = f["delta"]
    if f["trend"] == "steady":
        return f"{_af(abs(d))} AF near {TREND_WINDOW_DAYS}-day avg"
    direction = "above" if d > 0 else "below"
    return f"{_af(abs(d))} AF {direction} {TREND_WINDOW_DAYS}-day avg"


def build_indicator(series, f):
    return {
        "label": "CAP Diversion at Lake Havasu",
        "display_value": _af(f["latest_val"]),
        "note": "AF/day diverted at Mark Wilmer Pumping Plant",
        "timestamp": "Observed " + f["latest_dt"].strftime("%Y-%m-%d"),
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_PAGE,
        "api_url": FEED_URL,
        "update_cadence": "Daily",
        "provisional": True,
        "usbr_sdi": str(series.get("SDI", "")),
        "usbr_site_name": series.get("SiteName", ""),
        "usbr_observed_date": f["latest_dt"].strftime("%Y-%m-%d"),
        # month-to-date (also feeds the 6A "Month to Date" cell once it's hooked)
        "month_to_date": round(f["mtd"]),
        "month_to_date_display": _af(f["mtd"]),
        "month_to_date_note": "acre-feet, " + f["latest_dt"].strftime("%B") + " to date",
        # trend cluster, parallel to mead_elevation / havasu_elevation
        "trend_band_af": TREND_BAND_AF,
        "avg_7day": round(f["avg7"], 1),
        "avg_7day_display": _af(f["avg7"]),
        "trend_window_days": TREND_WINDOW_DAYS,
        "trend_window_start": f["window_start"].strftime("%Y-%m-%d"),
        "trend_window_end": f["window_end"].strftime("%Y-%m-%d"),
        "af_vs_avg": round(f["delta"], 1),
        "trend": f["trend"],
        "trend_note": _trend_note(f),
    }


def write_status(status_file, indicator):
    with open(status_file, "r", encoding="utf-8") as fh:
        status = json.load(fh)

    status.setdefault("indicators", {})[INDICATOR_KEY] = indicator

    now_disp = datetime.now().strftime("%B %-d, %Y, %-I:%M %p Arizona time")
    auto = status.setdefault("automation", {})
    auto["script_cap"] = "scripts/update_cap_diversion.py"
    auto["cap_source"] = "USBR Lower Colorado River Daily Report (accumweb.json)"
    auto["cap_last_run_display"] = now_disp

    with open(status_file, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, ensure_ascii=ENSURE_ASCII)
        fh.write("\n")


def main():
    ap = argparse.ArgumentParser(description="Publish cap_diversion into snowpack-status.json")
    ap.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    ap.add_argument("--sdi", default=SDI_OVERRIDE, help="Pin a specific series SDI (skips auto-match)")
    ap.add_argument("--dry-run", action="store_true", help="Print the block; do not write the file")
    args = ap.parse_args()

    feed = fetch_feed()

    # Always print the full series inventory -- this is how you SEE the CAP series.
    print("Series in feed (SDI | SiteName | DataTypeName | Unit):", file=sys.stderr)
    for sdi, site, dtype, unit in list_series(feed):
        print(f"  {sdi:>6} | {site} | {dtype} | {unit}", file=sys.stderr)

    series, how = select_series(feed, sdi_override=args.sdi)
    pts = clean_points(series)
    f = compute_fields(pts)

    print(f"\nSelected CAP series via {how}:", file=sys.stderr)
    print(f"  SDI {series.get('SDI')} | {series.get('SiteName')} | {series.get('DataTypeName')}",
          file=sys.stderr)
    print(f"  latest {f['latest_dt']:%Y-%m-%d} = {_af(f['latest_val'])} AF/day "
          f"| MTD {_af(f['mtd'])} AF | trend {f['trend']}", file=sys.stderr)

    indicator = build_indicator(series, f)

    if args.dry_run:
        print("\n--dry-run, would write this block:\n", file=sys.stderr)
        print(json.dumps({INDICATOR_KEY: indicator}, indent=2, ensure_ascii=ENSURE_ASCII))
        return

    write_status(args.status_file, indicator)
    print(f"\nWrote {INDICATOR_KEY} to {args.status_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
