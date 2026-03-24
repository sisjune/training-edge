"""ICS calendar generation for planned workouts."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional
from uuid import uuid5, NAMESPACE_URL


# Sport emoji mapping
_SPORT_EMOJI = {
    "cycling": "🚴",
    "running": "🏃",
    "training": "💪",
    "rest": "🧘",
    "swimming": "🏊",
}

# Default start hour for workouts (when no specific time is set)
_DEFAULT_START_HOUR = 8


def _escape_ics(text: str) -> str:
    """Escape special characters for ICS text fields."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    """Fold long ICS lines at 75 octets per RFC 5545."""
    if len(line.encode("utf-8")) <= 75:
        return line
    result = []
    current = ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            result.append(current)
            current = " " + char  # continuation line starts with space
        else:
            current += char
    if current:
        result.append(current)
    return "\r\n".join(result)


def _workout_uid(workout: Dict[str, Any]) -> str:
    """Generate a stable UID for a workout."""
    seed = f"training-edge-workout-{workout.get('id', 0)}-{workout.get('date', '')}"
    return str(uuid5(NAMESPACE_URL, seed))


def workout_to_vevent(w: Dict[str, Any]) -> str:
    """Convert a single planned workout dict to a VEVENT block."""
    workout_date = w.get("date", "")
    if not workout_date:
        return ""

    sport = w.get("sport", "training")
    emoji = _SPORT_EMOJI.get(sport, "🏅")
    title = w.get("title", sport)
    summary = f"{emoji} {title}"

    duration_min = w.get("target_duration_min") or 60
    dt_start = datetime.fromisoformat(workout_date).replace(hour=_DEFAULT_START_HOUR)
    dt_end = dt_start + timedelta(minutes=duration_min)

    # Build description
    desc_parts = []
    if w.get("target_intensity"):
        desc_parts.append(f"强度: {w['target_intensity']}")
    if w.get("target_tss"):
        desc_parts.append(f"目标 TSS: {w['target_tss']:.0f}")
    if w.get("target_duration_min"):
        desc_parts.append(f"时长: {w['target_duration_min']:.0f}min")
    if w.get("description"):
        desc_parts.append("")
        desc_parts.append(w["description"][:500])

    status = w.get("compliance_status", "pending")
    if status == "completed":
        actual_tss = w.get("actual_tss")
        summary = f"✅ {summary}"
        if actual_tss:
            desc_parts.append(f"\n实际 TSS: {actual_tss:.0f}")

    description = _escape_ics("\n".join(desc_parts))
    uid = _workout_uid(w)
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{dt_start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND:{dt_end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_escape_ics(summary)}",
        f"DESCRIPTION:{description}",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape_ics(title)}",
        "END:VALARM",
        "END:VEVENT",
    ]
    return "\r\n".join(_fold_line(line) for line in lines)


def generate_ics(workouts: List[Dict[str, Any]], cal_name: str = "TrainingEdge") -> str:
    """Generate a complete ICS calendar string from a list of planned workouts."""
    header = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TrainingEdge//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{cal_name}",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ])

    events = []
    for w in workouts:
        if w.get("sport") == "rest":
            continue  # skip rest days
        vevent = workout_to_vevent(w)
        if vevent:
            events.append(vevent)

    footer = "END:VCALENDAR"
    parts = [header] + events + [footer]
    return "\r\n".join(parts)


def get_workouts_for_calendar(
    conn: sqlite3.Connection,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Fetch planned workouts for calendar generation.

    Merges:
    1. Intervals.icu planned events (cycling/running) — if configured
    2. TrainingEdge planned_workouts table (strength, local overrides)
    """
    from_date = date.today().isoformat()
    to_date = (date.today() + timedelta(days=days)).isoformat()

    # Local planned workouts (strength, etc.)
    rows = conn.execute(
        """SELECT * FROM planned_workouts
           WHERE date >= ? AND date <= ?
           ORDER BY date""",
        (from_date, to_date),
    ).fetchall()
    local_workouts = [dict(r) for r in rows]

    # Intervals.icu events (cycling/running)
    intervals_workouts = []
    try:
        from engine import intervals
        if intervals.is_configured():
            events = intervals.fetch_planned_events(from_date, to_date)
            for e in events:
                if e.get("category") != "WORKOUT":
                    continue
                sport_map = {"Ride": "cycling", "Run": "running", "Swim": "swimming"}
                intervals_workouts.append({
                    "date": e["date"],
                    "sport": sport_map.get(e.get("type"), "training"),
                    "title": e.get("name", ""),
                    "description": e.get("description", ""),
                    "target_tss": e.get("planned_tss"),
                    "target_duration_min": (e.get("planned_duration_s") or 0) / 60 or None,
                    "source": "intervals.icu",
                })
    except Exception:
        pass  # Intervals unavailable, use local only

    # Merge: Intervals events take priority for cycling/running
    intervals_dates = {(w["date"], w["sport"]) for w in intervals_workouts}
    merged = list(intervals_workouts)
    for w in local_workouts:
        key = (w.get("date"), w.get("sport"))
        if key not in intervals_dates:
            merged.append(w)

    merged.sort(key=lambda x: x.get("date", ""))
    return merged
