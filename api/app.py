"""FastAPI application — REST API + web dashboard for TrainingEdge."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import hashlib
import hmac
import os
import secrets

logger = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from engine import database, metrics, validator
from engine.auth import verify_api_key, get_or_create_api_key
from engine.readiness import (
    compute_readiness, compute_weekly_deviation,
    compute_body_trend_summary, get_metric_comparisons,
    get_body_comp_comparisons, compute_decision_summary,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TrainingEdge",
    description="自建运动数据分析平台 — FIT 解析 + 训练指标计算",
    version="0.7.0",
)


# ---------------------------------------------------------------------------
# Web Access Protection — simple password gate for public tunnel access
# ---------------------------------------------------------------------------

_ACCESS_PASSWORD = os.environ.get("TRAININGEDGE_PASSWORD", "")
_SESSION_SECRET = os.environ.get("TRAININGEDGE_SESSION_SECRET", secrets.token_hex(32))
_AUTH_COOKIE = "oc_session"
_PUBLIC_PATHS = {"/api/health", "/login", "/static"}


def _make_session_token(password: str) -> str:
    """Create HMAC session token from password."""
    return hmac.new(_SESSION_SECRET.encode(), password.encode(), hashlib.sha256).hexdigest()[:32]


class AccessGateMiddleware(BaseHTTPMiddleware):
    """Block unauthenticated web access when TRAININGEDGE_PASSWORD is set."""

    async def dispatch(self, request: Request, call_next):
        # Skip if no password configured (local/dev mode)
        if not _ACCESS_PASSWORD:
            return await call_next(request)

        path = request.url.path

        # Allow public paths
        if any(path.startswith(p) for p in _PUBLIC_PATHS):
            return await call_next(request)

        # Allow API calls with valid API key
        if path.startswith("/api/") and (
            request.headers.get("X-API-Key") or request.query_params.get("api_key")
        ):
            return await call_next(request)

        # Check session cookie
        session = request.cookies.get(_AUTH_COOKIE, "")
        expected = _make_session_token(_ACCESS_PASSWORD)
        if hmac.compare_digest(session, expected):
            return await call_next(request)

        # Not authenticated → redirect to login
        return RedirectResponse(f"/login?next={path}", status_code=302)


app.add_middleware(AccessGateMiddleware)

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "web" / "static"
TEMPLATES_DIR = BASE_DIR / "web" / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.auto_reload = True
templates.env.cache = None  # Fix Python 3.13 + Jinja2 cache unhashable dict bug


# ---------------------------------------------------------------------------
# Auto-sync scheduler
# ---------------------------------------------------------------------------

_SYNC_INTERVAL_HOURS = int(os.environ.get("TRAININGEDGE_SYNC_INTERVAL_HOURS", "6"))
_sync_task: Optional[asyncio.Task] = None


async def _auto_sync_loop():
    """Background loop: sync Garmin activities + wellness every N hours."""
    from engine import sync as garmin_sync

    await asyncio.sleep(30)  # 启动后等 30 秒再首次同步，避免和 init_db 竞争

    while True:
        try:
            logger.info("[auto-sync] starting Garmin sync (interval=%dh)", _SYNC_INTERVAL_HOURS)

            # 同步最近 3 天活动
            try:
                act_result = garmin_sync.sync_recent(days=3)
                logger.info("[auto-sync] activities: synced %d activities", len(act_result))
            except Exception as e:
                logger.error("[auto-sync] activities sync failed: %s", e)

            # 同步最近 3 天 wellness（HRV / 睡眠）
            try:
                well_result = garmin_sync.sync_garmin_wellness(days=3)
                logger.info("[auto-sync] wellness: %s", well_result.get("message", well_result))
            except Exception as e:
                logger.error("[auto-sync] wellness sync failed: %s", e)

            # 同步后自动跑 match_compliance
            try:
                import sqlite3
                db_path = os.environ.get("TRAININGEDGE_DB_PATH", "/data/training_edge.db")
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    database.match_compliance(conn)
                logger.info("[auto-sync] match_compliance done")
            except Exception as e:
                logger.error("[auto-sync] match_compliance failed: %s", e)

        except Exception as e:
            logger.error("[auto-sync] unexpected error: %s", e)

        await asyncio.sleep(_SYNC_INTERVAL_HOURS * 3600)


@app.on_event("startup")
def startup():
    database.init_db()

    global _sync_task
    if _SYNC_INTERVAL_HOURS > 0:
        loop = asyncio.get_event_loop()
        _sync_task = loop.create_task(_auto_sync_loop())
        logger.info("[auto-sync] scheduled every %d hours", _SYNC_INTERVAL_HOURS)
    else:
        logger.info("[auto-sync] disabled (TRAININGEDGE_SYNC_INTERVAL_HOURS=0)")


# ---------------------------------------------------------------------------
# Login page (only active when TRAININGEDGE_PASSWORD is set)
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    if not _ACCESS_PASSWORD:
        return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {
        "request": request, "next": next, "error": error,
    })


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = form.get("password", "")
    next_url = form.get("next", "/")

    if password == _ACCESS_PASSWORD:
        resp = RedirectResponse(next_url, status_code=302)
        # 根据请求协议判断是否设置 secure（HTTP 内网访问兼容）
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(
            _AUTH_COOKIE,
            _make_session_token(_ACCESS_PASSWORD),
            httponly=True,
            secure=is_https,
            samesite="lax",
            max_age=86400 * 30,  # 30 days
        )
        return resp
    return RedirectResponse(f"/login?next={next_url}&error=密码错误", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# REST API — for skill consumption
# ═══════════════════════════════════════════════════════════════════════════════

# Health check - no auth required
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.6.0"}


# Status - auth required
@app.get("/api/status", dependencies=[Depends(verify_api_key)])
async def status():
    database.init_db()
    with database.get_db() as conn:
        act_count = conn.execute("SELECT COUNT(*) as c FROM activities").fetchone()['c']
        last_sync = conn.execute("SELECT MAX(date) as d FROM activities").fetchone()['d']
        import os
        db_path = database.DB_PATH
        db_size_mb = os.path.getsize(db_path) / 1024 / 1024 if os.path.exists(str(db_path)) else 0
    return {
        "status": "ok",
        "activities": act_count,
        "last_sync": last_sync,
        "db_size_mb": round(db_size_mb, 2),
    }


# Summary endpoint for TrainingEdge Skill - auth required
@app.get("/api/summary", dependencies=[Depends(verify_api_key)])
async def summary():
    database.init_db()
    with database.get_db() as conn:
        # Fitness
        fitness_rows = conn.execute(
            "SELECT * FROM fitness_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        ctl = fitness_rows['ctl'] if fitness_rows else None
        atl = fitness_rows['atl'] if fitness_rows else None
        tsb = fitness_rows['tsb'] if fitness_rows else None

        # Weekly stats
        from engine.database import weekly_stats
        weekly = weekly_stats(conn)

        # Latest body comp
        from engine.database import get_latest_body_comp
        body = get_latest_body_comp(conn)

        # Today's plan
        from datetime import date as dt_date
        today = dt_date.today().isoformat()
        planned = conn.execute(
            "SELECT * FROM planned_workouts WHERE date=? ORDER BY id", (today,)
        ).fetchall()
        today_plan = [dict(p) for p in planned] if planned else []

        # Recent activities
        recent = conn.execute(
            "SELECT id, date, name, sport, tss, normalized_power, distance_m FROM activities ORDER BY date DESC LIMIT 5"
        ).fetchall()

        # Muscle fatigue
        from engine.database import get_muscle_fatigue
        fatigue = get_muscle_fatigue(conn, today)

        # TSB status text
        if tsb is not None:
            if tsb < -20:
                tsb_status = "建议休息或轻松恢复"
            elif tsb < 0:
                tsb_status = "正常训练，注意疲劳积累"
            elif tsb <= 15:
                tsb_status = "状态良好，可以安排强度训练"
            else:
                tsb_status = "可能脱训，需要增加训练量"
        else:
            tsb_status = "暂无数据"

    return {
        "date": today,
        "fitness": {
            "ctl": ctl, "atl": atl, "tsb": tsb,
            "status": tsb_status,
        },
        "body": dict(body) if body else None,
        "today_plan": today_plan,
        "muscle_fatigue": fatigue,
        "weekly": weekly,
        "recent_activities": [dict(r) for r in recent],
    }


@app.get("/api/activities", dependencies=[Depends(verify_api_key)])
def api_list_activities(
    sport: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
):
    """List recent activities with computed metrics."""
    with database.get_db() as conn:
        activities = database.list_activities(conn, sport=sport, days=days, limit=limit)
    return {"ok": True, "count": len(activities), "activities": activities}


@app.get("/api/activity/{activity_id}", dependencies=[Depends(verify_api_key)])
def api_get_activity(activity_id: int):
    """Get a single activity with all computed metrics."""
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
    if not activity:
        raise HTTPException(404, f"Activity {activity_id} not found")

    # Parse JSON fields
    for json_field in ["power_zones_json", "hr_zones_json", "pdc_json", "laps_json", "validation_json"]:
        if activity.get(json_field):
            activity[json_field.replace("_json", "")] = json.loads(activity[json_field])

    return {"ok": True, "activity": activity}


@app.get("/api/fitness", dependencies=[Depends(verify_api_key)])
def api_fitness_history(days: int = Query(90, ge=1, le=730)):
    """Get CTL/ATL/TSB history."""
    with database.get_db() as conn:
        history = database.list_fitness_history(conn, days=days)
    return {"ok": True, "count": len(history), "history": history}


@app.get("/api/wellness", dependencies=[Depends(verify_api_key)])
def api_wellness(days: int = Query(30, ge=1, le=365)):
    """Get daily wellness data."""
    with database.get_db() as conn:
        wellness = database.list_wellness(conn, days=days)
    return {"ok": True, "count": len(wellness), "wellness": wellness}


@app.get("/api/pdc", dependencies=[Depends(verify_api_key)])
def api_pdc_bests(days: int = Query(90, ge=1, le=365)):
    """Get power duration curve (best efforts)."""
    with database.get_db() as conn:
        bests = database.get_pdc_bests(conn, days=days)
    return {"ok": True, "bests": bests}


@app.get("/api/validation", dependencies=[Depends(verify_api_key)])
def api_validation(days: int = Query(30, ge=1, le=365)):
    """Get validation dashboard data."""
    return {"ok": True, "dashboard": validator.validation_dashboard(days)}


@app.post("/api/validate/{activity_id}", dependencies=[Depends(verify_api_key)])
def api_validate_activity(activity_id: int, intervals_data: Dict[str, Any]):
    """Validate a single activity against Intervals.icu data."""
    result = validator.validate_activity(activity_id, intervals_data)
    return {
        "ok": True,
        "result": {
            "activity_id": result.activity_id,
            "date": result.activity_date,
            "name": result.activity_name,
            "all_passed": result.all_passed,
            "summary": result.summary,
            "comparisons": [
                {
                    "field": c.field, "ours": c.ours, "theirs": c.theirs,
                    "diff": c.diff, "tolerance": c.tolerance,
                    "passed": c.passed, "note": c.note,
                }
                for c in result.comparisons
            ],
        },
    }


@app.get("/api/analyze/{activity_id}", dependencies=[Depends(verify_api_key)])
def api_analyze(activity_id: int):
    """Full analysis for a single activity — designed for skill consumption.

    Returns everything the AI skill needs in one call.
    """
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
        if not activity:
            raise HTTPException(404, f"Activity {activity_id} not found")

        activity_date = activity.get("date", "")
        wellness = database.get_wellness(conn, activity_date) if activity_date else None
        fitness = conn.execute(
            "SELECT * FROM fitness_history WHERE date = ?", (activity_date,)
        ).fetchone()
        fitness = dict(fitness) if fitness else None

        # Parse JSON blobs
        pz = json.loads(activity["power_zones_json"]) if activity.get("power_zones_json") else None
        hz = json.loads(activity["hr_zones_json"]) if activity.get("hr_zones_json") else None
        pdc = json.loads(activity["pdc_json"]) if activity.get("pdc_json") else None
        laps = json.loads(activity["laps_json"]) if activity.get("laps_json") else None

    # Build the unified response the skill expects
    return {
        "ok": True,
        "activity": {
            "id": activity["id"],
            "name": activity["name"],
            "sport": activity["sport"],
            "date": activity["date"],
            "start_time": activity["start_time"],
            "distance_km": round(activity["distance_m"] / 1000, 2) if activity.get("distance_m") else None,
            "duration_min": round(activity["total_elapsed_s"] / 60, 1) if activity.get("total_elapsed_s") else None,
            "moving_duration_min": round(activity["total_timer_s"] / 60, 1) if activity.get("total_timer_s") else None,
            "avg_hr_bpm": activity["avg_hr"],
            "max_hr_bpm": activity["max_hr"],
            "avg_power_w": activity["avg_power"],
            "max_power_w": activity["max_power"],
            "normalized_power_w": activity["normalized_power"],
            "avg_speed_kph": round(activity["avg_speed"] * 3.6, 2) if activity.get("avg_speed") else None,
            "avg_cadence_rpm": activity["avg_cadence"],
            "elevation_gain_m": activity["total_ascent"],
            "calories_kcal": activity["total_calories"],
            "aerobic_te": activity["aerobic_te"],
            "anaerobic_te": activity["anaerobic_te"],
        },
        "training_load": {
            "tss": activity["tss"],
            "intensity_factor": activity["intensity_factor"],
            "ftp_w": activity["device_ftp"] or activity["estimated_ftp"],
            "estimated_ftp_w": activity["estimated_ftp"],
            "w_prime_j": activity["w_prime"],
            "xpower_w": activity["xpower"],
        },
        "fitness": {
            "ctl": fitness["ctl"] if fitness else None,
            "atl": fitness["atl"] if fitness else None,
            "tsb": fitness["tsb"] if fitness else None,
            "ramp_rate": fitness["ramp_rate"] if fitness else None,
        },
        "zones": {
            "power_zones": pz,
            "hr_zones": hz,
        },
        "drift": {
            "method": activity["drift_method"],
            "drift_pct": activity["drift_pct"],
            "classification": activity["drift_classification"],
        },
        "running": {
            "trimp": activity["trimp"],
            "vdot": activity["vdot"],
            "carbs_used_g": activity["carbs_used_g"],
        },
        "pdc": pdc,
        "laps": laps,
        "wellness": dict(wellness) if wellness else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Decision Cockpit APIs — v1 结论层接口
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/readiness")
def api_readiness():
    """今日训练就绪度评估。"""
    with database.get_db() as conn:
        result = compute_readiness(conn)
    return {"ok": True, **result.to_dict()}


@app.get("/api/weekly-deviation")
def api_weekly_deviation(week: Optional[str] = None):
    """本周训练执行偏差分析。"""
    with database.get_db() as conn:
        result = compute_weekly_deviation(conn, ref_date=week)
    return {"ok": True, **result.to_dict()}


@app.get("/api/body-trend-summary")
def api_body_trend_summary():
    """身体数据趋势结论。"""
    with database.get_db() as conn:
        result = compute_body_trend_summary(conn)
    return {"ok": True, **result.to_dict()}


@app.get("/api/decision-summary")
def api_decision_summary():
    """综合决策摘要 — 聚合就绪度、周偏差、身体趋势。"""
    with database.get_db() as conn:
        result = compute_decision_summary(conn)
    return {"ok": True, **result}


@app.get("/api/constraint-status")
def api_constraint_status():
    """本周计划约束满足情况检查。

    检查项：休息日约束、运动频率约束、连续高负荷天数约束。
    返回格式: {"constraints": [{"rule": "...", "status": "met/unmet/in_progress", "detail": "..."}]}
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    with database.get_db() as conn:
        # 获取本周计划训练
        workouts = conn.execute(
            "SELECT * FROM planned_workouts WHERE date >= ? AND date <= ? ORDER BY date",
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
        workouts = [dict(w) for w in workouts]

        # 获取本周实际活动
        activities = conn.execute(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
        activities = [dict(a) for a in activities]

        # 获取当前训练阶段约束
        try:
            from engine.plan_generator import detect_training_phase, _PHASE_CONSTRAINTS, TrainingPhase
            phase, _ = detect_training_phase(conn)
            phase_constraints = _PHASE_CONSTRAINTS.get(phase, _PHASE_CONSTRAINTS[TrainingPhase.BUILD])
        except Exception:
            phase_constraints = {"min_rest_days": 1, "max_intensity_days": 2, "max_daily_tss": 150}

    constraints = []
    week_done = today > sunday

    # ── 辅助：统计实际活动 ──
    active_dates = set(a.get("date", "")[:10] for a in activities)
    passed_dates = set()
    for i in range(7):
        d = monday + timedelta(days=i)
        if d <= today:
            passed_dates.add(d.isoformat())
    rest_dates_actual = passed_dates - active_dates
    rest_count_actual = len(rest_dates_actual)

    sport_actual: Dict[str, int] = {}
    for a in activities:
        s = a.get("sport", "unknown")
        sport_actual[s] = sport_actual.get(s, 0) + 1

    # 按日期汇总实际 TSS
    daily_tss: Dict[str, float] = {}
    for a in activities:
        d = a.get("date", "")[:10]
        daily_tss[d] = daily_tss.get(d, 0) + (a.get("tss") or 0)
    daily_planned_tss: Dict[str, float] = {}
    for w in workouts:
        d = w.get("date", "")[:10]
        daily_planned_tss[d] = daily_planned_tss.get(d, 0) + (w.get("target_tss") or 0)

    # ── 约束1: 周一休息 ──
    monday_str = monday.isoformat()
    monday_has_activity = monday_str in active_dates
    if monday_str in passed_dates:
        status = "met" if not monday_has_activity else "unmet"
        detail = "周一已休息" if not monday_has_activity else "周一有训练活动，未满足休息要求"
    else:
        status = "in_progress"
        detail = "周一尚未到来"
    constraints.append({"rule": "周一休息", "status": status, "detail": detail})

    # ── 约束2: 每周 3-4 次骑行 ──
    cycling_count = sport_actual.get("cycling", 0)
    if week_done:
        status = "met" if 3 <= cycling_count <= 4 else "unmet"
    else:
        status = "met" if cycling_count >= 3 else "in_progress"
    constraints.append({
        "rule": "每周 3-4 次骑行",
        "status": status,
        "detail": f"已完成 {cycling_count} 次骑行",
    })

    # ── 约束3: 每周至少 1 次跑步 ──
    running_count = sport_actual.get("running", 0)
    if week_done:
        status = "met" if running_count >= 1 else "unmet"
    else:
        status = "met" if running_count >= 1 else "in_progress"
    constraints.append({
        "rule": "每周至少 1 次跑步",
        "status": status,
        "detail": f"已完成 {running_count} 次跑步",
    })

    # ── 约束4: 每周 3-4 次力量 ──
    strength_count = sport_actual.get("training", 0)
    if week_done:
        status = "met" if 3 <= strength_count <= 4 else "unmet"
    else:
        status = "met" if strength_count >= 3 else "in_progress"
    constraints.append({
        "rule": "每周 3-4 次力量",
        "status": status,
        "detail": f"已完成 {strength_count} 次力量训练",
    })

    # ── 约束5: 避免连续 3 天高负荷 ──
    max_consecutive_high = 2
    high_tss_threshold = 80
    consecutive_high = 0
    max_found = 0
    for i in range(7):
        d = (monday + timedelta(days=i)).isoformat()
        tss = daily_tss.get(d, 0) if d <= today.isoformat() else daily_planned_tss.get(d, 0)
        if tss >= high_tss_threshold:
            consecutive_high += 1
            max_found = max(max_found, consecutive_high)
        else:
            consecutive_high = 0
    if max_found > max_consecutive_high:
        status = "unmet"
        detail = f"出现 {max_found} 天连续高负荷，超过上限"
    else:
        status = "met"
        detail = f"最长连续高负荷 {max_found} 天，当前满足"
    constraints.append({
        "rule": "避免连续 3 天高负荷",
        "status": status,
        "detail": detail,
    })

    # ── 约束6: 骑行强度课后次日不安排腿部大重量 ──
    # 检查高强度骑行日的次日是否有力量训练
    intensity_ride_dates = set()
    for w in workouts:
        if w.get("sport") == "cycling":
            intensity = (w.get("target_intensity") or "").lower()
            title = (w.get("title") or "").lower()
            if any(k in intensity for k in ("z4", "z5", "vo2", "threshold")) or "间歇" in title or "关键" in title:
                intensity_ride_dates.add(w.get("date", "")[:10])
    # 也检查实际高强度骑行
    for a in activities:
        if a.get("sport") == "cycling" and (a.get("tss") or 0) >= 80:
            intensity_ride_dates.add(a.get("date", "")[:10])

    leg_conflict = False
    for ride_date_str in intensity_ride_dates:
        next_day = (date.fromisoformat(ride_date_str) + timedelta(days=1)).isoformat()
        # 检查次日是否有力量训练（含腿部）
        for w in workouts:
            if w.get("date", "")[:10] == next_day and w.get("sport") == "training":
                mg = (w.get("muscle_groups") or w.get("muscle_groups_json") or "").lower()
                title = (w.get("title") or "").lower()
                if any(k in mg or k in title for k in ("quad", "leg", "hamstr", "glute", "下肢", "腿", "臀")):
                    leg_conflict = True
    constraints.append({
        "rule": "强度骑后次日不安排腿部大重量",
        "status": "unmet" if leg_conflict else "met",
        "detail": "存在冲突" if leg_conflict else "当前满足",
    })

    # ── 约束7: 每周总时长 10-12 小时 ──
    total_planned_min = sum(w.get("target_duration_min") or 0 for w in workouts)
    total_planned_hrs = total_planned_min / 60
    if total_planned_min > 0:
        if 10 <= total_planned_hrs <= 12:
            status = "met"
        elif total_planned_hrs < 10:
            status = "in_progress" if not week_done else "unmet"
        else:
            status = "unmet"
        constraints.append({
            "rule": "每周总时长 10-12 小时",
            "status": status,
            "detail": f"计划 {total_planned_hrs:.1f}h",
        })

    return {"ok": True, "week_start": monday.isoformat(), "week_end": sunday.isoformat(), "constraints": constraints}


# ═══════════════════════════════════════════════════════════════════════════════
# Web Dashboard — for visual monitoring
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """Main dashboard page — v1 Decision Cockpit."""
    with database.get_db() as conn:
        activities = database.list_activities(conn, days=365, limit=100)
        fitness = database.list_fitness_history(conn, days=180)
        pdc_season = database.get_pdc_bests(conn, days=90)
        pdc_alltime = database.get_pdc_bests(conn, days=9999)
        val = validator.validation_dashboard(30)
        wk_stats = database.weekly_stats(conn)
        wellness = database.list_wellness(conn, days=30)

        # v1: 结论层数据
        readiness = compute_readiness(conn)
        deviation = compute_weekly_deviation(conn)
        metric_cards = get_metric_comparisons(conn)
        decision_summary = compute_decision_summary(conn)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "activities": activities,
        "fitness": fitness,
        "pdc_season": pdc_season,
        "pdc_alltime": pdc_alltime,
        "validation": val,
        "weekly": wk_stats,
        "wellness": wellness,
        # v1: Decision Cockpit
        "readiness": readiness.to_dict(),
        "deviation": deviation.to_dict(),
        "metric_cards": metric_cards,
        "decision_summary": decision_summary,
    })


