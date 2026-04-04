"""SQLite database layer — stores activities, records, wellness, and computed metrics."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DB_PATH = Path(
    os.environ.get("TRAININGEDGE_DB_PATH",
                    str(Path(__file__).resolve().parents[1] / "state" / "training_edge.db"))
)


def _ensure_db_dir():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db(db_path: Path = DB_PATH):
    """Context manager for database connections."""
    _ensure_db_dir()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH):
    """Create all tables if they don't exist."""
    with get_db(db_path) as conn:
        conn.executescript("""

        -- Activities: one row per Garmin activity
        CREATE TABLE IF NOT EXISTS activities (
            id              INTEGER PRIMARY KEY,   -- Garmin activity ID
            sport           TEXT,
            sub_sport       TEXT,
            name            TEXT,
            start_time      TEXT,                   -- ISO datetime
            date            TEXT,                   -- YYYY-MM-DD
            total_elapsed_s REAL,
            total_timer_s   REAL,                   -- moving time
            distance_m      REAL,
            avg_hr          INTEGER,
            max_hr          INTEGER,
            avg_power       INTEGER,
            max_power       INTEGER,
            avg_speed       REAL,                   -- m/s
            max_speed       REAL,
            avg_cadence     INTEGER,
            max_cadence     INTEGER,
            total_ascent    REAL,
            total_descent   REAL,
            total_calories  INTEGER,
            avg_temperature REAL,
            aerobic_te      REAL,
            anaerobic_te    REAL,
            device_ftp      INTEGER,                -- FTP from device at time of ride

            -- Computed metrics
            normalized_power    REAL,
            intensity_factor    REAL,
            tss                 REAL,
            xpower              REAL,
            estimated_ftp       REAL,               -- eFTP from PDC
            w_prime             REAL,
            carbs_used_g        REAL,
            trimp               REAL,
            vdot                REAL,

            -- Drift
            drift_method        TEXT,
            drift_pct           REAL,
            drift_classification TEXT,

            -- Garmin native metrics (来自 Garmin Connect API，非 FIT 自算)
            garmin_load             REAL,    -- Garmin Training Load (EPOC-based, 全运动类型)
            garmin_tss              REAL,    -- Garmin TSS (仅骑行有功率时)
            garmin_vo2max           REAL,    -- Garmin VO2max

            -- Running dynamics (跑步动态)
            avg_stance_time_ms      REAL,    -- 触地时间 ms
            avg_vertical_osc_cm     REAL,    -- 垂直振幅 cm
            avg_step_length_cm      REAL,    -- 步幅 cm
            avg_vertical_ratio      REAL,    -- 垂直步幅比 %

            -- JSON blobs for complex data
            power_zones_json    TEXT,                -- JSON: zone distribution
            hr_zones_json       TEXT,
            pdc_json            TEXT,                -- JSON: power duration curve
            laps_json           TEXT,                -- JSON: lap summaries

            -- Validation (校验期用)
            intervals_tss       REAL,
            intervals_np        REAL,
            intervals_ctl       REAL,
            intervals_atl       REAL,
            intervals_if        REAL,
            validation_json     TEXT,                -- JSON: full validation comparison

            -- Metadata
            fit_file_path       TEXT,
            synced_at           TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        );

        -- Records: second-by-second time series (optional, for deep analysis)
        -- Only stored for recent activities to save space
        CREATE TABLE IF NOT EXISTS records (
            activity_id     INTEGER NOT NULL,
            offset_s        INTEGER NOT NULL,       -- seconds from activity start
            heart_rate      INTEGER,
            power           INTEGER,
            speed           REAL,
            cadence         INTEGER,
            temperature     REAL,
            altitude        REAL,
            latitude        REAL,
            longitude       REAL,
            distance        REAL,
            PRIMARY KEY (activity_id, offset_s),
            FOREIGN KEY (activity_id) REFERENCES activities(id)
        );

        -- Daily wellness data
        CREATE TABLE IF NOT EXISTS wellness (
            date            TEXT PRIMARY KEY,        -- YYYY-MM-DD
            ctl             REAL,
            atl             REAL,
            tsb             REAL,
            ramp_rate       REAL,
            sleep_hours     REAL,
            sleep_score     REAL,
            readiness       REAL,
            resting_hr      INTEGER,
            hrv             REAL,
            steps           INTEGER,
            weight_kg       REAL,
            notes           TEXT,
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        -- Fitness history: daily CTL/ATL/TSB snapshots
        CREATE TABLE IF NOT EXISTS fitness_history (
            date            TEXT PRIMARY KEY,
            ctl             REAL,
            atl             REAL,
            tsb             REAL,
            ramp_rate       REAL,
            daily_tss       REAL,
            sport           TEXT
        );

        -- Settings: key-value store for FTP, max HR, zones, etc.
        CREATE TABLE IF NOT EXISTS settings (
            key             TEXT PRIMARY KEY,
            value           TEXT,
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        -- PDC history: best power for each duration across all activities
        CREATE TABLE IF NOT EXISTS pdc_bests (
            duration_s      INTEGER NOT NULL,
            power           REAL NOT NULL,
            activity_id     INTEGER NOT NULL,
            date            TEXT,
            PRIMARY KEY (duration_s, activity_id),
            FOREIGN KEY (activity_id) REFERENCES activities(id)
        );

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
        CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport);
        CREATE INDEX IF NOT EXISTS idx_records_activity ON records(activity_id);
        CREATE INDEX IF NOT EXISTS idx_wellness_date ON wellness(date);
        CREATE INDEX IF NOT EXISTS idx_fitness_date ON fitness_history(date);

        -- Body composition measurements
        CREATE TABLE IF NOT EXISTS body_composition (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            -- 基础
            weight_kg REAL,
            body_fat_pct REAL,
            bmi REAL,
            -- InBody 身体成分
            fat_mass_kg REAL,
            lean_body_mass_kg REAL,
            skeletal_muscle_kg REAL,
            muscle_mass_kg REAL,
            protein_kg REAL,
            bone_mass_kg REAL,
            body_water_kg REAL,
            body_water_pct REAL,
            -- InBody 身体参数
            visceral_fat_level INTEGER,
            waist_hip_ratio REAL,
            fitness_index REAL,
            mineral_kg REAL,
            bmr_kcal INTEGER,
            tdee_kcal INTEGER,
            body_age REAL,
            body_score INTEGER,
            body_type TEXT,
            -- InBody 调节建议
            fat_adjust_kg REAL,
            muscle_adjust_kg REAL,
            weight_adjust_kg REAL,
            -- Garmin 健康指标
            resting_hr INTEGER,
            hrv_ms REAL,
            sleep_duration_min REAL,
            deep_sleep_pct REAL,
            endurance_score REAL,
            body_battery INTEGER,
            -- 其他
            segmental_json TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(date, source)
        );

        -- Training plans
        CREATE TABLE IF NOT EXISTS training_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            phase TEXT,
            weekly_tss_target REAL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Planned workouts
        CREATE TABLE IF NOT EXISTS planned_workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            date TEXT NOT NULL,
            sport TEXT NOT NULL,
            title TEXT,
            description TEXT,
            target_duration_min REAL,
            target_tss REAL,
            target_intensity TEXT,
            muscle_groups_json TEXT,
            exercises_json TEXT,
            actual_activity_id TEXT,
            actual_tss REAL,
            actual_duration_min REAL,
            compliance_status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (plan_id) REFERENCES training_plans(id)
        );

        -- Muscle fatigue tracking
        CREATE TABLE IF NOT EXISTS muscle_fatigue (
            date TEXT NOT NULL,
            muscle_group TEXT NOT NULL,
            fatigue_score REAL,
            source_activity_ids TEXT,
            PRIMARY KEY (date, muscle_group)
        );

        -- Weekly workout templates
        CREATE TABLE IF NOT EXISTS weekly_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phase TEXT,
            days_json TEXT NOT NULL,
            total_tss_target REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Activity AI reviews
        CREATE TABLE IF NOT EXISTS activity_ai_reviews (
            activity_id TEXT PRIMARY KEY,
            analysis_version TEXT NOT NULL DEFAULT 'ride_review_v1',
            generated_at TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'completed',
            sport_type TEXT,
            summary_json TEXT,
            key_findings_json TEXT,
            structured_assessment_json TEXT,
            narrative_json TEXT,
            confidence_json TEXT,
            decision_hooks_json TEXT,
            metrics_used_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_body_comp_date ON body_composition(date);
        CREATE INDEX IF NOT EXISTS idx_planned_workouts_date ON planned_workouts(date);
        CREATE INDEX IF NOT EXISTS idx_planned_workouts_plan ON planned_workouts(plan_id);
        """)

        # 增量迁移：为已有数据库添加跑步动态列
        _migrate_running_dynamics(conn)


