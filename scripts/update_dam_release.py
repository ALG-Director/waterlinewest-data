#!/usr/bin/env python3
"""
Phase 2C: Update the Dam Release value in docs/snowpack-status.json.

What this is:
  Glen Canyon Dam "Power Release" — the daily flow routed through the
  powerplant's turbines, in cubic feet per second (cfs). It is the closest
  daily, public read on how hard the dam is generating. (Note: release is a
  proxy for generation, not a meter of megawatt-hours — as the lake drops, the
  same flow makes less power because there is less head behind it.)

Source:
  U.S. Bureau of Reclamation — Upper Colorado HydroData portal
  Reservoir: 919  -> Lake Powell (Glen Canyon Dam)
  Datatype:  39   -> Power Release, cfs
  URL pattern is identical to the pool-elevation feed (datatype 49) that
  scripts/update_lake_powell.py already uses, so the JSON shape is the same:
      {"columns": ["datetime", "power release"], "data": [["YYYY-MM-DD", val], ...]}

Indicator written:
  indicators.powerplant_release   (the page binds this to the "Dam Release" pill)

Hardened behavior (mirrors update_lake_powell.py):
  * Retries the Reclamation feed a few times before giving up.
  * If Reclamation is unreachable after the retries, this script SOFT-FAILS:
    it prints a warning, leaves the existing value untouched, and exits 0
    (success) so the rest of the workflow (other updaters + the commit) runs.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# Reservoir 919 = Lake Powell; datatype 39 = Power Release (cfs)
USBR_POWER_RELEASE_URL = (
    "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/39.json"
)
STATUS_PATH = Path("docs/snowpack-status.json")
INDICATOR_KEY = "powerplant_release"
AZ = ZoneInfo("America/Phoenix")

# Trend is computed by comparing the latest reading to one about a week earlier.
TREND_LOOKBACK_DAYS = 7
# Deadband so tiny day-to-day wiggles read as "steady" rather than flapping.
TREND_DEADBAND_CFS = 150.0


def pretty_datetime(dt: datetime) -> str:
    dt_az = dt.astimezone(AZ)
    month = dt_az.strftime("%B")
    day = dt_az.day
    year = dt_az.year
    hour = dt_az.strftime("%I").lstrip("0") or "0"
    minute = dt_az.strftime("%M")
    ampm = dt_az.strftime("%p")
    return f"{month} {day}, {year}, {hour}:{minute} {ampm} Arizona time"


def pretty_date(d: datetime) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def load_json_from_url(url: str, attempts: int = 4, timeout: int = 60) -> dict:
    """Fetch JSON with retries. Raises only after all attempts fail."""
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = RuntimeError(
                f"USBR request failed with HTTP {exc.code}: {exc.reason}"
            )
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"USBR request failed: {reason}")
        if attempt < attempts:
            time.sleep(5 * attempt)  # 5s, 10s, 15s between tries
    raise last_error


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def latest_valid_index(rows: list) -> int:
    """Index of the most recent row that has a real (non-null) numeric value."""
    for i in range(len(rows) - 1, -1, -1):
        val = rows[i][1] if len(rows[i]) > 1 else None
        if val is not None:
            try:
                float(val)
                return i
            except (TypeError, ValueError):
                continue
    raise RuntimeError("USBR power-release feed had no usable numeric values.")


def value_about_n_days_before(rows: list, end_index: int, days: int):
    """
    Find a valid value ~`days` before rows[end_index], walking backward to skip
    any null gaps. Returns the float value, or None if nothing suitable is found.
    """
    end_date = parse_date(rows[end_index][0])
    for i in range(end_index - 1, -1, -1):
        try:
            d = parse_date(rows[i][0])
        except (ValueError, IndexError):
            continue
        if (end_date - d).days < days:
            continue
        val = rows[i][1] if len(rows[i]) > 1 else None
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def compute_trend(latest: float, prior):
    """Return (trend_word, trend_note) using a small deadband."""
    if prior is None:
        return None, None
    diff = latest - prior
    if abs(diff) < TREND_DEADBAND_CFS:
        return "steady", "About the same as a week ago"
    direction = "Up" if diff > 0 else "Down"
    word = "rising" if diff > 0 else "falling"
    return word, f"{direction} about {abs(round(diff)):,} cfs vs a week ago"


def main() -> int:
    now = datetime.now(tz=AZ)

    # --- Fetch (soft-fail so a Reclamation outage never blocks the workflow) ---
    try:
        payload = load_json_from_url(USBR_POWER_RELEASE_URL)
    except Exception as exc:  # noqa: BLE001 - intentional soft fail
        print(
            f"WARNING: could not reach Reclamation power-release feed "
            f"({exc}). Leaving existing '{INDICATOR_KEY}' value untouched.",
            file=sys.stderr,
        )
        return 0

    rows = payload.get("data", [])
    if not rows:
        print("WARNING: USBR response had no data rows; leaving value untouched.",
              file=sys.stderr)
        return 0

    idx = latest_valid_index(rows)
    observed_date = parse_date(rows[idx][0])
    latest_value = float(rows[idx][1])

    prior_value = value_about_n_days_before(rows, idx, TREND_LOOKBACK_DAYS)
    trend_word, trend_note = compute_trend(latest_value, prior_value)

    display_value = f"{round(latest_value):,}"
    observed_display = f"Observed {pretty_date(observed_date)}"

    # --- Read existing status file ---
    if not STATUS_PATH.exists():
        print(f"ERROR: {STATUS_PATH} not found (run from the repo root).",
              file=sys.stderr)
        return 1

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    indicators = status.setdefault("indicators", {})
    block = indicators.setdefault(INDICATOR_KEY, {})

    # --- Update only this indicator ---
    block["label"] = "Dam Release"
    block["display_value"] = display_value
    block["note"] = "cfs"
    block["timestamp"] = observed_display
    block["provisional"] = True
    if trend_word:
        block["trend"] = trend_word
        block["trend_note"] = trend_note
    else:
        block.pop("trend", None)
        block.pop("trend_note", None)

    # Refresh the file-level "last checked" stamp the header reads.
    status["site_last_checked_display"] = pretty_datetime(now)

    STATUS_PATH.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    trend_msg = f" ({trend_word})" if trend_word else ""
    print(f"Updated Dam Release to {display_value} cfs — {observed_display}{trend_msg}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
