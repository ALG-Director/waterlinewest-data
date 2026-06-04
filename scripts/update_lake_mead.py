#!/usr/bin/env python3
"""
DAILY UPDATER: publish Lake Mead's live values into docs/snowpack-status.json.

Sibling of update_lake_powell.py. Reads two USBR Upper Colorado HydroData
series for Lake Mead (reservoir 921):

    datatype 49 -> Pool Elevation (ft)   -> status key "mead_elevation"
    datatype 42 -> Total Release   (cfs) -> status key "hoover_release"

Each series is JSON: {"columns":[...], "data":[["YYYY-MM-DD", value], ...]},
sorted ascending, so the newest observation is the last element. We take the
latest point as the current value and the preceding 7 daily points as the
trend window (matching the powell_elevation / grand_canyon_inflow blocks).

Runs in the daily workflow BEFORE update_history_append.py, which already
lists "mead_elevation" and "hoover_release" in its TRACKED dict and will pick
these up automatically once they're published here.

Soft-fail: if a fetch or parse fails, it logs and exits 0 so the rest of the
workflow still commits whatever else succeeded.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

STATUS_PATH = Path("docs/snowpack-status.json")
ARIZONA = ZoneInfo("America/Phoenix")

RES_ID = "921"
DASHBOARD_URL = f"https://www.usbr.gov/uc/water/hydrodata/reservoir_data/{RES_ID}/dashboard.html"

# (status_key, datatype_id, kind, rounding, band, band_field, note_unit)
SERIES = [
    ("mead_elevation", "49", "elevation", 2, 0.5, "trend_band_ft", "ft"),
    ("hoover_release", "42", "release", 0, 8.0, "trend_band_pct", "cfs"),
]

LABELS = {"mead_elevation": "Lake Mead Elevation", "hoover_release": "Hoover Dam Release"}
NOTES = {"mead_elevation": "ft above sea level", "hoover_release": "cfs"}


def api_url(datatype_id: str) -> str:
    return f"https://www.usbr.gov/uc/water/hydrodata/reservoir_data/{RES_ID}/json/{datatype_id}.json"


def fetch_series(url: str, attempts: int = 3) -> list:
    """Return the ascending [[date, value], ...] data array from a HydroData JSON.

    Retries with backoff and a generous timeout — the release file (period of
    record) can be slow, and a single slow moment shouldn't skip the series.
    """
    delay = 2.0
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WaterLineWest/1.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = payload.get("data", [])
            # Keep only well-formed [date, numeric] rows.
            return [(str(d)[:10], float(v)) for d, v in data if v is not None]
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last:
        raise last


def fmt(value: float, rnd: int) -> str:
    """Thousands-separated display string: 1052.49 -> '1,052.49'; 8535.0 -> '8,535'."""
    return f"{value:,.{rnd}f}"


def build_block(key: str, datatype_id: str, kind: str, rnd: int,
                band: float, band_field: str, points: list) -> dict | None:
    """Build one status indicator block from the series points."""
    if not points:
        print(f"WARNING: {key} series empty; skipped.", file=sys.stderr)
        return None

    obs_date, current = points[-1]
    current = round(current, rnd)

    # Trend window = the 7 daily points immediately before the latest obs.
    window = points[-8:-1] if len(points) >= 8 else points[:-1]
    block = {
        "label": LABELS[key],
        "display_value": fmt(current, rnd),
        "note": NOTES[key],
        "timestamp": f"Observed {obs_date}",
        "source_name": "U.S. Bureau of Reclamation",
        "source_url": DASHBOARD_URL,
        "api_url": api_url(datatype_id),
        "update_cadence": "Daily",
        "provisional": True,
        "usbr_site_id": RES_ID,
        "usbr_datatype_id": datatype_id,
        "usbr_observed_date": obs_date,
    }

    if not window:
        block["trend"] = "steady"
        block["trend_note"] = "no recent window"
        return block

    avg = round(mean(v for _, v in window), rnd)
    block[band_field] = band
    block["avg_7day"] = avg
    block["avg_7day_display"] = fmt(avg, rnd)
    block["trend_window_days"] = 7
    block["trend_window_start"] = window[0][0]
    block["trend_window_end"] = window[-1][0]

    if kind == "elevation":
        diff = round(current - avg, 2)
        if abs(diff) <= band:
            trend, word = "steady", "near"
        elif diff > 0:
            trend, word = "rising", "above"
        else:
            trend, word = "falling", "below"
        block["ft_vs_avg"] = diff
        block["trend"] = trend
        block["trend_note"] = f"{abs(diff):.2f} ft {word} 7-day avg"
    else:  # release / flow, percent-based
        pct = round((current - avg) / avg * 100, 1) if avg else 0.0
        if abs(pct) <= band:
            trend, word = "steady", "near"
        elif pct > 0:
            trend, word = "rising", "above"
        else:
            trend, word = "falling", "below"
        block["pct_vs_avg"] = pct
        block["trend"] = trend
        block["trend_note"] = f"{abs(pct):.1f}% {word} 7-day avg"

    return block


def main() -> int:
    if not STATUS_PATH.exists():
        print(f"WARNING: {STATUS_PATH} missing — skipping Lake Mead update.", file=sys.stderr)
        return 0

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    indicators = status.setdefault("indicators", {})

    changed = False
    for key, datatype_id, kind, rnd, band, band_field, _unit in SERIES:
        try:
            points = fetch_series(api_url(datatype_id))
            block = build_block(key, datatype_id, kind, rnd, band, band_field, points)
        except Exception as exc:
            print(f"WARNING: {key} update failed ({exc}); leaving prior value.", file=sys.stderr)
            continue
        if block is None:
            continue
        indicators[key] = block
        changed = True
        print(f"{key}: {block['display_value']} {block['note']} "
              f"({block.get('trend', '?')}) @ {block['usbr_observed_date']}")

    if changed:
        now = datetime.now(ARIZONA)
        status.setdefault("automation", {})
        status["automation"]["script_mead"] = "scripts/update_lake_mead.py"
        status["automation"]["mead_source"] = (
            "USBR Upper Colorado HydroData (reservoir 921, datatypes 49 & 42)"
        )
        status["automation"]["mead_last_run_display"] = now.strftime(
            "%B %-d, %Y, %-I:%M %p Arizona time"
        )
        STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        print("snowpack-status.json updated with Lake Mead values.")
    else:
        print("No Lake Mead changes written.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