def _migrate_running_dynamics(conn: sqlite3.Connection):
    """Add running dynamics + Garmin native columns to existing activities table."""
    new_cols = [
        ("garmin_load", "REAL"),
        ("garmin_tss", "REAL"),
        ("garmin_vo2max", "REAL"),
        ("avg_stance_time_ms", "REAL"),
        ("avg_vertical_osc_cm", "REAL"),
        ("avg_step_length_cm", "REAL"),
        ("avg_vertical_ratio", "REAL"),
    ]
    for col_name, col_type in new_cols:
        try:
            conn.execute(f"ALTER TABLE activities ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass  # 列已存在


# ---------------------------------------------------------------------------
# Activity CRUD
# ---------------------------------------------------------------------------

def upsert_activity(conn: sqlite3.Connection, data: Dict[str, Any]) -> int:
    """Insert or update an activity."""
    cols = [
        "id", "sport", "sub_sport", "name", "start_time", "date",
        "total_elapsed_s", "total_timer_s", "distance_m",
        "avg_hr", "max_hr", "avg_power", "max_power",
        "avg_speed", "max_speed", "avg_cadence", "max_cadence",
        "total_ascent", "total_descent", "total_calories",
        "avg_temperature", "aerobic_te", "anaerobic_te", "device_ftp",
        "normalized_power", "intensity_factor", "tss", "xpower",
        "estimated_ftp", "w_prime", "carbs_used_g", "trimp", "vdot",
        "drift_method", "drift_pct", "drift_classification",
        "garmin_load", "garmin_tss", "garmin_vo2max",
        "avg_stance_time_ms", "avg_vertical_osc_cm",
        "avg_step_length_cm", "avg_vertical_ratio",
        "power_zones_json", "hr_zones_json", "pdc_json", "laps_json",
        "intervals_tss", "intervals_np", "intervals_ctl", "intervals_atl",
        "intervals_if", "validation_json", "fit_file_path",
    ]
    present = {k: v for k, v in data.items() if k in cols}
    keys = list(present.keys())
    placeholders = ", ".join(["?"] * len(keys))
    updates = ", ".join([f"{k}=excluded.{k}" for k in keys if k != "id"])

    sql = f"""
        INSERT INTO activities ({', '.join(keys)})
        VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET {updates}, updated_at=datetime('now')
    """
    conn.execute(sql, [present.get(k) for k in keys])
    return present.get("id", 0)


def insert_records(conn: sqlite3.Connection, activity_id: int, records: List[Dict[str, Any]]):
    """Bulk insert second-by-second records."""
    conn.execute("DELETE FROM records WHERE activity_id = ?", (activity_id,))
    conn.executemany(
        """INSERT INTO records (activity_id, offset_s, heart_rate, power, speed,
           cadence, temperature, altitude, latitude, longitude, distance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                activity_id,
                r.get("offset_s", 0),
                r.get("heart_rate"),
                r.get("power"),
                r.get("speed"),
                r.get("cadence"),
                r.get("temperature"),
                r.get("altitude"),
                r.get("latitude"),
                r.get("longitude"),
                r.get("distance"),
            )
            for r in records
        ],
    )


def get_activity(conn: sqlite3.Connection, activity_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single activity by ID."""
    row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    return dict(row) if row else None


def list_activities(
    conn: sqlite3.Connection,
    sport: Optional[str] = None,
    days: int = 30,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List recent activities."""
    from_date = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    sql = "SELECT * FROM activities WHERE date >= ?"
    params: list = [from_date]
    if sport:
        sql += " AND sport = ?"
        params.append(sport)
    sql += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Wellness CRUD
# ---------------------------------------------------------------------------

def upsert_wellness(conn: sqlite3.Connection, data: Dict[str, Any]):
    """Insert or update daily wellness."""
    keys = [k for k in data.keys() if k in (
        "date", "ctl", "atl", "tsb", "ramp_rate",
        "sleep_hours", "sleep_score", "readiness",
        "resting_hr", "hrv", "steps", "weight_kg", "notes",
    )]
    placeholders = ", ".join(["?"] * len(keys))
    updates = ", ".join([f"{k}=excluded.{k}" for k in keys if k != "date"])
    sql = f"""
        INSERT INTO wellness ({', '.join(keys)}) VALUES ({placeholders})
        ON CONFLICT(date) DO UPDATE SET {updates}, updated_at=datetime('now')
    """
    conn.execute(sql, [data.get(k) for k in keys])


def get_wellness(conn: sqlite3.Connection, on_date: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM wellness WHERE date = ?", (on_date,)).fetchone()
    return dict(row) if row else None


def list_wellness(conn: sqlite3.Connection, days: int = 30) -> List[Dict[str, Any]]:
    from_date = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    return [dict(row) for row in conn.execute(
        "SELECT * FROM wellness WHERE date >= ? ORDER BY date DESC", (from_date,)
    ).fetchall()]


# ---------------------------------------------------------------------------
# Fitness history
# ---------------------------------------------------------------------------

def upsert_fitness(conn: sqlite3.Connection, data: Dict[str, Any]):
    keys = ["date", "ctl", "atl", "tsb", "ramp_rate", "daily_tss", "sport"]
    present = {k: v for k, v in data.items() if k in keys}
    ks = list(present.keys())
    placeholders = ", ".join(["?"] * len(ks))
    updates = ", ".join([f"{k}=excluded.{k}" for k in ks if k != "date"])
    sql = f"""
        INSERT INTO fitness_history ({', '.join(ks)}) VALUES ({placeholders})
        ON CONFLICT(date) DO UPDATE SET {updates}
    """
    conn.execute(sql, [present.get(k) for k in ks])


def list_fitness_history(conn: sqlite3.Connection, days: int = 90) -> List[Dict[str, Any]]:
    from_date = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    return [dict(row) for row in conn.execute(
        "SELECT * FROM fitness_history WHERE date >= ? ORDER BY date", (from_date,)
    ).fetchall()]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )


# ---------------------------------------------------------------------------
# PDC bests
# ---------------------------------------------------------------------------

def upsert_pdc_best(conn: sqlite3.Connection, duration_s: int, power: float, activity_id: int, on_date: str):
    conn.execute(
        """INSERT INTO pdc_bests (duration_s, power, activity_id, date)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(duration_s, activity_id) DO UPDATE SET power=excluded.power, date=excluded.date""",
        (duration_s, power, activity_id, on_date),
    )


def get_pdc_bests(conn: sqlite3.Connection, days: int = 90) -> List[Dict[str, Any]]:
    """Get best power for each duration in the last N days."""
    from_date = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    return [dict(row) for row in conn.execute(
        """SELECT duration_s, MAX(power) as power, activity_id, date
           FROM pdc_bests WHERE date >= ?
           GROUP BY duration_s ORDER BY duration_s""",
        (from_date,),
    ).fetchall()]


# ---------------------------------------------------------------------------
# Weekly stats
# ---------------------------------------------------------------------------

def weekly_stats(conn: sqlite3.Connection, ref_date: Optional[str] = None) -> Dict[str, Any]:
    """Return aggregated stats for the ISO week containing *ref_date* (Mon-Sun).

    If the current week has no activities, automatically falls back to the
    previous week so the dashboard never shows all-zeros.

    Keys: cycling_distance_km, running_distance_km, strength_count,
          total_tss, activity_count, week_label.
    """
    import datetime as _dt
    d = date.fromisoformat(ref_date) if ref_date else date.today()
    monday = d - _dt.timedelta(days=d.weekday())
    sunday = monday + _dt.timedelta(days=6)

    def _query_week(mon, sun):
        mon_s, sun_s = mon.isoformat(), sun.isoformat()
        row = conn.execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN sport='cycling' THEN distance_m ELSE 0 END), 0) / 1000.0
                       AS cycling_distance_km,
                   COALESCE(SUM(CASE WHEN sport='running' THEN distance_m ELSE 0 END), 0) / 1000.0
                       AS running_distance_km,
                   COALESCE(SUM(CASE WHEN sport='training' THEN 1 ELSE 0 END), 0)
                       AS strength_count,
                   COALESCE(SUM(tss), 0)       AS total_tss,
                   COUNT(*)                     AS activity_count
               FROM activities
               WHERE date >= ? AND date <= ?""",
            (mon_s, sun_s),
        ).fetchone()
        return dict(row) if row else None

    result = _query_week(monday, sunday)
    label = "本周"

    # Fallback to previous week if current week is empty
    if not result or result.get("activity_count", 0) == 0:
        prev_monday = monday - _dt.timedelta(days=7)
        prev_sunday = prev_monday + _dt.timedelta(days=6)
        result = _query_week(prev_monday, prev_sunday)
        label = "上周"

    if not result:
        result = {
            "cycling_distance_km": 0, "running_distance_km": 0,
            "strength_count": 0, "total_tss": 0, "activity_count": 0,
        }

    result["week_label"] = label
    return result


# ---------------------------------------------------------------------------
# Body composition CRUD
# ---------------------------------------------------------------------------

_BODY_COMP_COLS = [
    "date", "source",
    # 基础
    "weight_kg", "body_fat_pct", "bmi",
    # InBody 身体成分
    "fat_mass_kg", "lean_body_mass_kg", "skeletal_muscle_kg",
    "muscle_mass_kg", "protein_kg", "bone_mass_kg",
    "body_water_kg", "body_water_pct",
    # InBody 身体参数
    "visceral_fat_level", "waist_hip_ratio", "fitness_index",
    "mineral_kg", "bmr_kcal", "tdee_kcal",
    "body_age", "body_score", "body_type",
    # InBody 调节建议
    "fat_adjust_kg", "muscle_adjust_kg", "weight_adjust_kg",
    # Garmin 健康指标
    "resting_hr", "hrv_ms", "sleep_duration_min",
    "deep_sleep_pct", "endurance_score", "body_battery",
    # 其他
    "segmental_json", "notes",
]


def upsert_body_comp(conn: sqlite3.Connection, data: Dict[str, Any]):
    """Insert or update a body_composition row (unique on date+source)."""
    present = {k: v for k, v in data.items() if k in _BODY_COMP_COLS}
    if "date" not in present:
        present["date"] = date.today().isoformat()
    if "source" not in present:
        present["source"] = "manual"
    keys = list(present.keys())
    placeholders = ", ".join(["?"] * len(keys))
    updates = ", ".join([f"{k}=excluded.{k}" for k in keys if k not in ("date", "source")])
    sql = f"""
        INSERT INTO body_composition ({', '.join(keys)}) VALUES ({placeholders})
        ON CONFLICT(date, source) DO UPDATE SET {updates}
    """
    conn.execute(sql, [present.get(k) for k in keys])


def list_body_comp(
    conn: sqlite3.Connection, days: int = 90, source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List body composition records from the last *days* days."""
    from_date = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    sql = "SELECT * FROM body_composition WHERE date >= ?"
    params: list = [from_date]
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY date DESC"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_latest_body_comp(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Return the most recent body_composition row."""
    row = conn.execute(
        "SELECT * FROM body_composition ORDER BY date DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Training Plan — Planned Workouts
# ---------------------------------------------------------------------------

def list_planned_workouts(conn: sqlite3.Connection, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Get planned workouts for a date range."""
    rows = conn.execute(
        "SELECT * FROM planned_workouts WHERE date >= ? AND date <= ? ORDER BY date",
        (date_from, date_to),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_planned_workout(conn: sqlite3.Connection, data: Dict[str, Any]):
    """Insert or update a planned workout."""
    fields = [
        'plan_id', 'date', 'sport', 'title', 'description',
        'target_duration_min', 'target_tss', 'target_intensity',
        'muscle_groups_json', 'exercises_json', 'actual_activity_id',
        'actual_tss', 'actual_duration_min', 'compliance_status',
    ]
    cols = [f for f in fields if f in data]
    vals = [data[f] for f in cols]

    if 'id' in data and data['id']:
        conn.execute(
            f"UPDATE planned_workouts SET {','.join(f'{c}=?' for c in cols)} WHERE id=?",
            vals + [data['id']],
        )
    else:
        placeholders = ','.join(['?'] * len(cols))
        col_names = ','.join(cols)
        conn.execute(
            f"INSERT INTO planned_workouts ({col_names}) VALUES ({placeholders})",
            vals,
        )
    conn.commit()


def delete_planned_workout(conn: sqlite3.Connection, workout_id: int):
    """Delete a planned workout by ID."""
    conn.execute("DELETE FROM planned_workouts WHERE id=?", (workout_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Training Plan — Weekly Templates
# ---------------------------------------------------------------------------

def list_weekly_templates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """List all weekly workout templates."""
    rows = conn.execute("SELECT * FROM weekly_templates ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def upsert_weekly_template(conn: sqlite3.Connection, data: Dict[str, Any]):
    """Insert or update a weekly template."""
    if 'id' in data and data['id']:
        conn.execute(
            "UPDATE weekly_templates SET name=?, phase=?, days_json=?, total_tss_target=? WHERE id=?",
            (data['name'], data.get('phase'), data['days_json'], data.get('total_tss_target'), data['id']),
        )
    else:
        conn.execute(
            "INSERT INTO weekly_templates (name, phase, days_json, total_tss_target) VALUES (?,?,?,?)",
            (data['name'], data.get('phase'), data['days_json'], data.get('total_tss_target')),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Training Plan — Muscle Fatigue
# ---------------------------------------------------------------------------

def get_muscle_fatigue(conn: sqlite3.Connection, on_date: str) -> Dict[str, float]:
    """Get muscle fatigue scores — uses latest data within 7 days of on_date.

    Looks back up to 7 days to find the most recent fatigue data per muscle group,
    so the heatmap doesn't show all-zeros just because today has no planned workouts.
    """
    import datetime as _dt
    d = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
    week_ago = (d - _dt.timedelta(days=7)).isoformat()

    rows = conn.execute(
        """SELECT muscle_group, fatigue_score, date
           FROM muscle_fatigue
           WHERE date >= ? AND date <= ?
           ORDER BY date DESC""",
        (week_ago, on_date),
    ).fetchall()

    # Keep the most recent score per muscle group
    result: Dict[str, float] = {}
    for r in rows:
        group = r['muscle_group']
        if group not in result:
            result[group] = r['fatigue_score']
    return result


def match_compliance(conn: sqlite3.Connection, activity_date: str = None):
    """Auto-match actual activities to planned workouts using weekly fuzzy matching.

    Logic (v2 — weekly fuzzy):
    1. Determine the ISO week that each pending planned workout belongs to.
    2. Collect all unmatched activities in the same week with a matching sport.
    3. Pick the activity whose TSS is closest to the plan's target_tss.
       If target_tss is NULL / 0, prefer the activity closest in date.
    4. Mark the planned workout as 'completed' with the matched activity.
    5. Only mark a workout as 'missed' after the entire week has passed
       (i.e. today > Sunday of that plan week), so swapping days within a
       week is allowed.

    Sport mapping: 'training' plans match 'strength_training' / 'training' /
    'cardio_training' activities.
    """
    import datetime as _dt
    today = date.today()
    today_iso = today.isoformat()

    # ── helpers ──────────────────────────────────────────────────────
    def _week_bounds(d: date):
        """Return (monday, sunday) for the ISO week containing *d*."""
        monday = d - _dt.timedelta(days=d.weekday())
        sunday = monday + _dt.timedelta(days=6)
        return monday, sunday

    def _parse_date(s: str) -> date:
        return date.fromisoformat(s)

    # ── date range ───────────────────────────────────────────────────
    if activity_date:
        ref = _parse_date(activity_date)
        monday, sunday = _week_bounds(ref)
        date_from = monday.isoformat()
        date_to = sunday.isoformat()
    else:
        # Cover the current week + previous 2 weeks
        monday, _ = _week_bounds(today)
        date_from = (monday - _dt.timedelta(weeks=2)).isoformat()
        date_to = today_iso

    # ── pending + missed plans (missed can be re-matched by weekly fuzzy) ──
    pending = conn.execute(
        """SELECT id, date, sport, target_tss FROM planned_workouts
           WHERE date >= ? AND date <= ? AND compliance_status IN ('pending', 'missed')
           ORDER BY date, id""",
        (date_from, date_to),
    ).fetchall()

    if not pending:
        return 0

    # Sport matching map: planned sport -> possible actual sports
    sport_map = {
        'cycling': ['cycling'],
        'running': ['running'],
        'training': ['training', 'strength_training', 'cardio_training'],
        'rest': [],
    }

    # ── global exclude set (activities already linked to a plan) ─────
    already_matched_ids = conn.execute(
        "SELECT actual_activity_id FROM planned_workouts WHERE actual_activity_id IS NOT NULL"
    ).fetchall()
    exclude_ids = {str(r['actual_activity_id']) for r in already_matched_ids}

    # ── cache: week-key -> list of activities ────────────────────────
    _week_activities_cache: dict[str, list] = {}

    def _get_week_activities(pw_date_str: str, possible_sports: list[str]) -> list:
        """Return activities in the same ISO week with matching sport."""
        pw_d = _parse_date(pw_date_str)
        mon, sun = _week_bounds(pw_d)
        cache_key = f"{mon.isoformat()}|{'|'.join(sorted(possible_sports))}"
        if cache_key not in _week_activities_cache:
            sport_ph = ','.join(['?'] * len(possible_sports))
            rows = conn.execute(
                f"""SELECT id, date, tss, total_timer_s, sport FROM activities
                    WHERE date >= ? AND date <= ? AND sport IN ({sport_ph})
                    ORDER BY date, start_time""",
                (mon.isoformat(), sun.isoformat(), *possible_sports),
            ).fetchall()
            _week_activities_cache[cache_key] = rows
        return _week_activities_cache[cache_key]

    # ── matching loop ────────────────────────────────────────────────
    matched = 0
    for pw in pending:
        pw_date = pw['date']
        pw_sport = pw['sport']

        # Skip rest days
        if pw_sport in ('rest', 'stretch'):
            continue

        possible_sports = sport_map.get(pw_sport, [pw_sport])
        if not possible_sports:
            continue

        candidates = _get_week_activities(pw_date, possible_sports)

        # Filter out already-matched activities
        available = [a for a in candidates if str(a['id']) not in exclude_ids]
        if not available:
            continue

        # Pick best match: closest TSS if target_tss is set, else closest date
        target_tss = pw['target_tss'] or 0

        def _score(act):
            act_tss = act['tss'] or 0
            # For strength training without TSS, estimate from duration
            if act_tss == 0 and act['sport'] in ('training', 'strength_training', 'cardio_training'):
                dur_min = (act['total_timer_s'] or 0) / 60.0
                act_tss = dur_min * 0.6
            tss_diff = abs(act_tss - target_tss) if target_tss > 0 else 0
            date_diff = abs((_parse_date(act['date']) - _parse_date(pw_date)).days)
            # Primary: TSS distance; secondary: date distance
            return (tss_diff, date_diff)

        best = min(available, key=_score)

        actual_tss = best['tss']
        actual_duration = round(best['total_timer_s'] / 60.0, 1) if best['total_timer_s'] else None

        # Estimate TSS for strength training without power data
        if actual_tss is None and best['sport'] in ('training', 'strength_training', 'cardio_training'):
            if actual_duration:
                actual_tss = round(actual_duration * 0.6)

        conn.execute(
            """UPDATE planned_workouts
               SET compliance_status = 'completed',
                   actual_activity_id = ?,
                   actual_tss = ?,
                   actual_duration_min = ?
               WHERE id = ?""",
            (str(best['id']), actual_tss, actual_duration, pw['id']),
        )
        exclude_ids.add(str(best['id']))
        matched += 1

    # ── mark missed: only after the ENTIRE week has passed ───────────
    # A workout is 'missed' only if today > Sunday of the plan's week
    still_pending = conn.execute(
        """SELECT id, date FROM planned_workouts
           WHERE date >= ? AND date <= ? AND compliance_status = 'pending'
           AND sport NOT IN ('rest', 'stretch')""",
        (date_from, date_to),
    ).fetchall()

    for wp in still_pending:
        wp_d = _parse_date(wp['date'])
        _, week_sunday = _week_bounds(wp_d)
        if today > week_sunday:
            conn.execute(
                "UPDATE planned_workouts SET compliance_status = 'missed' WHERE id = ?",
                (wp['id'],),
            )

    return matched


# ---------------------------------------------------------------------------
# Activity AI Reviews
# ---------------------------------------------------------------------------

def upsert_ai_review(conn: sqlite3.Connection, activity_id: str, review_data: dict):
    """存储或更新活动 AI 复盘结果。"""
    conn.execute(
        """INSERT INTO activity_ai_reviews
           (activity_id, analysis_version, generated_at, review_status, sport_type,
            summary_json, key_findings_json, structured_assessment_json,
            narrative_json, confidence_json, decision_hooks_json, metrics_used_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(activity_id) DO UPDATE SET
            analysis_version=excluded.analysis_version,
            generated_at=excluded.generated_at,
            review_status=excluded.review_status,
            sport_type=excluded.sport_type,
            summary_json=excluded.summary_json,
            key_findings_json=excluded.key_findings_json,
            structured_assessment_json=excluded.structured_assessment_json,
            narrative_json=excluded.narrative_json,
            confidence_json=excluded.confidence_json,
            decision_hooks_json=excluded.decision_hooks_json,
            metrics_used_json=excluded.metrics_used_json""",
        (
            str(activity_id),
            review_data.get("analysis_version", "ride_review_v1"),
            review_data.get("generated_at", datetime.now().isoformat()),
            review_data.get("review_status", "completed"),
            review_data.get("sport_type"),
            json.dumps(review_data.get("summary"), ensure_ascii=False) if review_data.get("summary") else None,
            json.dumps(review_data.get("key_findings"), ensure_ascii=False) if review_data.get("key_findings") else None,
            json.dumps(review_data.get("structured_assessment"), ensure_ascii=False) if review_data.get("structured_assessment") else None,
            json.dumps(review_data.get("narrative"), ensure_ascii=False) if review_data.get("narrative") else None,
            json.dumps(review_data.get("confidence"), ensure_ascii=False) if review_data.get("confidence") else None,
            json.dumps(review_data.get("decision_hooks"), ensure_ascii=False) if review_data.get("decision_hooks") else None,
            json.dumps(review_data.get("metrics_used"), ensure_ascii=False) if review_data.get("metrics_used") else None,
        ),
    )


def get_ai_review(conn: sqlite3.Connection, activity_id) -> Optional[Dict[str, Any]]:
    """获取活动的 AI 复盘结果。"""
    row = conn.execute(
        "SELECT * FROM activity_ai_reviews WHERE activity_id = ?",
        (str(activity_id),),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    # 解析 JSON 字段
    for field in ("summary", "key_findings", "structured_assessment",
                  "narrative", "confidence", "decision_hooks", "metrics_used"):
        json_key = f"{field}_json"
        if result.get(json_key):
            try:
                result[field] = json.loads(result[json_key])
            except (json.JSONDecodeError, TypeError):
                result[field] = None
        else:
            result[field] = None
    return result


def list_ai_badges(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """返回最近活动的 AI 复盘标签（activity_id + overall_label）。"""
    rows = conn.execute(
        """SELECT r.activity_id, r.summary_json, r.generated_at
           FROM activity_ai_reviews r
           JOIN activities a ON CAST(r.activity_id AS INTEGER) = a.id
           ORDER BY a.start_time DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    badges = []
    for row in rows:
        badge = {"activity_id": row["activity_id"], "generated_at": row["generated_at"]}
        if row["summary_json"]:
            try:
                summary = json.loads(row["summary_json"])
                badge["overall_label"] = summary.get("overall_label", "")
            except (json.JSONDecodeError, TypeError):
                badge["overall_label"] = ""
        else:
            badge["overall_label"] = ""
        badges.append(badge)
    return badges
