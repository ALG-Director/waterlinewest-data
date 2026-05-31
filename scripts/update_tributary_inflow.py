#!/usr/bin/env python3
"""
Phase 2C: Update Tributary Current Inflow in docs/snowpack-status.json, with a
rolling 7-day trend.

What "inflow" means here:
  Lake Powell has no single gauge at its edge -- the head of the reservoir sits
  in backwater, where flow cannot be rated (the gauge there reports only water-
  surface elevation, not discharge). The standard practice is to sum the last
  free-flowing gauges on each river arm before it slows into the lake. This
  script sums three USGS discharge gauges:

    09180500 -- Colorado River near Cisco, UT      (Colorado main-stem arm)
    09315000 -- Green River at Green River, UT      (Green arm)
    09379500 -- San Juan River near Bluff, UT       (San Juan arm)

  These three do not overlap (no double-counting) and together carry nearly all
  of the water entering Lake Powell. Parameter 00060 = discharge, cubic feet/sec.

  >>> To switch to a SINGLE-GAUGE definition (Colorado near Cisco only), just
  >>> reduce STATIONS to one entry. Everything else below adapts automatically.

What it writes (indicators["tributary_inflow"]):
  * Current composite flow (sum of the latest instantaneous reading per gauge)
  * 7-day rolling average of the composite daily sums (today vs trailing week)
  * trend = "rising" / "falling" / "steady" using an 8% band
  * components_cfs so you can see each gauge's contribution

Soft-fail: if USGS is unreachable, or if any required gauge is missing from the
real-time feed, the run keeps the previous value and exits 0 rather than
publishing a silently-undercounted inflow number.
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

# The gauges that make up the composite. Keys are USGS site numbers.
# Reduce this to one entry for a single-gauge ("Colorado near Cisco") definition.
STATIONS = {
    "09180500": "Colorado River near Cisco",
    "09315000": "Green River at Green River",
    "09379500": "San Juan River near Bluff",
}
SITE_STRING = ",".join(STATIONS)

# Latest reading (real-time), all gauges in one request.
USGS_IV_URL = (
    "https://waterservices.usgs.gov/nwis/iv/"
    f"?format=json&sites={SITE_STRING}&parameterCd=00060&siteStatus=all"
)
# Daily mean values for the past 8 days (one number per day per gauge).
USGS_DV_URL = (
    "https://waterservices.usgs.gov/nwis/dv/"
    f"?format=json&sites={SITE_STRING}&parameterCd=00060&statCd=00003&period=P8D&siteStatus=all"
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


def _site_no(item: dict):
    codes = item.get("sourceInfo", {}).get("siteCode", [])
    return codes[0].get("value") if codes else None


def _is_discharge(item: dict) -> bool:
    codes = item.get("variable", {}).get("variableCode", [])
    return any(c.get("value") == "00060" for c in codes)


def extract_latest_by_site(payload: dict) -> dict:
    """Return {site_no: (cfs, observed_dt)} for each gauge with a usable reading."""
    series = payload.get("value", {}).get("timeSeries", [])
    found = {}
    for item in series:
        if not _is_discharge(item):
            continue
        site_no = _site_no(item)
        if site_no not in STATIONS:
            continue
        groups = item.get("values", [])
        if not groups or not groups[0].get("value"):
            continue
        latest = groups[0]["value"][-1]
        raw_value, raw_time = latest.get("value"), latest.get("dateTime")
        if raw_value is None or raw_time is None:
            continue
        try:
            cfs = float(raw_value)
        except ValueError:
            continue
        if cfs < 0:  # -999999 is the USGS no-data sentinel
            continue
        observed_dt = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        found[site_no] = (cfs, observed_dt)
    return found


def extract_daily_means_by_site(payload: dict) -> dict:
    """Return {site_no: {date_str: mean_cfs}} from the USGS daily-values feed."""
    series = payload.get("value", {}).get("timeSeries", [])
    by_site = {}
    for item in series:
        if not _is_discharge(item):
            continue
        site_no = _site_no(item)
        if site_no not in STATIONS:
            continue
        groups = item.get("values", [])
        if not groups or not groups[0].get("value"):
            continue
        day_map = {}
        for pt in groups[0]["value"]:
            v, d = pt.get("value"), pt.get("dateTime")
            if v is None or d is None:
                continue
            try:
                val = float(v)
            except ValueError:
                continue
            if val < 0:
                continue
            day_map[str(d)[:10]] = val
        by_site[site_no] = day_map
    return by_site


def composite_daily_sums(by_site: dict) -> list:
    """List of (date_str, composite_sum) for dates present in EVERY gauge."""
    if not all(site in by_site for site in STATIONS):
        return []
    common_dates = set.intersection(*(set(by_site[site].keys()) for site in STATIONS))
    return [(d, sum(by_site[site][d] for site in STATIONS)) for d in sorted(common_dates)]


def compute_trend(current_cfs: float, daily_sums: list):
    """Return (trend, avg, pct, n_days, window_start, window_end) or None."""
    pts = daily_sums[-7:]  # most recent 7 complete composite days
    if len(pts) < 3:       # need a few days for a meaningful average
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

    # --- Current composite flow (soft-fail on any incompleteness) ---
    try:
        latest = extract_latest_by_site(load_json_from_url(USGS_IV_URL))
    except Exception as exc:
        print(f"WARNING: Tributary inflow update skipped this run ({exc}). "
              f"Keeping previous value.", file=sys.stderr)
        return 0

    missing = [site for site in STATIONS if site not in latest]
    if missing:
        names = ", ".join(f"{s} ({STATIONS[s]})" for s in missing)
        print(f"WARNING: Tributary inflow skipped — missing real-time data for {names}. "
              f"Not publishing an undercounted sum; keeping previous value.", file=sys.stderr)
        return 0

    components = {site: round(latest[site][0]) for site in STATIONS}
    current_composite = sum(latest[site][0] for site in STATIONS)
    # Label the composite as only as fresh as its STALEST gauge (honest "as of").
    observed_dt = min(latest[site][1] for site in STATIONS)

    # --- 7-day trend (nice-to-have; omit rather than fail the run) ---
    trend_info = None
    try:
        dv = load_json_from_url(USGS_DV_URL)
        trend_info = compute_trend(current_composite, composite_daily_sums(extract_daily_means_by_site(dv)))
    except Exception as exc:
        print(f"WARNING: Tributary inflow 7-day trend skipped ({exc}).", file=sys.stderr)

    now_az = datetime.now(tz=AZ)
    display_value = f"{current_composite:,.0f}"
    observed_display = "Observed " + pretty_datetime(observed_dt)

    status["site_last_checked"] = now_az.isoformat(timespec="seconds")
    status["site_last_checked_display"] = pretty_datetime(now_az)
    status.setdefault("automation", {})
    # Additive keys only -- do not clobber the Lees Ferry / Powell automation fields.
    status["automation"].update({
        "phase": "2C",
        "script_tributary": "scripts/update_tributary_inflow.py",
        "tributary_source": "USGS Instantaneous Values service (composite of "
                            + ", ".join(STATIONS) + ")",
        "tributary_last_run_display": pretty_datetime(now_az),
    })

    indicators = status.setdefault("indicators", {})
    trib = indicators.setdefault("tributary_inflow", {})
    trib.update({
        "label": "Tributary Inflow",
        "display_value": display_value,
        "note": "cfs",
        "timestamp": observed_display,
        "source_name": "USGS Water Data",
        "source_url": "https://waterdata.usgs.gov/monitoring-location/09180500/",
        "api_url": USGS_IV_URL,
        "update_cadence": "Near real time where available",
        "provisional": True,
        "composite": True,
        "usgs_sites": list(STATIONS.keys()),
        "usgs_site_names": dict(STATIONS),
        "usgs_parameter_cd": "00060",
        "components_cfs": components,
        "usgs_observed_datetime": observed_dt.isoformat(),
    })

    if trend_info:
        trend, avg, pct, n_days, w_start, w_end = trend_info
        direction = {"rising": "above", "falling": "below", "steady": "near"}[trend]
        trib.update({
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
        # Clear any stale trend so the page doesn't show old info.
        for k in ("trend", "pct_vs_avg", "trend_note"):
            trib.pop(k, None)

    STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    breakdown = " + ".join(f"{components[s]:,}" for s in STATIONS)
    extra = ""
    if trend_info:
        extra = f" | trend: {trend_info[0]} ({trend_info[2]:+.1f}% vs 7-day avg {round(trend_info[1]):,})"
    print(f"Updated Tributary inflow to {display_value} cfs ({breakdown}) — {observed_display}{extra}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
