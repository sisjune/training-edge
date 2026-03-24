"""Intervals.icu integration — auto-fetch CTL/ATL/TSB, wellness, activity metrics for seeding & validation."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from . import database


INTERVALS_BASE_URL = os.environ.get("INTERVALS_API_BASE", "https://intervals.icu/api/v1")


def _find_api_key() -> Optional[str]:
    """Find Intervals.icu API key from env, DB settings, or local state files."""
    # 1. Environment variable
    env_key = os.environ.get("INTERVALS_API_KEY", "").strip()
    if env_key:
        return env_key

    # 2. DB settings (set via web settings page)
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'intervals_api_key'"
            ).fetchone()
            if row and row[0]:
                return row[0].strip()
    except Exception:
        pass

    # 3. Local state file (inside project or container)
    for candidate in [
        Path(__file__).resolve().parent.parent / "state" / "intervals_api_key",
        Path("/data") / "intervals_api_key",
    ]:
        try:
            if candidate.exists():
                key = candidate.read_text(encoding="utf-8").strip()
                if key:
                    return key
        except Exception:
            pass

    # 4. OpenClaw skill's key file (development environment)
    try:
        skill_key = (
            Path(__file__).resolve().parents[3]
            / "skills" / "garmin-cycling-coach" / "state" / "intervals_api_key"
        )
        if skill_key.exists():
            key = skill_key.read_text(encoding="utf-8").strip()
            if key:
                return key
    except (IndexError, Exception):
        pass

    return None


def _auth() -> HTTPBasicAuth:
    key = _find_api_key()
    if not key:
        raise RuntimeError(
            "Intervals.icu API key not found. "
            "Set INTERVALS_API_KEY env var or run: "
            "garmin_coach.sh intervals-login"
        )
    return HTTPBasicAuth("API_KEY", key)


def _get(path: str, **params: Any) -> Any:
    """GET request to Intervals.icu API."""
    resp = requests.get(
        f"{INTERVALS_BASE_URL}{path}",
        params={k: v for k, v in params.items() if v is not None},
        auth=_auth(),
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Intervals.icu {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def is_configured() -> bool:
    """Check if Intervals.icu API key is available."""
    return _find_api_key() is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Wellness / Fitness data
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_wellness(on_date: str) -> Dict[str, Any]:
    """Fetch wellness data for a specific date (CTL, ATL, sleep, HRV, etc.)."""
    rows = _get("/athlete/0/wellness", oldest=on_date, newest=on_date)
    if not isinstance(rows, list) or not rows:
        return {}
    return _normalize_wellness(rows[0])


def fetch_wellness_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Fetch wellness data for a date range."""
    rows = _get("/athlete/0/wellness", oldest=start_date, newest=end_date)
    if not isinstance(rows, list):
        return []
    return [_normalize_wellness(r) for r in rows]


def fetch_today_fitness() -> Dict[str, Any]:
    """Fetch today's CTL/ATL/TSB — the most common use case for seeding."""
    return fetch_wellness(date.today().isoformat())


