#!/usr/bin/env python3
"""
Phase 2C: Update Lake Powell pool elevation in docs/snowpack-status.json,
with a rolling 7-day trend (measured in FEET, not percent).

Source:
  U.S. Bureau of Reclamation — Upper Colorado HydroData portal
  Reservoir: 919 — Lake Powell (Glen Canyon Dam)
  Datatype: 49 — Pool Elevation, feet above mean sea level

Why feet, not percent: Powell sits near 3,527 ft and moves only inches to a
few feet per week, so a percentage band would never trigger. Reservoir trend
is conventionally tracked as change in feet.

Soft-fail: if Reclamation is unreachable after retries, keep the previous
value and exit 0 so the workflow still commits the other indicators.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

USBR_ELEVATION_URL = (
    "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/json/49.json"
)
STATUS_PATH = Path("docs/snowpack-status.json")
AZ = ZoneInfo("America/Phoenix")

# How many feet current elevation must differ from the 7-day average before
# the arrow leaves "steady". Tune to taste.
TREND_BAND_FT = 0.5


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
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = RuntimeError(f"USBR request failed with HTTP {exc.code}: {exc.reason}")
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"USBR request failed: {reason}")
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise last_error


def extract_elevation_series(payload: dict):
    """Return a list of (date, elevation_ft) in chronological order, nulls dropped."""
    rows = payload.get("data", [])
    out = []
    for row in rows:
        if not row or len(row) < 2:
            continue
        raw_date, raw_value = row[0], row[1]
        if raw_value is None or raw_date is None:
            continue
        try:
            elev = float(raw_value)
            d = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        out.append((d, elev))
    return out


def compute_trend(current_ft: float, series: list):
    """Use the most recent 7 daily elevations as the rolling window."""
    pts = series[-7:]
    if len(pts) < 3:
        return None
    avg = mean(e for _, e in pts)
    diff = current_ft - avg
    if diff > TREND_BAND_FT:
        trend = "rising"
    elif diff < -TREND_BAND_FT:
        trend = "falling"
    else:
        trend = "steady"
    return trend, avg, diff, len(pts), pts[0][0].date().isoformat(), pts[-1][0].date().isoformat()


def main() -> int:
    if not STATUS_PATH.exists():
        raise RuntimeError(f"Missing status file: {STATUS_PATH}")

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))

    try:
        payload = load_json_from_url(USBR_ELEVATION_URL)
        series = extract_elevation_series(payload)
        if not series:
            raise RuntimeError("USBR response had no usable elevation values.")
    except Exception as exc:
        print(f"WARNING: Lake Powell update skipped this run ({exc}). Keeping previous value.",
              file=sys.stderr)
        return 0

    observed_dt, elevation_ft = series[-1]
    trend_info = compute_trend(elevation_ft, series)

    now_az = datetime.now(tz=AZ)
    display_value = f"{elevation_ft:,.2f}"
    observed_display = "Observed " + pretty_date(observed_dt)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)
    status.setdefault("automation", {})
    status["automation"].update({
        "phase": "2C",
        "script_powell": "scripts/update_lake_powell.py",
        "powell_source": "USBR Upper Colorado HydroData (reservoir 919, datatype 49)",
        "powell_last_run_display": pretty_datetime(now_az),
    })

    indicators = status.setdefault("indicators", {})
    powell = indicators.setdefault("powell_elevation", {})
    powell.update({
        "label": "Lake Powell Elevation",
        "display_value": display_value,
        "note": "ft above sea level",
        "timestamp": observed_display,
        "source_name": "U.S. Bureau of Reclamation",
        "source_url": "https://www.usbr.gov/uc/water/hydrodata/reservoir_data/919/dashboard.html",
        "api_url": USBR_ELEVATION_URL,
        "update_cadence": "Daily",
        "provisional": True,
        "usbr_site_id": "919",
        "usbr_datatype_id": "49",
        "usbr_observed_date": observed_dt.date().isoformat(),
    })

    if trend_info:
        trend, avg, diff, n_days, w_start, w_end = trend_info
        direction = {"rising": "above", "falling": "below", "steady": "near"}[trend]
        powell.update({
            "trend": trend,
            "trend_band_ft": TREND_BAND_FT,
            "avg_7day": round(avg, 2),
            "avg_7day_display": f"{round(avg, 2):,.2f}",
            "ft_vs_avg": round(diff, 2),
            "trend_note": f"{abs(round(diff,2))} ft {direction} 7-day avg",
            "trend_window_days": n_days,
            "trend_window_start": w_start,
            "trend_window_end": w_end,
        })
    else:
        for k in ("trend", "ft_vs_avg", "trend_note"):
            powell.pop(k, None)

    STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    extra = ""
    if trend_info:
        extra = f" | trend: {trend_info[0]} ({trend_info[2]:+.2f} ft vs 7-day avg {round(trend_info[1],2)})"
    print(f"Updated Lake Powell elevation to {display_value} ft — {observed_display}{extra}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
