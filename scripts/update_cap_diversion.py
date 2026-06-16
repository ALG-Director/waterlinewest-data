#!/usr/bin/env python3
"""
update_cap_diversion.py  --  WaterLineWest / Colorado River Water Scout

Publishes the `cap_diversion` indicator into docs/snowpack-status.json.

SOURCE NOTE: Reclamation's machine feed (accumweb.json) defines a "CAP Canal
Export" series (SDI 3413) but leaves its values BLANK -- the column exists, the
numbers don't. The actual CAP diversion figures are only published in the human
"Lower Colorado River Daily Report" table on levels.html. So this script parses
that report's ACCUMULATIONS table directly.

  https://www.usbr.gov/lc/region/g4000/hourly/levels.html

In that table the two right-most columns (header "C.A.P.  DIVER. / ACCUM.") are:
    DIVER. A.F.   = that day's CAP diversion        -> headline (display_value)
    DIVER. ACCUM. = running month-to-date total     -> month-to-date cell
A 7-day average of the daily column gives the trend, same as Mead/Havasu.

Because the daily report is fixed-width text, parsing is by whitespace tokens,
anchored on the last two numeric tokens of each day row (robust to the mid-row
gaps the report sometimes has, e.g. a missing Davis gen cell). Stdlib only.
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime

# ----------------------------------------------------------------------------- config
REPORT_URL = "https://www.usbr.gov/lc/region/g4000/hourly/levels.html"
SOURCE_NAME = "U.S. Bureau of Reclamation \u2014 Lower Colorado River Daily Report"
INDICATOR_KEY = "cap_diversion"
DEFAULT_STATUS_FILE = "docs/snowpack-status.json"

TREND_WINDOW_DAYS = 7
TREND_BAND_AF = 150          # within +/- this many AF/day of the 7-day avg => "steady"
ENSURE_ASCII = False         # match whatever your other update_*.py scripts use
# -----------------------------------------------------------------------------


def fetch_report(url=REPORT_URL, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "waterlinewest-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_accumulations(html_text):
    """Parse the ACCUMULATIONS table -> [(date, cap_daily_af, cap_accum_af), ...] asc."""
    text = re.sub(r"<[^>]+>", "", html_text)  # drop any HTML tags, keep the <pre> text

    m = re.search(r"ACCUMULATIONS\s+FOR\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        raise ValueError("Could not find 'ACCUMULATIONS FOR <MONTH> <YEAR>' in the report.")
    month = datetime.strptime(m.group(1).title(), "%B").month
    year = int(m.group(2))

    # Scope strictly to this table: from the header to its TOTAL line. The later
    # tables (reservoir elevations, CRSP, losses) also have day-numbered rows but
    # with different right-most columns, so we must not read past TOTAL.
    tail = text[m.end():]
    cut = tail.find("TOTAL")
    block = tail[:cut] if cut != -1 else tail

    rows = []
    for line in block.splitlines():
        toks = line.split()
        if len(toks) < 3 or not toks[0].isdigit():
            continue
        day = int(toks[0])
        if not (1 <= day <= 31):
            continue
        try:
            daily = float(toks[-2].replace(",", ""))
            accum = float(toks[-1].replace(",", ""))
            dt = datetime(year, month, day)
        except ValueError:
            continue
        rows.append((dt, daily, accum))

    rows.sort(key=lambda r: r[0])
    return rows


def compute_fields(rows):
    if not rows:
        raise ValueError("No CAP rows parsed from the daily report (table layout changed?).")
    latest_dt, latest_val, accum = rows[-1]

    prior = [d for (dt, d, a) in rows if dt < latest_dt][-TREND_WINDOW_DAYS:]
    if prior:
        avg7 = sum(prior) / len(prior)
        delta = latest_val - avg7
        if delta > TREND_BAND_AF:
            trend = "rising"
        elif delta < -TREND_BAND_AF:
            trend = "falling"
        else:
            trend = "steady"
        earlier = [dt for (dt, d, a) in rows if dt < latest_dt]
        window_start, window_end = earlier[-len(prior)], earlier[-1]
    else:
        avg7, delta, trend = latest_val, 0.0, "steady"
        window_start = window_end = latest_dt

    return {
        "latest_dt": latest_dt, "latest_val": latest_val, "mtd": accum,
        "avg7": avg7, "delta": delta, "trend": trend,
        "window_start": window_start, "window_end": window_end, "n_window": len(prior),
    }


def _af(x):
    return f"{round(x):,}"


def _trend_note(f):
    d = f["delta"]
    if f["trend"] == "steady":
        return f"{_af(abs(d))} AF near {TREND_WINDOW_DAYS}-day avg"
    return f"{_af(abs(d))} AF {'above' if d > 0 else 'below'} {TREND_WINDOW_DAYS}-day avg"


def build_indicator(f):
    return {
        "label": "CAP Diversion at Lake Havasu",
        "display_value": _af(f["latest_val"]),
        "note": "AF/day diverted at Mark Wilmer Pumping Plant",
        "timestamp": "Observed " + f["latest_dt"].strftime("%Y-%m-%d"),
        "source_name": SOURCE_NAME,
        "source_url": REPORT_URL,
        "api_url": REPORT_URL,
        "update_cadence": "Daily",
        "provisional": True,
        "extraction": "parsed from LC Daily Report ACCUMULATIONS table (C.A.P. columns)",
        "usbr_observed_date": f["latest_dt"].strftime("%Y-%m-%d"),
        "month_to_date": round(f["mtd"]),
        "month_to_date_display": _af(f["mtd"]),
        "month_to_date_note": "acre-feet, " + f["latest_dt"].strftime("%B") + " to date",
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
    auto["cap_source"] = "USBR LC Daily Report (levels.html ACCUMULATIONS table)"
    auto["cap_last_run_display"] = now_disp
    with open(status_file, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, ensure_ascii=ENSURE_ASCII)
        fh.write("\n")


def main():
    ap = argparse.ArgumentParser(description="Publish cap_diversion from the LC Daily Report")
    ap.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    ap.add_argument("--dry-run", action="store_true", help="Print the block; do not write")
    args = ap.parse_args()

    rows = parse_accumulations(fetch_report())

    print(f"Parsed {len(rows)} CAP day-rows from the report. Last 7:", file=sys.stderr)
    for dt, d, a in rows[-7:]:
        print(f"  {dt:%Y-%m-%d}  daily={_af(d)} AF  accum={_af(a)} AF", file=sys.stderr)

    f = compute_fields(rows)
    indicator = build_indicator(f)

    print(f"\nLatest {f['latest_dt']:%Y-%m-%d} = {_af(f['latest_val'])} AF/day "
          f"| MTD {_af(f['mtd'])} AF | trend {f['trend']}", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run, would write this block:\n", file=sys.stderr)
        print(json.dumps({INDICATOR_KEY: indicator}, indent=2, ensure_ascii=ENSURE_ASCII))
        return

    write_status(args.status_file, indicator)
    print(f"\nWrote {INDICATOR_KEY} to {args.status_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
