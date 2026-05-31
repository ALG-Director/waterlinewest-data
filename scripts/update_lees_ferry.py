#!/usr/bin/env python3
"""
Phase 2C: Update Lees Ferry flow in docs/snowpack-status.json, with a
rolling 7-day trend.

Source:
  USGS Instantaneous Values service
  Site: 09380000 — Colorado River at Lees Ferry, AZ
  Parameter: 00060 — Discharge, cubic feet per second

What it writes:
  * Current flow (latest instantaneous reading)
  * 7-day rolling average (mean of the last 7 daily means; the window slides
    forward with each run so it is always "today vs the trailing week")
  * trend = "rising" / "falling" / "steady" using an 8% band
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

# Latest reading (real-time)
USGS_IV_URL = (
    "https://waterservices.usgs.gov/nwis/iv/"
    "?format=json&sites=09380000&parameterCd=00060&siteStatus=all"
)
# Daily mean values for the past 7 days (small, clean, one number per day)
USGS_DV_URL = (
    "https://waterservices.usgs.gov/nwis/dv/"
    "?format=json&sites=09380000&parameterCd=00060&statCd=00003&period=P8D&siteStatus=all"
)
STATUS_PATH = Path("docs/snowpack-status.json")
AZ = ZoneInfo("America/Phoenix")

# How far current flow must differ from the 7-day average before the arrow
# changes from "steady". Tune this one number to taste.
TREND_BAND_PCT = 8.0


def pretty_datetime(dt: datetime) -> str:
    dt_az = dt.astimezone(AZ)
    month = dt_az.strftime("%B")
    day = dt_az.day
    year = dt_az.year
    hour = dt_az.strftime("%I").lstrip("0") or "0"
    minute = dt_az.strftime("%M")
    ampm = dt_az.strftime("%p")
    return f"{month} {day}, {year}, {hour}:{minute} {ampm} Arizona time"


def load_json_from_url(url: str, attempts: int = 3, timeout: int = 45) -> dict:
    request = Request(url, headers={"User-Agent": "WaterLineWest-Data/1.0"})
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = RuntimeError(f"USGS request failed with HTTP {exc.code}: {exc.reason}")
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"USGS request failed: {reason}")
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise last_error


def extract_latest_discharge(payload: dict):
    series = payload.get("value", {}).get("timeSeries", [])
    if not series:
        raise RuntimeError("USGS response did not include any timeSeries records.")
    chosen = None
    for item in series:
        codes = item.get("variable", {}).get("variableCode", [])
        if any(c.get("value") == "00060" for c in codes):
            chosen = item
            break
    if chosen is None:
        chosen = series[0]
    groups = chosen.get("values", [])
    if not groups or not groups[0].get("value"):
        raise RuntimeError("USGS response did not include discharge values.")
    latest = groups[0]["value"][-1]
    raw_value = latest.get("value")
    raw_time = latest.get("dateTime")
    if raw_value is None or raw_time is None:
        raise RuntimeError("Latest USGS value was missing value or dateTime.")
    discharge_cfs = float(raw_value)
    observed_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    return discharge_cfs, observed_dt


def extract_daily_means(payload: dict):
    """Return a list of (date_str, mean_cfs) from the USGS daily-values feed."""
    series = payload.get("value", {}).get("timeSeries", [])
    if not series:
        return []
    groups = series[0].get("values", [])
    if not groups or not groups[0].get("value"):
        return []
    out = []
    for pt in groups[0]["value"]:
        v, d = pt.get("value"), pt.get("dateTime")
        if v is None or d is None:
            continue
        try:
            out.append((str(d)[:10], float(v)))
        except ValueError:
            continue
    return out


def compute_trend(current_cfs: float, daily_means: list):
    """Return (trend, avg, pct, n_days, window_start, window_end) or None."""
    # Use the most recent 7 daily means as the rolling window.
    pts = daily_means[-7:]
    if len(pts) < 3:  # need a few days for a meaningful average
        return None
    values = [v for _, v in pts]
    avg = mean(values)
    if avg == 0:
        return None
    pct = (current_cfs - avg) / avg * 100.0
    if pct > TREND_BAND_PCT:
        trend = "rising"
    elif pct < -TREND_BAND_PCT:
        trend = "falling"
    else:
        trend = "steady"
    return trend, avg, pct, len(pts), pts[0][0], pts[-1][0]


def main() -> int:
    if not STATUS_PATH.exists():
        raise RuntimeError(f"Missing status file: {STATUS_PATH}")

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))

    payload = load_json_from_url(USGS_IV_URL)
    discharge_cfs, observed_dt = extract_latest_discharge(payload)

    # Trend is a nice-to-have: if the daily feed is unavailable, keep going
    # and just omit the trend rather than failing the whole run.
    trend_info = None
    try:
        dv = load_json_from_url(USGS_DV_URL)
        trend_info = compute_trend(discharge_cfs, extract_daily_means(dv))
    except Exception as exc:
        print(f"WARNING: Lees Ferry 7-day trend skipped ({exc}).", file=sys.stderr)

    now_az = datetime.now(tz=AZ)
    display_value = f"{discharge_cfs:,.0f}"
    observed_display = "Observed " + pretty_datetime(observed_dt)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)
    status.setdefault("automation", {})
    status["automation"].update({
        "phase": "2C",
        "updated_indicator": "lees_ferry_flow",
        "script": "scripts/update_lees_ferry.py",
        "source": "USGS Instantaneous Values service",
        "last_run_display": pretty_datetime(now_az),
    })

    indicators = status.setdefault("indicators", {})
    lees = indicators.setdefault("lees_ferry_flow", {})
    lees.update({
        "label": "Lees Ferry Flow",
        "display_value": display_value,
        "note": "cfs",
        "timestamp": observed_display,
        "source_name": "USGS Water Data",
        "source_url": "https://waterdata.usgs.gov/monitoring-location/09380000/",
        "api_url": USGS_IV_URL,
        "update_cadence": "Near real time where available",
        "provisional": True,
        "usgs_site_no": "09380000",
        "usgs_parameter_cd": "00060",
        "usgs_observed_datetime": observed_dt.isoformat(),
    })

    if trend_info:
        trend, avg, pct, n_days, w_start, w_end = trend_info
        direction = {"rising": "above", "falling": "below", "steady": "near"}[trend]
        lees.update({
            "trend": trend,
            "trend_band_pct": TREND_BAND_PCT,
            "avg_7day": round(avg),
            "avg_7day_display": f"{round(avg):,}",
            "pct_vs_avg": round(pct, 1),
            "trend_note": f"{abs(round(pct,1))}% {direction} 7-day avg",
            "trend_window_days": n_days,
            "trend_window_start": w_start,
            "trend_window_end": w_end,
        })
    else:
        # Clear any stale trend so the page doesn't show old info.
        for k in ("trend", "pct_vs_avg", "trend_note"):
            lees.pop(k, None)

    status["source_line"] = (
        "Sources: NRCS/CAP snowpack reporting · NOAA CBRFC · "
        "U.S. Bureau of Reclamation · USGS Water Data. "
        "Lees Ferry flow, tributary inflow, and Lake Powell elevation update automatically from USGS and Reclamation; snowpack and forecast values remain curated. "
        "Provisional values may be revised."
    )

    STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    extra = ""
    if trend_info:
        extra = f" | trend: {trend_info[0]} ({trend_info[2]:+.1f}% vs 7-day avg {round(trend_info[1]):,})"
    print(f"Updated Lees Ferry flow to {display_value} cfs — {observed_display}{extra}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