def _normalize_wellness(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Intervals.icu wellness data to our format."""
    def _num(val):
        if val is None:
            return None
        try:
            return round(float(val), 2)
        except (TypeError, ValueError):
            return None

    # Extract ride-specific eFTP from sportInfo
    ride_eftp = None
    ride_w_prime = None
    sport_info = row.get("sportInfo")
    if isinstance(sport_info, list):
        for item in sport_info:
            sport_type = str(item.get("type") or "").lower()
            if sport_type in ("ride", "cycling"):
                ride_eftp = _num(item.get("eftp"))
                ride_w_prime = _num(item.get("wPrime"))
                break

    sleep_secs = _num(row.get("sleepSecs"))
    return {
        "date": row.get("id"),
        "ctl": _num(row.get("ctl")),
        "atl": _num(row.get("atl")),
        "tsb": _num(row.get("ctl")) - _num(row.get("atl")) if _num(row.get("ctl")) is not None and _num(row.get("atl")) is not None else None,
        "ramp_rate": _num(row.get("rampRate")),
        "sleep_hours": round(sleep_secs / 3600, 2) if sleep_secs else None,
        "sleep_score": _num(row.get("sleepScore")),
        "readiness": _num(row.get("readiness")),
        "resting_hr": int(float(row.get("restingHR") or 0)) if row.get("restingHR") else None,
        "hrv": _num(row.get("hrv")),
        "steps": int(float(row.get("steps") or 0)) if row.get("steps") else None,
        "weight_kg": _num(row.get("weight")),
        "ride_eftp_w": ride_eftp,
        "ride_w_prime_j": ride_w_prime,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Activity data (for validation)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_activities(days: int = 30, limit: int = 50) -> List[Dict[str, Any]]:
    """Fetch recent activities from Intervals.icu."""
    end = date.today()
    start = end - timedelta(days=max(days - 1, 0))
    activities = _get(
        "/athlete/0/activities",
        oldest=start.isoformat(),
        newest=end.isoformat(),
        limit=max(limit, 10),
    )
    if not isinstance(activities, list):
        return []
    return [_normalize_activity(a) for a in activities]


def _normalize_activity(act: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Intervals.icu activity to validation-friendly format."""
    def _num(val):
        if val is None:
            return None
        try:
            return round(float(val), 2)
        except (TypeError, ValueError):
            return None

    return {
        "intervals_id": act.get("id"),
        "external_id": act.get("external_id"),  # Garmin activity ID
        "name": act.get("name"),
        "type": act.get("type"),
        "start_date_local": act.get("start_date_local"),
        "distance_m": _num(act.get("distance")),
        "moving_time_s": _num(act.get("moving_time")),
        "elapsed_time_s": _num(act.get("elapsed_time")),
        # Key validation fields
        "np": _num(act.get("icu_weighted_avg_watts")),
        "tss": _num(act.get("icu_training_load")),
        "intensity_pct": _num(act.get("icu_intensity")),
        "avg_power": _num(act.get("average_watts")),
        "avg_hr": _num(act.get("average_heartrate")),
        "max_hr": _num(act.get("max_heartrate")),
        "ctl": _num(act.get("icu_ctl")),
        "atl": _num(act.get("icu_atl")),
        "ftp": _num(act.get("icu_ftp")),
        "eftp": _num(act.get("icu_eftp")),
    }


def match_activity_by_garmin_id(garmin_id: int, intervals_activities: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find matching Intervals.icu activity by Garmin external ID."""
    garmin_id_str = str(garmin_id)
    for act in intervals_activities:
        if str(act.get("external_id", "")) == garmin_id_str:
            return act
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-seed and auto-validate
# ═══════════════════════════════════════════════════════════════════════════════

def auto_seed() -> Dict[str, Any]:
    """Automatically fetch CTL/ATL/FTP from Intervals.icu and seed into our database.

    Called during init — no manual input needed.
    """
    today_data = fetch_today_fitness()

    result = {"source": "intervals.icu", "date": date.today().isoformat()}

    with database.get_db() as conn:
        if today_data.get("ctl") is not None:
            database.set_setting(conn, "initial_ctl", str(today_data["ctl"]))
            result["ctl"] = today_data["ctl"]

        if today_data.get("atl") is not None:
            database.set_setting(conn, "initial_atl", str(today_data["atl"]))
            result["atl"] = today_data["atl"]

        if today_data.get("ride_eftp_w") is not None:
            database.set_setting(conn, "ftp", str(today_data["ride_eftp_w"]))
            result["ftp"] = today_data["ride_eftp_w"]

        if today_data.get("resting_hr") is not None:
            database.set_setting(conn, "resting_hr", str(today_data["resting_hr"]))
            result["resting_hr"] = today_data["resting_hr"]

        if today_data.get("weight_kg") is not None:
            database.set_setting(conn, "weight_kg", str(today_data["weight_kg"]))
            result["weight_kg"] = today_data["weight_kg"]

        # Also store the wellness data
        database.upsert_wellness(conn, today_data)

    return result


def auto_validate(days: int = 30) -> Dict[str, Any]:
    """Fetch Intervals.icu activities and validate against our computed metrics.

    Called after sync — automatically compares each activity.
    """
    from . import validator

    intervals_acts = fetch_activities(days=days)
    if not intervals_acts:
        return {"validated": 0, "message": "No Intervals.icu activities found"}

    validated = 0
    passed = 0
    details = []

    with database.get_db() as conn:
        our_activities = database.list_activities(conn, days=days)

    for our_act in our_activities:
        our_id = our_act.get("id")
        matched = match_activity_by_garmin_id(our_id, intervals_acts)
        if not matched:
            continue

        result = validator.validate_activity(our_id, matched)
        validated += 1
        if result.all_passed:
            passed += 1
        details.append({
            "id": our_id,
            "name": our_act.get("name"),
            "date": our_act.get("date"),
            "passed": result.all_passed,
            "summary": result.summary,
        })

    return {
        "validated": validated,
        "passed": passed,
        "pass_rate": round(passed / validated * 100, 1) if validated > 0 else 0,
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Planned Events — read training plan from Intervals.icu calendar
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_planned_events(oldest: str, newest: str) -> List[Dict[str, Any]]:
    """Fetch planned workout events from Intervals.icu calendar.

    Args:
        oldest: Start date (YYYY-MM-DD)
        newest: End date (YYYY-MM-DD)

    Returns:
        List of planned events with normalized fields:
        - date, name, type (Ride/Run/Swim/Other), category (WORKOUT/NOTE/...)
        - workout_doc (structured steps if available)
        - icu_training_load (planned TSS)
        - description
    """
    events = _get(f"/athlete/me/events", oldest=oldest, newest=newest)
    if not isinstance(events, list):
        return []

    result = []
    for e in events:
        start = e.get("start_date_local", "")
        result.append({
            "id": e.get("id"),
            "date": start[:10] if start else None,
            "name": e.get("name", ""),
            "type": e.get("type", ""),         # Ride, Run, Swim, WeightTraining, ...
            "category": e.get("category", ""),  # WORKOUT, NOTE, RACE, TARGET, ...
            "description": e.get("description", ""),
            "planned_tss": e.get("icu_training_load"),
            "planned_duration_s": e.get("moving_time"),
            "planned_distance_m": e.get("distance"),
            "color": e.get("color"),
            "workout_doc": e.get("workout_doc"),
        })
    return result


def fetch_week_plan(week_offset: int = 0) -> Dict[str, Any]:
    """Fetch this week's (or next week's) plan from Intervals.icu.

    Args:
        week_offset: 0 = this week, 1 = next week, -1 = last week

    Returns:
        {
            "week_start": "2026-03-24",
            "week_end": "2026-03-30",
            "events": [...],
            "ride_count": 3,
            "run_count": 0,
            "strength_count": 0,
            "total_planned_tss": 250,
            "rest_days": ["2026-03-24", "2026-03-26", "2026-03-28"],
        }
    """
    today = date.today()
    # Monday of the target week
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)

    events = fetch_planned_events(monday.isoformat(), sunday.isoformat())
    workouts = [e for e in events if e["category"] == "WORKOUT"]

    occupied_dates = {e["date"] for e in workouts}
    all_dates = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    rest_days = [d for d in all_dates if d not in occupied_dates]

    ride_count = sum(1 for e in workouts if e["type"] == "Ride")
    run_count = sum(1 for e in workouts if e["type"] == "Run")
    strength_count = sum(1 for e in workouts if e["type"] in ("WeightTraining", "Strength"))
    total_tss = sum(e.get("planned_tss") or 0 for e in workouts)

    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "events": workouts,
        "ride_count": ride_count,
        "run_count": run_count,
        "strength_count": strength_count,
        "total_planned_tss": total_tss,
        "rest_days": rest_days,
    }
