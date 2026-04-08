"""Tests for engine/database.py — SQLite data layer."""

import os
import tempfile

import pytest

from engine import database


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database for testing."""
    path = tmp_path / "test.db"
    database.init_db(path)
    return path


class TestInitDB:
    def test_creates_tables(self, db_path):
        with database.get_db(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "activities" in tables
        assert "records" in tables
        assert "wellness" in tables
        assert "fitness_history" in tables
        assert "settings" in tables
        assert "pdc_bests" in tables
        assert "planned_workouts" in tables

    def test_running_dynamics_columns(self, db_path):
        with database.get_db(db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
        assert "avg_stance_time_ms" in cols
        assert "avg_vertical_osc_cm" in cols
        assert "avg_step_length_cm" in cols
        assert "avg_vertical_ratio" in cols

    def test_garmin_load_columns(self, db_path):
        with database.get_db(db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
        assert "garmin_load" in cols
        assert "garmin_tss" in cols
        assert "garmin_vo2max" in cols


class TestActivityCRUD:
    def test_upsert_and_get(self, db_path):
        data = {
            "id": 12345,
            "sport": "cycling",
            "name": "Test Ride",
            "date": "2026-04-01",
            "distance_m": 50000,
            "avg_hr": 145,
            "tss": 80.5,
            "garmin_load": 120.3,
        }
        with database.get_db(db_path) as conn:
            database.upsert_activity(conn, data)

        with database.get_db(db_path) as conn:
            act = database.get_activity(conn, 12345)
        assert act is not None
        assert act["sport"] == "cycling"
        assert act["name"] == "Test Ride"
        assert act["tss"] == 80.5
        assert act["garmin_load"] == 120.3

    def test_upsert_update(self, db_path):
        """Upsert should update existing activity."""
        with database.get_db(db_path) as conn:
            database.upsert_activity(conn, {"id": 100, "sport": "running", "date": "2026-04-01"})
            database.upsert_activity(conn, {"id": 100, "sport": "running", "date": "2026-04-01", "tss": 50})
            act = database.get_activity(conn, 100)
        assert act["tss"] == 50

    def test_list_activities(self, db_path):
        with database.get_db(db_path) as conn:
            database.upsert_activity(conn, {"id": 1, "sport": "cycling", "date": "2026-04-01"})
            database.upsert_activity(conn, {"id": 2, "sport": "running", "date": "2026-04-02"})
            database.upsert_activity(conn, {"id": 3, "sport": "cycling", "date": "2026-04-03"})

            all_acts = database.list_activities(conn, days=30)
            assert len(all_acts) == 3

            cycling = database.list_activities(conn, days=30, sport="cycling")
            assert len(cycling) == 2


class TestSettings:
    def test_set_and_get(self, db_path):
        with database.get_db(db_path) as conn:
            database.set_setting(conn, "ftp", "229")
            val = database.get_setting(conn, "ftp")
        assert val == "229"

    def test_get_missing(self, db_path):
        with database.get_db(db_path) as conn:
            val = database.get_setting(conn, "nonexistent")
        assert val is None

    def test_overwrite(self, db_path):
        with database.get_db(db_path) as conn:
            database.set_setting(conn, "key", "old")
            database.set_setting(conn, "key", "new")
            val = database.get_setting(conn, "key")
        assert val == "new"


class TestFitnessHistory:
    def test_upsert_and_list(self, db_path):
        with database.get_db(db_path) as conn:
            database.upsert_fitness(conn, {
                "date": "2026-04-01", "ctl": 40.0, "atl": 50.0, "tsb": -10.0,
                "ramp_rate": 2.0, "daily_tss": 80.0,
            })
            database.upsert_fitness(conn, {
                "date": "2026-04-02", "ctl": 41.0, "atl": 48.0, "tsb": -7.0,
                "ramp_rate": 1.5, "daily_tss": 60.0,
            })
            history = database.list_fitness_history(conn, days=30)
        assert len(history) == 2
        assert len(history) >= 2  # both entries present


class TestMigrationIdempotent:
    def test_double_init(self, db_path):
        """init_db should be safe to call multiple times."""
        database.init_db(db_path)
        database.init_db(db_path)
        with database.get_db(db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
        assert "garmin_load" in cols
