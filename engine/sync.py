"""Garmin sync — download FIT files, parse, compute metrics, store."""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Local timezone for date assignment (Garmin FIT stores UTC)
_LOCAL_TZ = timezone(timedelta(hours=int(os.environ.get("TRAININGEDGE_TZ_OFFSET", "8"))))

from . import fit_parser, metrics, database


FIT_DIR = Path(
    os.environ.get("TRAININGEDGE_FIT_DIR",
                    str(Path(__file__).resolve().parents[1] / "state" / "fit_files"))
)


def _ensure_fit_dir():
    FIT_DIR.mkdir(parents=True, exist_ok=True)


def get_garmin_client():
    """Initialize and return a Garmin Connect client with saved tokens."""
    from garminconnect import Garmin

    token_dir = os.environ.get("GARMINTOKENS", "")
    if not token_dir:
        # Fallback to the existing skill's token location
        token_dir = str(
            Path(__file__).resolve().parents[3]
            / "skills" / "garmin-cycling-coach" / "state" / "tokens"
        )

    api = Garmin()
    api.login(token_dir)
    return api


def download_fit(api, activity_id: int) -> Path:
    """Download the original FIT file for an activity.

    Returns path to the extracted .fit file.
    """
    _ensure_fit_dir()
    fit_path = FIT_DIR / f"{activity_id}.fit"

    if fit_path.exists():
        return fit_path

    # Download as ZIP
    zip_data = api.download_activity(
        activity_id, dl_fmt=api.ActivityDownloadFormat.ORIGINAL
    )

    # Extract .fit from ZIP
    if zipfile.is_zipfile(io.BytesIO(zip_data)):
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if fit_names:
                with zf.open(fit_names[0]) as src, open(fit_path, "wb") as dst:
                    dst.write(src.read())
                return fit_path
            else:
                # ZIP without .fit — write raw
                fit_path.write_bytes(zip_data)
                return fit_path
    else:
        # Not a ZIP, might be raw FIT
        fit_path.write_bytes(zip_data)
        return fit_path


