#!/usr/bin/env python3
"""
DAILY APPEND: keep docs/history-stats.json current.

Runs in the daily workflow AFTER the three updater scripts have written
docs/snowpack-status.json. It reads today's freshly-published values straight
out of snowpack-status.json (no extra API calls) and appends them to each
indicator's recent[] window, trimming to recent_days.

It also refreshes today's day-of-year envelope slot with the new observation so
the "normal" band keeps learning over time. (A full envelope rebuild only
happens when you re-run backfill_history.py; this is a light touch-up.)

Soft-fail: if the history file or a value is missing, it logs and exits 0 so the
rest of the workflow still commits.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from statistics import median

HISTORY_PATH = Path("docs/history-stats.json")
STATUS_PATH = Path("docs/snowpack-status.json")

# Which status indicators map into history, and how to round their values.
TRACKED = {
    "tributary_inflow": 0,
    "lees_ferry_flow": 0,
    "powell_elevation": 2,
}


def parse_display_value(raw) -> float | None:
    """'9,470' -> 9470.0 ; '3,527.41' -> 3527.41 ; None on junk."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def observed_date(indicator: dict) -> str:
    """Best-effort YYYY-MM-DD for the observation; falls back to today (UTC)."""
    for k in ("usgs_observed_datetime", "usbr_observed_date"):
        v = indicator.get(k)
        if v:
            return str(v)[:10]
    return date.today().isoformat()


def day_of_year_key(d: date) -> int:
    doy = d.timetuple().tm_yday
    if (d.month, d.day) == (2, 29):
        return 59
    if d.month > 2 and (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        doy -= 1
    return min(max(doy, 1), 365)


def main() -> int:
    if not HISTORY_PATH.exists():
        print(f"WARNING: {HISTORY_PATH} missing — run backfill_history.py once first. "
              f"Skipping append.", file=sys.stderr)
        return 0
    if not STATUS_PATH.exists():
        print(f"WARNING: {STATUS_PATH} missing — skipping history append.", file=sys.stderr)
        return 0

    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    recent_days = history.get("recent_days", 760)
    status_inds = status.get("indicators", {})
    hist_inds = history.setdefault("indicators", {})

    changed = False
    for key, rnd in TRACKED.items():
        s_ind = status_inds.get(key)
        h_ind = hist_inds.get(key)
        if not s_ind or not h_ind:
            continue  # nothing published yet, or not in history file

        value = parse_display_value(s_ind.get("display_value"))
        if value is None:
            print(f"WARNING: {key} had no usable display_value; skipped.", file=sys.stderr)
            continue
        value = round(value, rnd)
        d_str = observed_date(s_ind)

        recent = h_ind.setdefault("recent", [])
        # Upsert today's point (replace if same date already present).
        if recent and recent[-1].get("d") == d_str:
            if recent[-1].get("v") != value:
                recent[-1]["v"] = value
                changed = True
        else:
            recent.append({"d": d_str, "v": value})
            changed = True

        # Trim to the rolling window.
        if len(recent) > recent_days:
            del recent[: len(recent) - recent_days]

        # Light envelope touch-up for this day-of-year slot.
        try:
            slot = str(day_of_year_key(date.fromisoformat(d_str)))
            stats = h_ind.setdefault("doy_stats", {}).get(slot)
            if stats is None:
                h_ind["doy_stats"][slot] = {"min": value, "med": value, "max": value}
            else:
                stats["min"] = round(min(stats["min"], value), rnd)
                stats["max"] = round(max(stats["max"], value), rnd)
                # nudge median toward the new obs without a full rebuild
                stats["med"] = round(median([stats["min"], stats["med"], stats["max"], value]), rnd)
            h_ind["record_end"] = d_str
            changed = True
        except Exception as exc:
            print(f"WARNING: envelope touch-up skipped for {key} ({exc}).", file=sys.stderr)

    if changed:
        HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
        print("history-stats.json updated.")
    else:
        print("No history changes to write.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
