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
    """Find Intervals.icu API key from env or existing skill's state."""
    # 1. Environment variable
    env_key = os.environ.get("INTERVALS_API_KEY", "").strip()
    if env_key:
        return env_key

    # 2. Existing skill's key file
    key_file = (
        Path(__file__).resolve().parents[3]
        / "skills" / "garmin-cycling-coach" / "state" / "intervals_api_key"
    )
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key

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