def process_activity(
    api,
    garmin_activity: Dict[str, Any],
    ftp: Optional[float] = None,
    max_hr: int = 190,
    resting_hr: int = 50,
    weight_kg: float = 62.0,
    store_records: bool = True,
) -> Dict[str, Any]:
    """Full pipeline: download → parse → compute → store.

    Args:
        api: Garmin Connect client.
        garmin_activity: Activity dict from Garmin API.
        ftp: Current FTP (if None, uses device FTP or estimates).
        max_hr: Max heart rate.
        resting_hr: Resting heart rate.
        weight_kg: Body weight in kg.
        store_records: Whether to store second-by-second records (uses space).

    Returns:
        Dict with all computed metrics.
    """
    activity_id = garmin_activity.get("activityId") or garmin_activity.get("id")
    activity_id = int(activity_id)
    activity_name = garmin_activity.get("activityName", "Untitled")

    # 1. Download FIT
    fit_path = download_fit(api, activity_id)

    # 2. Parse FIT
    parsed = fit_parser.parse_fit(fit_path)
    session = parsed.session

    # 3. Extract time series
    powers = fit_parser.power_series(parsed)
    hrs = fit_parser.hr_series(parsed)
    speeds = fit_parser.speed_series(parsed)

    # 4. Determine FTP
    if ftp is None:
        ftp = session.threshold_power  # from device
    if ftp is None or ftp <= 0:
        ftp = 200  # fallback, will be updated by eFTP later

    # 5. Compute metrics
    np = metrics.normalized_power(powers)
    xp = metrics.xpower(powers)
    if_ = metrics.intensity_factor(np, ftp) if np else None
    tss = metrics.training_stress_score(np, ftp, session.total_timer_time or 0) if np else None

    # Power Duration Curve
    pdc = metrics.power_duration_curve(powers)
    eftp = metrics.estimate_ftp_from_pdc(pdc)
    wp = metrics.w_prime(pdc, ftp)

    # Zones
    pz = metrics.power_zone_distribution(powers, ftp) if powers else []
    hz = metrics.hr_zone_distribution(hrs, max_hr) if hrs else []

    # Drift
    drift = None
    if powers and any(p > 0 for p in powers):
        drift = metrics.compute_hr_drift(hrs, [float(p) for p in powers], method="hr_power")
    if drift is None and speeds and any(s > 0 for s in speeds):
        drift = metrics.compute_hr_drift(hrs, speeds, method="hr_speed")

    # Running metrics
    trimp = metrics.trimp_exp(hrs, resting_hr, max_hr, gender="male")

    # VDOT (only for running)
    vdot = None
    sport = str(session.sport or "").lower()
    if "running" in sport or "run" in sport:
        if session.total_distance and session.total_timer_time:
            vdot = metrics.vdot_from_race(session.total_distance, session.total_timer_time)

    # Carbs
    carbs = metrics.estimate_carbs_used(powers, hrs, max_hr, weight_kg) if powers else None

    # 6. Store in database
    # FIT stores UTC — convert to local timezone for date and start_time
    start_time_local = None
    activity_date = None
    if session.start_time:
        st = session.start_time
        if isinstance(st, datetime):
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            local_st = st.astimezone(_LOCAL_TZ)
            start_time_local = local_st.strftime("%Y-%m-%d %H:%M:%S")
            activity_date = local_st.strftime("%Y-%m-%d")
        else:
            start_time_local = str(st)
            activity_date = str(st)[:10]

    # 跑步步频: FIT 存的是单腿 strides/min，需要 ×2 得到总步频 spm
    sport = str(session.sport or "").lower()
    is_running = "running" in sport or "run" in sport
    avg_cadence = session.avg_cadence
    if is_running and avg_cadence and avg_cadence < 130:
        avg_cadence = avg_cadence * 2

    activity_data = {
        "id": activity_id,
        "sport": str(session.sport) if session.sport else None,
        "sub_sport": str(session.sub_sport) if session.sub_sport else None,
        "name": activity_name,
        "start_time": start_time_local,
        "date": activity_date,
        "total_elapsed_s": session.total_elapsed_time,
        "total_timer_s": session.total_timer_time,
        "distance_m": session.total_distance,
        "avg_hr": session.avg_heart_rate,
        "max_hr": session.max_heart_rate,
        "avg_power": session.avg_power,
        "max_power": session.max_power,
        "avg_speed": session.avg_speed,
        "max_speed": session.max_speed,
        "avg_cadence": avg_cadence,
        "max_cadence": session.max_cadence,
        "total_ascent": session.total_ascent,
        "total_descent": session.total_descent,
        "total_calories": session.total_calories,
        "avg_temperature": session.avg_temperature,
        "aerobic_te": session.training_effect,
        "anaerobic_te": session.anaerobic_training_effect,
        "device_ftp": session.threshold_power,
        "normalized_power": np,
        "intensity_factor": if_,
        "tss": tss,
        "xpower": xp,
        "estimated_ftp": eftp,
        "w_prime": wp,
        "carbs_used_g": carbs,
        "trimp": trimp,
        "vdot": vdot,
        "drift_method": drift.method if drift else None,
        "drift_pct": drift.drift_pct if drift else None,
        "drift_classification": drift.classification if drift else None,
        # Running dynamics (FIT stores mm, convert to cm)
        "avg_stance_time_ms": session.avg_stance_time,
        "avg_vertical_osc_cm": round(session.avg_vertical_oscillation / 10, 1) if session.avg_vertical_oscillation else None,
        "avg_step_length_cm": round(session.avg_step_length / 10, 1) if session.avg_step_length else None,
        "avg_vertical_ratio": round(session.avg_vertical_ratio / 100, 2) if session.avg_vertical_ratio else None,
        "power_zones_json": json.dumps([
            {"zone": z.zone, "seconds": z.seconds, "pct": z.pct,
             "watts_low": z.watts_low, "watts_high": z.watts_high}
            for z in pz
        ]) if pz else None,
        "hr_zones_json": json.dumps([
            {"zone": z.zone, "seconds": z.seconds, "pct": z.pct,
             "watts_low": z.watts_low, "watts_high": z.watts_high}
            for z in hz
        ]) if hz else None,
        "pdc_json": json.dumps({str(k): v for k, v in pdc.items()}) if pdc else None,
        "laps_json": json.dumps([
            {
                "total_elapsed_time": lap.total_elapsed_time,
                "total_distance": lap.total_distance,
                "avg_heart_rate": lap.avg_heart_rate,
                "max_heart_rate": lap.max_heart_rate,
                "avg_power": lap.avg_power,
                "avg_speed": lap.avg_speed,
                "avg_cadence": lap.avg_cadence,
            }
            for lap in parsed.laps
        ]) if parsed.laps else None,
        "fit_file_path": str(fit_path),
    }

    with database.get_db() as conn:
        database.upsert_activity(conn, activity_data)

        # Store PDC bests
        if pdc and activity_date:
            for duration_s, power in pdc.items():
                if power is not None and power > 0:
                    database.upsert_pdc_best(conn, duration_s, power, activity_id, activity_date)

        # Store records (optional, for deep analysis)
        if store_records and parsed.records:
            base_time = parsed.records[0].timestamp
            record_dicts = []
            for i, rec in enumerate(parsed.records):
                offset = i  # assume 1-second intervals
                if base_time and rec.timestamp:
                    offset = int((rec.timestamp - base_time).total_seconds())
                record_dicts.append({
                    "offset_s": offset,
                    "heart_rate": rec.heart_rate,
                    "power": rec.power,
                    "speed": rec.speed,
                    "cadence": rec.cadence,
                    "temperature": rec.temperature,
                    "altitude": rec.altitude,
                    "latitude": rec.position_lat,
                    "longitude": rec.position_long,
                    "distance": rec.distance,
                })
            database.insert_records(conn, activity_id, record_dicts)

    return activity_data


