#!/usr/bin/env python3
"""
Phase 2E: Update Grand Canyon flow in docs/snowpack-status.json, plus a
"flow vs median" reading — both pills for the Chapter 4 (River Flow) page.

This is a twin of update_lees_ferry.py (same USGS Instantaneous Values call,
same 7-day rolling trend with an 8% band, same field shape), pointed at:
  Site: 09402500 — Colorado River near Grand Canyon, AZ
  Parameter: 00060 — Discharge, cubic feet per second

It writes TWO indicators:
  * grand_canyon_inflow      — current flow (cfs), with 7-day trend
  * grand_canyon_pct_median  — current flow as a percent of the historical
                               median for today's day-of-year. The median is
                               read from docs/history-stats.json (the same
                               day-of-year band the chart draws), using the
                               leap-adjusted day-of-year the history builder uses.

Failure behavior (matches update_lees_ferry.py):
  * The live gauge fetch is required — if USGS is unreachable the run fails,
    exactly like Lees Ferry.
  * The 7-day trend is a nice-to-have — skipped (not fatal) if the daily feed
    is down.
  * The percent-of-median is also a nice-to-have — skipped (not fatal) if the
    history file or today's median is unavailable; the existing value is left
    in place rather than cleared.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SITE = "09402500"
USGS_IV_URL = (
    "https://waterservices.usgs.gov/nwis/iv/"
    f"?format=json&sites={SITE}&parameterCd=00060&siteStatus=all"
)
USGS_DV_URL = (
    "https://waterservices.usgs.gov/nwis/dv/"
    f"?format=json&sites={SITE}&parameterCd=00060&statCd=00003&period=P8D&siteStatus=all"
)
STATUS_PATH = Path("docs/snowpack-status.json")
HISTORY_PATH = Path("docs/history-stats.json")
GAUGE_KEY = "grand_canyon_inflow"
PCT_KEY = "grand_canyon_pct_median"
AZ = ZoneInfo("America/Phoenix")
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
    pts = daily_means[-7:]
    if len(pts) < 3:
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


def doy_no_leap(d: date):
    """Day-of-year on a fixed 365-day calendar (matches the history builder)."""
    if d.month == 2 and d.day == 29:
        return 59  # fold leap day onto Feb 28's bucket
    doy = d.timetuple().tm_yday
    is_leap = (d.year % 4 == 0 and d.year % 100 != 0) or (d.year % 400 == 0)
    if is_leap and doy > 60:
        doy -= 1
    return doy


def grand_canyon_median(observed_az_date: date):
    """Today's day-of-year median for grand_canyon_inflow from history-stats.json."""
    if not HISTORY_PATH.exists():
        return None, None
    hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    stats = (
        hist.get("indicators", {})
        .get("grand_canyon_inflow", {})
        .get("doy_stats", {})
    )
    doy = doy_no_leap(observed_az_date)
    rec = stats.get(str(doy))
    if not rec or rec.get("med") in (None, 0):
        return None, doy
    return float(rec["med"]), doy


def main() -> int:
    if not STATUS_PATH.exists():
        raise RuntimeError(f"Missing status file: {STATUS_PATH}")

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))

    # --- live gauge (required) ---
    payload = load_json_from_url(USGS_IV_URL)
    discharge_cfs, observed_dt = extract_latest_discharge(payload)

    # --- 7-day trend (nice-to-have) ---
    trend_info = None
    try:
        dv = load_json_from_url(USGS_DV_URL)
        trend_info = compute_trend(discharge_cfs, extract_daily_means(dv))
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Grand Canyon 7-day trend skipped ({exc}).", file=sys.stderr)

    now_az = datetime.now(tz=AZ)
    display_value = f"{discharge_cfs:,.0f}"
    observed_display = "Observed " + pretty_datetime(observed_dt)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)
    status.setdefault("automation", {}).update({
        "phase": "2E",
        "updated_indicator": GAUGE_KEY,
        "script": "scripts/update_grand_canyon.py",
        "source": "USGS Instantaneous Values service",
        "last_run_display": pretty_datetime(now_az),
    })

    indicators = status.setdefault("indicators", {})
    gc = indicators.setdefault(GAUGE_KEY, {})
    gc.update({
        "label": "Grand Canyon Flow",
        "display_value": display_value,
        "note": "cfs",
        "timestamp": observed_display,
        "source_name": "USGS Water Data",
        "source_url": f"https://waterdata.usgs.gov/monitoring-location/{SITE}/",
        "api_url": USGS_IV_URL,
        "update_cadence": "Near real time where available",
        "provisional": True,
        "usgs_site_no": SITE,
        "usgs_parameter_cd": "00060",
        "usgs_observed_datetime": observed_dt.isoformat(),
    })

    if trend_info:
        trend, avg, pct, n_days, w_start, w_end = trend_info
        direction = {"rising": "above", "falling": "below", "steady": "near"}[trend]
        gc.update({
            "trend": trend,
            "trend_band_pct": TREND_BAND_PCT,
            "avg_7day": round(avg),
            "avg_7day_display": f"{round(avg):,}",
            "pct_vs_avg": round(pct, 1),
            "trend_note": f"{abs(round(pct, 1))}% {direction} 7-day avg",
            "trend_window_days": n_days,
            "trend_window_start": w_start,
            "trend_window_end": w_end,
        })
    else:
        for k in ("trend", "pct_vs_avg", "trend_note"):
            gc.pop(k, None)

    # --- flow vs median (nice-to-have) ---
    try:
        med, doy = grand_canyon_median(observed_dt.astimezone(AZ).date())
        if med:
            pct_med = discharge_cfs / med * 100.0
            indicators[PCT_KEY] = {
                "label": "Flow vs Median",
                "display_value": f"{round(pct_med)}%",
                "note": "of median for this date",
                "timestamp": observed_display,
                "median_cfs": round(med),
                "median_display": f"{round(med):,}",
                "current_cfs": round(discharge_cfs),
                "day_of_year": doy,
                "provisional": True,
            }
            pct_msg = f" | vs median: {round(pct_med)}% (median {round(med):,} cfs)"
        else:
            print("WARNING: no Grand Canyon median for today; percent left unchanged.",
                  file=sys.stderr)
            pct_msg = ""
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: flow-vs-median skipped ({exc}); existing value left in place.",
              file=sys.stderr)
        pct_msg = ""

    STATUS_PATH.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    extra = ""
    if trend_info:
        extra = f" | trend: {trend_info[0]} ({trend_info[2]:+.1f}% vs 7-day avg {round(trend_info[1]):,})"
    print(f"Updated Grand Canyon flow to {display_value} cfs — {observed_display}{extra}{pct_msg}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
