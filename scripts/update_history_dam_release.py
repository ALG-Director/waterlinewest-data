#!/usr/bin/env python3
"""
Phase 2D: Add the Dam Release history series to docs/history-stats.json.

What this builds:
  The grid/charts page reads history-stats.json. Each indicator there carries
  a day-of-year envelope (min / median / max across the record) plus the most
  recent ~760 daily values. This script computes that block for Glen Canyon
  Dam "Power Release" (water through the turbines) and MERGES it in under the
  key 'powerplant_release', leaving every other indicator untouched.

Source (same portal/format as the Powell elevation history, datatype 49):
  Reservoir 919 -> Lake Powell (Glen Canyon Dam)
  Datatype  39  -> Power Release, cfs
  https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/39.json
  Shape: {"columns": ["datetime", "power release"], "data": [["YYYY-MM-DD", v], ...]}

Envelope window:
  ENVELOPE_START_DATE trims the record before computing the day-of-year band.
  Default '2000-01-01' matches the cfs siblings (Lees Ferry, tributaries) and
  excludes the 1963-80s reservoir-filling era (long stretches of zeros) that
  would otherwise pin the band floor at 0. Set to None for the full record.

How to run:
  Add as a workflow step AFTER whatever builds history-stats.json (and before
  the commit step), so it merges into the freshly built file each run. If the
  fetch fails, it soft-fails: leaves the existing block in place and exits 0.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

USBR_POWER_RELEASE_URL = (
    "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/39.json"
)
HISTORY_PATH = Path("docs/history-stats.json")
INDICATOR_KEY = "powerplant_release"
SOURCE_LABEL = "USBR reservoir 919 datatype 39 Lake Powell power release"
UNITS = "cfs"
ENVELOPE_START_DATE = "2000-01-01"   # set to None to use the full record
DEFAULT_RECENT_DAYS = 760            # overridden by the file's own recent_days
AZ = ZoneInfo("America/Phoenix")


def load_json_from_url(url: str, attempts: int = 4, timeout: int = 90) -> dict:
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = RuntimeError(f"USBR HTTP {exc.code}: {exc.reason}")
        except (URLError, TimeoutError) as exc:
            last_error = RuntimeError(f"USBR request failed: {getattr(exc, 'reason', exc)}")
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise last_error


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def doy_no_leap(d: date) -> int | None:
    """Day-of-year on a fixed 365-day calendar. Feb 29 -> None (skipped)."""
    if d.month == 2 and d.day == 29:
        return None
    doy = d.timetuple().tm_yday
    # In leap years, shift everything after Feb 29 back by one so Mar 1 == 60.
    is_leap = (d.year % 4 == 0 and d.year % 100 != 0) or (d.year % 400 == 0)
    if is_leap and doy > 60:
        doy -= 1
    return doy


def clean_rows(rows: list) -> list:
    """Return [(date, float)] for rows with a usable numeric value."""
    out = []
    for r in rows:
        if len(r) < 2 or r[1] is None:
            continue
        try:
            d = parse_date(r[0])
            v = float(r[1])
        except (ValueError, TypeError):
            continue
        out.append((d, v))
    out.sort(key=lambda t: t[0])
    return out


def build_block(rows: list, recent_days: int) -> dict:
    pairs = clean_rows(rows)
    if not pairs:
        raise RuntimeError("power-release feed had no usable numeric values")

    # --- recent series: last N daily values (uses full record, like siblings) ---
    recent = [{"d": d.isoformat(), "v": float(round(v))} for d, v in pairs[-recent_days:]]

    # --- envelope window (trim filling era by default) ---
    if ENVELOPE_START_DATE:
        cutoff = parse_date(ENVELOPE_START_DATE)
        env = [(d, v) for d, v in pairs if d >= cutoff]
    else:
        env = pairs
    if not env:
        env = pairs  # safety: never end up empty

    buckets: dict[int, list] = {}
    for d, v in env:
        k = doy_no_leap(d)
        if k is None:
            continue
        buckets.setdefault(k, []).append(v)

    doy_stats = {}
    for k in range(1, 366):
        vals = buckets.get(k)
        if not vals:
            continue
        doy_stats[str(k)] = {
            "min": float(round(min(vals))),
            "med": float(round(statistics.median(vals))),
            "max": float(round(max(vals))),
        }

    start_d, end_d = env[0][0], env[-1][0]
    years = round((end_d - start_d).days / 365.25, 1)

    return {
        "units": UNITS,
        "record_start": start_d.isoformat(),
        "record_end": end_d.isoformat(),
        "years_of_record": years,
        "recent": recent,
        "doy_stats": doy_stats,
        "source": SOURCE_LABEL,
    }


def main() -> int:
    if not HISTORY_PATH.exists():
        print(f"ERROR: {HISTORY_PATH} not found (run from the repo root, "
              f"after the main history build).", file=sys.stderr)
        return 1

    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    recent_days = int(history.get("recent_days", DEFAULT_RECENT_DAYS))

    try:
        payload = load_json_from_url(USBR_POWER_RELEASE_URL)
    except Exception as exc:  # noqa: BLE001 - intentional soft fail
        print(f"WARNING: could not reach Reclamation power-release feed ({exc}). "
              f"Leaving existing '{INDICATOR_KEY}' history untouched.", file=sys.stderr)
        return 0

    rows = payload.get("data", [])
    if not rows:
        print("WARNING: USBR response had no data rows; leaving history untouched.",
              file=sys.stderr)
        return 0

    block = build_block(rows, recent_days)
    history.setdefault("indicators", {})[INDICATOR_KEY] = block

    HISTORY_PATH.write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Merged Dam Release history: {block['record_start']} -> {block['record_end']} "
          f"({block['years_of_record']} yrs), {len(block['recent'])} recent days, "
          f"{len(block['doy_stats'])} day-of-year buckets.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