def sync_recent(
    days: int = 7,
    activity_type: str = "all",
    ftp: Optional[float] = None,
    max_hr: int = 190,
    resting_hr: int = 50,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Sync recent activities from Garmin Connect.

    Downloads FIT files, parses, computes metrics, and stores in DB.
    """
    api = get_garmin_client()

    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))

    # Garmin API: pass None for all activity types, or a specific type like "cycling", "running"
    search_type = None if activity_type in ("all", "", None) else activity_type
    activities = api.get_activities_by_date(
        start_date.isoformat(), end_date.isoformat(), search_type
    )

    if not isinstance(activities, list):
        return []

    results = []
    for act in activities[:limit]:
        try:
            result = process_activity(
                api, act, ftp=ftp, max_hr=max_hr, resting_hr=resting_hr
            )
            results.append(result)
            print(f"  ✓ {result.get('name')} ({result.get('date')}) — TSS: {result.get('tss')}, NP: {result.get('normalized_power')}")
        except Exception as e:
            act_id = act.get("activityId") or act.get("id")
            print(f"  ✗ Activity {act_id}: {e}")

    # Update fitness history
    _update_fitness_history(ftp)

    # Auto-match activities to planned workouts
    with database.get_db() as conn:
        matched = database.match_compliance(conn)
        if matched:
            print(f"  ✓ Matched {matched} activities to planned workouts")

    return results


def sync_garmin_wellness(days: int = 14) -> Dict[str, Any]:
    """Sync wellness data (HRV, sleep, body battery, etc.) from Garmin Connect.

    Fetches daily summaries and stores in both wellness and body_composition tables.

    Returns:
        Summary of synced data.
    """
    api = get_garmin_client()
    today = date.today()
    results = {"days_synced": 0, "hrv_count": 0, "sleep_count": 0, "errors": []}

    for day_offset in range(days):
        d = today - timedelta(days=day_offset)
        d_str = d.isoformat()

        try:
            # --- Daily summary (resting HR, steps, etc.) ---
            try:
                user_summary = api.get_user_summary(d_str)
                resting_hr = user_summary.get("restingHeartRate")
                steps = user_summary.get("totalSteps")
                body_battery_high = user_summary.get("bodyBatteryHighestValue")
            except Exception:
                resting_hr = None
                steps = None
                body_battery_high = None

            # --- HRV ---
            hrv_ms = None
            try:
                hrv_data = api.get_hrv_data(d_str)
                if hrv_data:
                    # Try different response formats
                    if isinstance(hrv_data, dict):
                        summary = hrv_data.get("hrvSummary") or hrv_data.get("summary") or {}
                        hrv_ms = summary.get("weeklyAvg") or summary.get("lastNightAvg") or summary.get("lastNight5MinHigh")
                        if not hrv_ms and "startTimestampGMT" in summary:
                            hrv_ms = summary.get("weeklyAvg")
                    if hrv_ms:
                        results["hrv_count"] += 1
            except Exception as e:
                # HRV not available for this day is common
                pass

            # --- Sleep ---
            sleep_duration_min = None
            deep_sleep_pct = None
            sleep_score = None
            try:
                sleep_data = api.get_sleep_data(d_str)
                if sleep_data:
                    if isinstance(sleep_data, dict):
                        daily_sleep = sleep_data.get("dailySleepDTO") or sleep_data
                        sleep_secs = daily_sleep.get("sleepTimeSeconds")
                        if sleep_secs:
                            sleep_duration_min = sleep_secs / 60.0
                        deep_secs = daily_sleep.get("deepSleepSeconds")
                        if deep_secs and sleep_secs and sleep_secs > 0:
                            deep_sleep_pct = (deep_secs / sleep_secs) * 100.0
                        sleep_score = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value") if isinstance(daily_sleep.get("sleepScores"), dict) else None
                        if not sleep_score:
                            sleep_score = daily_sleep.get("overallScore")
                        results["sleep_count"] += 1
            except Exception:
                pass

            # --- Body Battery ---
            body_battery = body_battery_high
            try:
                bb_data = api.get_body_battery(d_str)
                if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
                    # Get the max charged value
                    bb_vals = [item.get("charged") for item in bb_data if item.get("charged")]
                    if bb_vals:
                        body_battery = max(bb_vals)
            except Exception:
                pass

            # --- Store in wellness table ---
            wellness_data = {"date": d_str}
            if resting_hr:
                wellness_data["resting_hr"] = resting_hr
            if hrv_ms:
                wellness_data["hrv"] = hrv_ms
            if steps:
                wellness_data["steps"] = steps
            if sleep_duration_min:
                wellness_data["sleep_hours"] = round(sleep_duration_min / 60.0, 2)
            if sleep_score:
                wellness_data["sleep_score"] = sleep_score

            if len(wellness_data) > 1:  # More than just date
                with database.get_db() as conn:
                    database.upsert_wellness(conn, wellness_data)

            # --- Store Garmin metrics in body_composition too (for body_data page) ---
            body_data = {"date": d_str, "source": "Garmin"}
            if resting_hr:
                body_data["resting_hr"] = resting_hr
            if hrv_ms:
                body_data["hrv_ms"] = hrv_ms
            if sleep_duration_min:
                body_data["sleep_duration_min"] = round(sleep_duration_min, 1)
            if deep_sleep_pct:
                body_data["deep_sleep_pct"] = round(deep_sleep_pct, 1)
            if body_battery:
                body_data["body_battery"] = body_battery

            if len(body_data) > 2:  # More than just date + source
                with database.get_db() as conn:
                    database.upsert_body_comp(conn, body_data)

            results["days_synced"] += 1

        except Exception as e:
            results["errors"].append(f"{d_str}: {str(e)}")

    return results


def _update_fitness_history(ftp: Optional[float] = None):
    """Recompute CTL/ATL/TSB from all stored activities."""
    with database.get_db() as conn:
        # Get initial CTL/ATL from settings (seeded from intervals.icu)
        initial_ctl = float(database.get_setting(conn, "initial_ctl") or "0")
        initial_atl = float(database.get_setting(conn, "initial_atl") or "0")

        # Get all activities with TSS
        rows = conn.execute(
            "SELECT date, tss, sport FROM activities WHERE tss IS NOT NULL ORDER BY date"
        ).fetchall()

        if not rows:
            return

        daily_loads = [
            metrics.DailyLoad(
                day=date.fromisoformat(row["date"]),
                tss=row["tss"],
                sport=row["sport"] or "cycling",
            )
            for row in rows
        ]

        history = metrics.compute_fitness_history(
            daily_loads,
            initial_ctl=initial_ctl,
            initial_atl=initial_atl,
        )

        for state in history:
            daily_tss = sum(
                dl.tss for dl in daily_loads if dl.day == state.day
            )
            database.upsert_fitness(conn, {
                "date": state.day.isoformat(),
                "ctl": state.ctl,
                "atl": state.atl,
                "tsb": state.tsb,
                "ramp_rate": state.ramp_rate,
                "daily_tss": daily_tss,
            })