@app.get("/activity/{activity_id}", response_class=HTMLResponse)
def activity_detail(request: Request, activity_id: int):
    """Activity detail page."""
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
        if not activity:
            raise HTTPException(404)

        # Get records for charts
        records = conn.execute(
            "SELECT * FROM records WHERE activity_id = ? ORDER BY offset_s",
            (activity_id,),
        ).fetchall()
        records = [dict(r) for r in records]

        # Get AI review if available
        ai_review = database.get_ai_review(conn, activity_id)

    # Parse JSON
    pz = json.loads(activity["power_zones_json"]) if activity.get("power_zones_json") else []
    hz = json.loads(activity["hr_zones_json"]) if activity.get("hr_zones_json") else []
    pdc = json.loads(activity["pdc_json"]) if activity.get("pdc_json") else {}
    laps = json.loads(activity["laps_json"]) if activity.get("laps_json") else []
    validation = json.loads(activity["validation_json"]) if activity.get("validation_json") else None

    return templates.TemplateResponse("activity.html", {
        "request": request,
        "activity": activity,
        "records": records,
        "power_zones": pz,
        "hr_zones": hz,
        "pdc": pdc,
        "laps": laps,
        "validation": validation,
        "ai_review": ai_review,
    })


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, week: Optional[str] = None):
    """Training plan weekly calendar page."""
    ref = date.fromisoformat(week) if week else date.today()
    week_start = ref - timedelta(days=ref.weekday())  # Monday
    week_end = week_start + timedelta(days=6)          # Sunday

    with database.get_db() as conn:
        # Auto-match actual activities to planned workouts
        database.match_compliance(conn, None)
        conn.commit()

        workouts = database.list_planned_workouts(conn, week_start.isoformat(), week_end.isoformat())

        # Actual activities for the week
        activities = conn.execute(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchall()
        activities = [dict(a) for a in activities]

        # Muscle fatigue for today
        muscle_fatigue = database.get_muscle_fatigue(conn, date.today().isoformat())

        # Latest fitness (CTL/ATL/TSB)
        fitness_row = conn.execute(
            "SELECT * FROM fitness_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        fitness = dict(fitness_row) if fitness_row else None

        # Check if API key is configured
        has_api_key = bool(database.get_setting(conn, "llm_api_key"))

        # Load athlete profile from settings
        from engine.plan_generator import (
            DEFAULT_PROFILE, detect_training_phase, _PHASE_CONSTRAINTS,
            TrainingPhase,
        )
        athlete = dict(DEFAULT_PROFILE)
        for key in DEFAULT_PROFILE:
            val = database.get_setting(conn, f"athlete_{key}")
            if val:
                if key in ("ftp", "max_hr", "resting_hr", "weekly_hours_available"):
                    athlete[key] = float(val) if "." in val else int(val)
                elif key == "constraints":
                    try:
                        athlete[key] = json.loads(val)
                    except Exception:
                        pass
                else:
                    athlete[key] = val

        # ── P0: Training phase & trigger info ──
        try:
            phase, phase_reason = detect_training_phase(conn)
        except Exception:
            phase, phase_reason = TrainingPhase.BASE, "检测失败，默认基础期"
        phase_constraints = _PHASE_CONSTRAINTS.get(phase, _PHASE_CONSTRAINTS[TrainingPhase.BUILD])

        # Last generation metadata (AI reasoning)
        last_plan_phase = database.get_setting(conn, "last_plan_phase") or ""
        last_plan_trigger = database.get_setting(conn, "last_plan_trigger") or ""
        last_plan_generated_at = database.get_setting(conn, "last_plan_generated_at") or ""

        # ── P0: Week summary stats ──
        sport_counts = {}
        total_duration_min = 0
        for w in workouts:
            s = w.get("sport", "rest")
            sport_counts[s] = sport_counts.get(s, 0) + 1
            total_duration_min += (w.get("target_duration_min") or 0)
        total_hours = total_duration_min / 60

        # ── P1: Last week actual stats ──
        prev_monday = week_start - timedelta(days=7)
        prev_sunday = prev_monday + timedelta(days=6)
        prev_workouts = database.list_planned_workouts(conn, prev_monday.isoformat(), prev_sunday.isoformat())
        prev_activities = conn.execute(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (prev_monday.isoformat(), prev_sunday.isoformat()),
        ).fetchall()
        prev_activities = [dict(a) for a in prev_activities]
        prev_tss_planned = sum(w.get('target_tss') or 0 for w in prev_workouts)
        prev_tss_actual = sum(a.get('tss') or 0 for a in prev_activities)
        prev_completed = sum(1 for w in prev_workouts if w.get('compliance_status') == 'completed')
        prev_total = len(prev_workouts)

        # ── P1: Next week preview ──
        next_monday = week_start + timedelta(days=7)
        next_sunday = next_monday + timedelta(days=6)
        next_workouts = database.list_planned_workouts(conn, next_monday.isoformat(), next_sunday.isoformat())
        next_tss_planned = sum(w.get('target_tss') or 0 for w in next_workouts)
        next_sport_counts = {}
        for w in next_workouts:
            s = w.get("sport", "rest")
            next_sport_counts[s] = next_sport_counts.get(s, 0) + 1

    # Build weekdays
    day_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    weekdays = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        ds = d.isoformat()
        day_workouts = [w for w in workouts if w['date'] == ds]
        day_activities = [a for a in activities if (a.get('date') or '')[:10] == ds]
        weekdays.append({
            'date': ds,
            'day_name': day_names[i],
            'day_short': f'{d.month}/{d.day}',
            'workouts': day_workouts,
            'activities': day_activities,
            'is_today': d == date.today(),
        })

    week_tss_planned = sum(w.get('target_tss') or 0 for w in workouts)
    week_tss_actual = sum(a.get('tss') or 0 for a in activities)
    completed = sum(1 for w in workouts if w.get('compliance_status') == 'completed')

    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()

    # Phase display names
    phase_names = {
        "base": "基础期 Base", "build": "构建期 Build", "peak": "巅峰期 Peak",
        "recovery": "恢复期 Recovery", "transition": "过渡期 Transition",
    }

    # v1: 偏差分析 + 就绪度
    with database.get_db() as conn_inner:
        deviation = compute_weekly_deviation(conn_inner, ref_date=week_start.isoformat())
        readiness = compute_readiness(conn_inner)

    return templates.TemplateResponse("plan.html", {
        "request": request,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "week_start_display": f"{week_start.month}月{week_start.day}日",
        "week_end_display": f"{week_end.month}月{week_end.day}日",
        "prev_week": prev_week,
        "next_week": next_week,
        "weekdays": weekdays,
        "workouts": workouts,
        "muscle_fatigue": muscle_fatigue,
        "week_tss_planned": week_tss_planned,
        "week_tss_actual": week_tss_actual,
        "completed": completed,
        "total_planned": len(workouts),
        "fitness": fitness,
        "has_api_key": has_api_key,
        "athlete": athlete,
        # P0: Phase & reasoning
        "phase": phase,
        "phase_name": phase_names.get(phase, phase),
        "phase_reason": phase_reason,
        "phase_constraints": phase_constraints,
        "last_plan_trigger": last_plan_trigger,
        "last_plan_generated_at": last_plan_generated_at,
        # P0: Week summary
        "sport_counts": sport_counts,
        "total_hours": total_hours,
        # P1: Last week
        "prev_tss_planned": prev_tss_planned,
        "prev_tss_actual": prev_tss_actual,
        "prev_completed": prev_completed,
        "prev_total": prev_total,
        # P1: Next week
        "next_tss_planned": next_tss_planned,
        "next_sport_counts": next_sport_counts,
        "next_total": len(next_workouts),
        # v1: Decision Cockpit
        "deviation": deviation.to_dict(),
        "readiness": readiness.to_dict(),
    })


@app.post("/api/workouts", dependencies=[Depends(verify_api_key)])
async def api_upsert_workout(request: Request):
    """Create or update a planned workout."""
    data = await request.json()
    with database.get_db() as conn:
        database.upsert_planned_workout(conn, data)
    return {"ok": True}


@app.delete("/api/workouts/{workout_id}", dependencies=[Depends(verify_api_key)])
def api_delete_workout(workout_id: int):
    """Delete a planned workout."""
    with database.get_db() as conn:
        database.delete_planned_workout(conn, workout_id)
    return {"ok": True}


@app.get("/api/workouts", dependencies=[Depends(verify_api_key)])
def api_list_workouts(
    date_from: str = Query(...),
    date_to: str = Query(...),
):
    """List planned workouts for a date range."""
    with database.get_db() as conn:
        rows = database.list_planned_workouts(conn, date_from, date_to)
    return {"ok": True, "count": len(rows), "workouts": rows}


@app.get("/api/calendar.ics", dependencies=[Depends(verify_api_key)])
def api_calendar_ics(
    days: int = Query(30, ge=1, le=180),
):
    """ICS calendar feed for planned workouts.

    Supports iOS calendar subscription via ?api_key=xxx query param
    (verify_api_key checks both header and query param).
    """
    from engine.calendar import generate_ics, get_workouts_for_calendar

    with database.get_db() as conn:
        workouts = get_workouts_for_calendar(conn, days=days)
    ics_content = generate_ics(workouts)
    return Response(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "inline; filename=training-edge.ics"},
    )


@app.get("/api/intervals/week-plan", dependencies=[Depends(verify_api_key)])
def api_intervals_week_plan(week: int = Query(0, ge=-4, le=4)):
    """Get planned events from Intervals.icu for a given week.

    week=0 is this week, week=1 is next week, etc.
    Merges Intervals cycling/running plan with TrainingEdge strength suggestions.
    """
    from engine import intervals
    if not intervals.is_configured():
        return {"ok": False, "error": "Intervals.icu not configured"}

    try:
        plan = intervals.fetch_week_plan(week_offset=week)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Suggest strength days on rest days (skip Monday = index 0)
    strength_suggestions = []
    for d in plan["rest_days"]:
        from datetime import date as dt_date
        weekday = dt_date.fromisoformat(d).weekday()
        if weekday == 0:  # Monday = rest
            continue
        strength_suggestions.append({
            "date": d,
            "type": "Strength",
            "name": "力量训练" if weekday in (2, 4) else "轻量辅助训练",
            "source": "TrainingEdge",
        })

    plan["strength_suggestions"] = strength_suggestions[:3]  # max 3 strength days
    plan["ok"] = True
    return plan


@app.post("/api/templates", dependencies=[Depends(verify_api_key)])
async def api_upsert_template(request: Request):
    """Create or update a weekly template."""
    data = await request.json()
    with database.get_db() as conn:
        database.upsert_weekly_template(conn, data)
    return {"ok": True}


@app.get("/api/templates", dependencies=[Depends(verify_api_key)])
def api_list_templates():
    """List all weekly templates."""
    with database.get_db() as conn:
        rows = database.list_weekly_templates(conn)
    return {"ok": True, "count": len(rows), "templates": rows}


@app.post("/api/generate-plan")
async def api_generate_plan(request: Request):
    """AI 生成训练计划。

    Body JSON:
      - profile: optional athlete profile overrides
      - week_offset: 0=本周, 1=下周 (default 1)
    """
    data = await request.json()
    profile = data.get("profile")
    week_offset = data.get("week_offset", 1)

    try:
        from engine.plan_generator import generate_weekly_plan, save_plan
        with database.get_db() as conn:
            workouts = generate_weekly_plan(conn, profile=profile, week_offset=week_offset)
            count = save_plan(conn, workouts)
        return {
            "ok": True,
            "count": count,
            "workouts": workouts,
            "message": f"已生成 {count} 个训练计划",
        }
    except ImportError as e:
        raise HTTPException(500, f"依赖缺失: {e}")
    except Exception as e:
        raise HTTPException(500, f"生成失败: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Activity AI Review — 活动 AI 复盘
# ═══════════════════════════════════════════════════════════════════════════════

def generate_activity_review(conn, activity_id: int) -> dict:
    """生成活动 AI 复盘。

    1. 获取活动数据
    2. 获取当日健康数据
    3. 获取当日计划训练（如有）
    4. 调用 LLM 生成结构化复盘
    5. 解析并存储结果
    """
    from engine import llm_client

    activity = database.get_activity(conn, activity_id)
    if not activity:
        raise HTTPException(404, f"活动 {activity_id} 不存在")

    activity_date = activity.get("date", "")
    sport = activity.get("sport", "cycling")

    # 获取当日健康/体能数据
    wellness = database.get_wellness(conn, activity_date) if activity_date else None
    fitness_row = conn.execute(
        "SELECT * FROM fitness_history WHERE date = ?", (activity_date,)
    ).fetchone()
    fitness = dict(fitness_row) if fitness_row else None

    # 获取当日计划训练
    planned = conn.execute(
        "SELECT * FROM planned_workouts WHERE date = ? ORDER BY id", (activity_date,)
    ).fetchall()
    planned_workouts = [dict(p) for p in planned] if planned else []

    # 解析 JSON 字段
    power_zones = json.loads(activity["power_zones_json"]) if activity.get("power_zones_json") else None
    hr_zones = json.loads(activity["hr_zones_json"]) if activity.get("hr_zones_json") else None
    laps = json.loads(activity["laps_json"]) if activity.get("laps_json") else None

    # 构建活动摘要（给 LLM 的上下文）
    sport_names = {"cycling": "骑行", "running": "跑步", "training": "力量训练"}
    sport_cn = sport_names.get(sport, sport)
    is_running = "running" in sport

    # 通用字段
    activity_summary = f"""活动名称: {activity.get('name', '未知')}
运动类型: {sport_cn}
日期: {activity_date}
距离: {round(activity['distance_m'] / 1000, 1) if activity.get('distance_m') else '无'}km
总时长: {round(activity['total_elapsed_s'] / 60) if activity.get('total_elapsed_s') else '无'}分钟
运动时间: {round(activity['total_timer_s'] / 60) if activity.get('total_timer_s') else '无'}分钟
平均心率: {activity.get('avg_hr') or '无'}bpm
最大心率: {activity.get('max_hr') or '无'}bpm"""

    if is_running:
        # 跑步配速
        pace_str = "无"
        if activity.get('avg_speed') and activity['avg_speed'] > 0:
            pace_total_s = int(1000 / activity['avg_speed'])
            pace_str = f"{pace_total_s // 60}:{pace_total_s % 60:02d} min/km"
        activity_summary += f"""
平均配速: {pace_str}
平均步频: {activity.get('avg_cadence') or '无'} spm
VDOT: {round(activity['vdot'], 1) if activity.get('vdot') else '无'}
触地时间: {round(activity['avg_stance_time_ms']) if activity.get('avg_stance_time_ms') else '无'}ms
垂直振幅: {round(activity['avg_vertical_osc_cm'], 1) if activity.get('avg_vertical_osc_cm') else '无'}cm
步幅: {round(activity['avg_step_length_cm']) if activity.get('avg_step_length_cm') else '无'}cm
爬升: {round(activity['total_ascent']) if activity.get('total_ascent') else '无'}m
TSS: {round(activity['tss']) if activity.get('tss') else '无'}
TRIMP: {round(activity['trimp']) if activity.get('trimp') else '无'}
心率漂移: {f"{round(activity['drift_pct'], 1)}% ({activity.get('drift_classification', '')})" if activity.get('drift_pct') is not None else '无'}
有氧训练效果: {activity.get('aerobic_te') or '无'}
无氧训练效果: {activity.get('anaerobic_te') or '无'}
热量: {activity.get('total_calories') or '无'}kcal"""
    else:
        activity_summary += f"""
平均功率: {activity.get('avg_power') or '无'}W
最大功率: {activity.get('max_power') or '无'}W
标准化功率(NP): {round(activity['normalized_power']) if activity.get('normalized_power') else '无'}W
TSS: {round(activity['tss']) if activity.get('tss') else '无'}
IF: {round(activity['intensity_factor'], 2) if activity.get('intensity_factor') else '无'}
xPower: {round(activity['xpower']) if activity.get('xpower') else '无'}W
FTP(设备): {activity.get('device_ftp') or '无'}W
eFTP(估算): {round(activity['estimated_ftp']) if activity.get('estimated_ftp') else '无'}W
爬升: {round(activity['total_ascent']) if activity.get('total_ascent') else '无'}m
平均踏频: {activity.get('avg_cadence') or '无'}rpm
平均速度: {round(activity['avg_speed'] * 3.6, 1) if activity.get('avg_speed') else '无'}km/h
有氧训练效果: {activity.get('aerobic_te') or '无'}
无氧训练效果: {activity.get('anaerobic_te') or '无'}
心率漂移: {f"{round(activity['drift_pct'], 1)}% ({activity.get('drift_classification', '')})" if activity.get('drift_pct') is not None else '无'}
TRIMP: {round(activity['trimp']) if activity.get('trimp') else '无'}
碳水消耗: {round(activity['carbs_used_g']) if activity.get('carbs_used_g') else '无'}g
热量: {activity.get('total_calories') or '无'}kcal"""

    # 体能状态
    fitness_context = ""
    if fitness:
        fitness_context = f"""
当日体能状态:
  CTL(长期负荷): {fitness.get('ctl') or '无'}
  ATL(短期负荷): {fitness.get('atl') or '无'}
  TSB(体能余量): {fitness.get('tsb') or '无'}
  Ramp Rate: {fitness.get('ramp_rate') or '无'}"""

    # 健康数据
    wellness_context = ""
    if wellness:
        wellness_context = f"""
当日健康数据:
  静息心率: {wellness.get('resting_hr') or '无'}bpm
  HRV: {wellness.get('hrv') or '无'}ms
  睡眠时长: {wellness.get('sleep_hours') or '无'}小时
  睡眠评分: {wellness.get('sleep_score') or '无'}
  体重: {wellness.get('weight_kg') or '无'}kg"""

    # 计划训练
    plan_context = ""
    if planned_workouts:
        plan_items = []
        for pw in planned_workouts:
            plan_items.append(
                f"  - {pw.get('title', '未命名')}: {pw.get('sport', '')}, "
                f"目标时长 {pw.get('target_duration_min') or '无'}分钟, "
                f"目标TSS {pw.get('target_tss') or '无'}, "
                f"强度 {pw.get('target_intensity') or '无'}"
            )
        plan_context = f"\n当日计划训练:\n" + "\n".join(plan_items)

    # 功率区间
    zones_context = ""
    if power_zones:
        zone_names = {"z1": "Z1恢复", "z2": "Z2耐力", "z3": "Z3节奏", "z4": "Z4阈值", "z5": "Z5 VO2max", "z6": "Z6无氧", "z7": "Z7神经"}
        zone_lines = [f"  {zone_names.get(z['zone'], z['zone'])}: {z.get('pct', 0):.0f}%（{z.get('seconds', 0):.0f}秒）" for z in power_zones]
        zones_context = "\n功率区间分布:\n" + "\n".join(zone_lines)

    # LLM 提示词（根据运动类型切换）
    if is_running:
        system_prompt = """你是一位专业的跑步训练分析师，负责对跑步活动进行结构化复盘分析。

你的分析必须基于数据，客观、简洁、具有训练指导价值。重点关注跑步专有指标：配速、步频、VDOT、触地时间、垂直振幅、心率漂移等。

免责声明：本分析仅用于训练管理参考，不构成医学诊断或医疗建议。如有健康疑虑，请咨询专业医生。

请严格按照以下 JSON 格式输出，不要添加任何额外说明文字：

{
  "summary": {
    "overall_label": "一个2-4字的评价标签，如'轻松恢复跑'、'节奏跑'、'长距离慢跑'、'间歇训练'等",
    "one_line_summary": "一句话总结本次跑步的核心特征和训练价值",
    "completion_status": "完成度评价：完美执行/基本完成/部分完成/未完成",
    "fatigue_impact": "对疲劳的影响评价：低/中/高/极高",
    "plan_impact": "对后续训练计划的影响：无影响/轻微调整/需要调整/需要重新规划"
  },
  "key_findings": [
    "第一个关键发现（最重要的训练信号）",
    "第二个关键发现",
    "第三个关键发现"
  ],
  "narrative": {
    "training_type": "识别本次训练的类型和目的（恢复跑、有氧慢跑、节奏跑、间歇跑、长距离等），并说明判断依据",
    "execution_quality": "评估训练执行质量：配速稳定性、心率控制、步频一致性、触地时间等跑姿指标",
    "physiological_cost": "分析生理成本：TSS/TRIMP负荷、心率漂移、恢复需求",
    "capacity_signal": "分析能力信号：VDOT趋势、配速/心率效率、步频步幅平衡、跑步经济性",
    "abnormal_and_noise": "指出异常数据或干扰因素（如天气、地形、路面、设备问题等）",
    "next_steps": "基于本次训练结果，对后续1-2天训练的具体建议"
  },
  "confidence": {
    "level": "高/中/低",
    "reasons": ["影响置信度的因素1", "影响置信度的因素2"]
  }
}"""
    else:
        system_prompt = """你是一位专业的自行车运动训练分析师，负责对骑行活动进行结构化复盘分析。

你的分析必须基于数据，客观、简洁、具有训练指导价值。

免责声明：本分析仅用于训练管理参考，不构成医学诊断或医疗建议。如有健康疑虑，请咨询专业医生。

请严格按照以下 JSON 格式输出，不要添加任何额外说明文字：

{
  "summary": {
    "overall_label": "一个2-4字的评价标签，如'高质量耐力骑'、'恢复骑行'、'过度疲劳'等",
    "one_line_summary": "一句话总结本次骑行的核心特征和训练价值",
    "completion_status": "完成度评价：完美执行/基本完成/部分完成/未完成",
    "fatigue_impact": "对疲劳的影响评价：低/中/高/极高",
    "plan_impact": "对后续训练计划的影响：无影响/轻微调整/需要调整/需要重新规划"
  },
  "key_findings": [
    "第一个关键发现（最重要的训练信号）",
    "第二个关键发现",
    "第三个关键发现"
  ],
  "narrative": {
    "training_type": "识别本次训练的类型和目的（耐力骑、间歇训练、恢复骑等），并说明判断依据",
    "execution_quality": "评估训练执行质量：功率稳定性、心率控制、节奏把控等",
    "physiological_cost": "分析生理成本：TSS负荷、心率漂移、碳水消耗、恢复需求",
    "capacity_signal": "分析能力信号：eFTP变化、功率区间表现、是否有突破迹象",
    "abnormal_and_noise": "指出异常数据或干扰因素（如天气、设备问题、路况等）",
    "next_steps": "基于本次训练结果，对后续1-2天训练的具体建议"
  },
  "confidence": {
    "level": "高/中/低",
    "reasons": ["影响置信度的因素1", "影响置信度的因素2"]
  }
}"""

    user_prompt = f"""请对以下{sport_cn}活动进行结构化复盘分析：

{activity_summary}
{fitness_context}
{wellness_context}
{plan_context}
{zones_context}

请严格按照 JSON 格式输出分析结果。"""

    # 调用 LLM
    response_text = llm_client.chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=3000,
        temperature=0.5,
    )

    # 解析 LLM 响应
    review_data = llm_client.extract_json(response_text)

    # 补充元数据
    review_data["analysis_version"] = "run_review_v1" if is_running else "ride_review_v1"
    review_data["generated_at"] = datetime.now().isoformat()
    review_data["review_status"] = "completed"
    review_data["sport_type"] = sport

    # 存储到数据库
    database.upsert_ai_review(conn, activity_id, review_data)

    return review_data


@app.get("/api/activities/{activity_id}/ai-review")
def api_get_ai_review(activity_id: int):
    """获取活动的 AI 复盘结果，如不存在则自动生成。"""
    with database.get_db() as conn:
        review = database.get_ai_review(conn, activity_id)
        if review:
            return {"ok": True, "review": review, "source": "cached"}

        # 自动生成
        try:
            review_data = generate_activity_review(conn, activity_id)
            review = database.get_ai_review(conn, activity_id)
            return {"ok": True, "review": review, "source": "generated"}
        except Exception as e:
            logger.exception("AI 复盘生成失败: activity_id=%s", activity_id)
            raise HTTPException(500, f"AI 复盘生成失败: {e}")


@app.post("/api/activities/{activity_id}/ai-review/regenerate")
def api_regenerate_ai_review(activity_id: int):
    """重新生成活动的 AI 复盘。"""
    try:
        with database.get_db() as conn:
            review_data = generate_activity_review(conn, activity_id)
            return {"ok": True, "review": review_data, "message": "AI 复盘已重新生成"}
    except Exception as e:
        logger.exception("AI 复盘重新生成失败: activity_id=%s", activity_id)
        raise HTTPException(500, f"AI 复盘重新生成失败: {e}")


@app.get("/api/activities/{activity_id}/ai-review/summary")
def api_get_ai_review_summary(activity_id: int):
    """获取活动 AI 复盘的摘要部分（适合 Telegram 等简短场景）。"""
    with database.get_db() as conn:
        review = database.get_ai_review(conn, activity_id)
        if not review:
            raise HTTPException(404, "暂无 AI 复盘，请先生成")

        summary = review.get("summary", {})
        key_findings = review.get("key_findings", [])
        narrative = review.get("narrative", {})

        return {
            "ok": True,
            "activity_id": activity_id,
            "overall_label": summary.get("overall_label", ""),
            "one_line_summary": summary.get("one_line_summary", ""),
            "key_findings": key_findings,
            "next_steps": narrative.get("next_steps", ""),
            "fatigue_impact": summary.get("fatigue_impact", ""),
        }


@app.get("/body-data", response_class=HTMLResponse)
def body_data_page(request: Request):
    """Body composition data page — v1 Decision Cockpit."""
    with database.get_db() as conn:
        latest = database.get_latest_body_comp(conn)
        history = database.list_body_comp(conn, days=730)
        # Get the previous record for trend comparison
        previous = None
        if len(history) >= 2:
            previous = history[1]

        # Get latest Garmin-sourced record (has HRV, sleep, body battery)
        garmin_records = database.list_body_comp(conn, days=90, source="Garmin")
        latest_garmin = garmin_records[0] if garmin_records else None

        # Get wellness data for HRV/sleep trend charts (separate from body_comp)
        wellness_history = database.list_wellness(conn, days=90)

        # v1: 结论层数据
        body_trend = compute_body_trend_summary(conn)
        body_comparisons = get_body_comp_comparisons(conn)
        metric_cards = get_metric_comparisons(conn)

    return templates.TemplateResponse("body_data.html", {
        "request": request,
        "latest": latest,
        "previous": previous,
        "history": history,
        "latest_garmin": latest_garmin,
        "wellness_history": wellness_history,
        # v1: Decision Cockpit
        "body_trend": body_trend.to_dict(),
        "body_comparisons": body_comparisons,
        "metric_cards": metric_cards,
    })


@app.post("/api/body-composition", dependencies=[Depends(verify_api_key)])
async def api_upsert_body_comp(request: Request):
    """Upsert a body composition record."""
    data = await request.json()
    with database.get_db() as conn:
        database.upsert_body_comp(conn, data)
    return {"ok": True}


@app.get("/api/body-composition", dependencies=[Depends(verify_api_key)])
def api_list_body_comp(
    days: int = Query(90, ge=1, le=730),
    source: Optional[str] = None,
):
    """List body composition history."""
    with database.get_db() as conn:
        records = database.list_body_comp(conn, days=days, source=source)
    return {"ok": True, "count": len(records), "records": records}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Settings page — API config + athlete profile."""
    setting_keys = [
        "llm_api_key", "llm_api_base", "llm_proxy", "llm_model", "llm_vision_model",
        "athlete_ftp", "athlete_max_hr", "athlete_resting_hr",
        "athlete_goal", "athlete_focus", "athlete_weekly_hours_available",
        "athlete_event_name", "athlete_event_date",
        "athlete_constraints", "athlete_constraints_text",
        "garmin_token_path",
    ]
    settings = {}
    with database.get_db() as conn:
        for key in setting_keys:
            val = database.get_setting(conn, key)
            if val:
                # Mask API key for display (only show last 4 chars)
                if key == "llm_api_key" and len(val) > 8:
                    settings[key] = "sk-" + "*" * 20 + val[-4:]
                    settings["llm_api_key_set"] = True
                else:
                    settings[key] = val
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": settings,
    })


@app.post("/api/settings")
async def api_save_settings(request: Request):
    """Save settings to database."""
    data = await request.json()
    with database.get_db() as conn:
        for key, value in data.items():
            if value is not None and str(value).strip():
                # Don't overwrite API key if masked (mask format: sk-****...8975)
                if key == "llm_api_key" and "*" in str(value):
                    continue
                database.set_setting(conn, key, str(value))
    return {"ok": True}


@app.post("/api/test-llm")
async def api_test_llm():
    """Test LLM API connection."""
    try:
        from engine import llm_client
        text = llm_client.chat_completion(
            messages=[{"role": "user", "content": "Say 'TrainingEdge connected!' in one line."}],
            max_tokens=50,
            temperature=0,
        )
        return {"ok": True, "response": text.strip(), "model": llm_client.get_model()}
    except Exception as e:
        raise HTTPException(500, f"LLM 连接失败: {e}")


@app.post("/api/sync-garmin")
async def api_sync_garmin(request: Request):
    """Sync data from Garmin Connect."""
    data = await request.json()
    sync_type = data.get("type", "activities")

    try:
        from engine import sync as garmin_sync

        if sync_type == "wellness":
            result = garmin_sync.sync_garmin_wellness(days=14)
            msg = (
                f"已同步 {result['days_synced']} 天数据: "
                f"HRV {result['hrv_count']} 条, "
                f"睡眠 {result['sleep_count']} 条"
            )
            if result["errors"]:
                msg += f" (错误: {len(result['errors'])})"
            return {"ok": True, "message": msg, "detail": result}
        else:
            results = garmin_sync.sync_recent(days=7)
            # Auto-match to planned workouts
            with database.get_db() as conn:
                matched = database.match_compliance(conn)
            return {
                "ok": True,
                "message": f"已同步 {len(results)} 个活动" + (f"，匹配 {matched} 个计划" if matched else ""),
                "count": len(results),
            }
    except Exception as e:
        raise HTTPException(500, f"Garmin 同步失败: {e}")


@app.post("/api/inbody-ocr")
async def api_inbody_ocr(files: List[UploadFile] = File(...)):
    """Upload InBody photos and extract data using Claude Vision.

    Accepts 1-4 images. Returns extracted data and saves to database.
    No API key required (web form upload).
    """
    if not files:
        raise HTTPException(400, "请上传至少一张 InBody 照片")
    if len(files) > 4:
        raise HTTPException(400, "最多上传 4 张照片")

    # Read all images
    images = []
    for f in files:
        content = await f.read()
        if len(content) > 10 * 1024 * 1024:  # 10MB limit per image
            raise HTTPException(400, f"图片 {f.filename} 过大（最大 10MB）")
        images.append(content)

    try:
        from engine.inbody_ocr import extract_inbody_data
        data = extract_inbody_data(images)
    except ImportError as e:
        raise HTTPException(500, f"依赖缺失: {e}")
    except ValueError as e:
        raise HTTPException(422, f"识别失败: {e}")
    except Exception as e:
        raise HTTPException(500, f"识别出错: {e}")

    # Save to database
    with database.get_db() as conn:
        database.upsert_body_comp(conn, data)

    return {"ok": True, "data": data, "message": f"已识别并保存 {data.get('date', '未知日期')} 的 InBody 数据"}
