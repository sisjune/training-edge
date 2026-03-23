"""FIT file parser — extracts second-by-second records from Garmin .fit files."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from fitparse import FitFile


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """Single data point (typically 1-second resolution)."""
    timestamp: Optional[datetime] = None
    heart_rate: Optional[int] = None
    power: Optional[int] = None
    speed: Optional[float] = None          # m/s
    cadence: Optional[int] = None
    temperature: Optional[float] = None     # °C
    altitude: Optional[float] = None        # m
    position_lat: Optional[float] = None    # degrees
    position_long: Optional[float] = None   # degrees
    distance: Optional[float] = None        # cumulative meters
    enhanced_speed: Optional[float] = None  # m/s (higher resolution)
    enhanced_altitude: Optional[float] = None


@dataclass
class Lap:
    """Lap / split summary from FIT file."""
    start_time: Optional[datetime] = None
    total_elapsed_time: Optional[float] = None
    total_distance: Optional[float] = None
    avg_heart_rate: Optional[int] = None
    max_heart_rate: Optional[int] = None
    avg_power: Optional[int] = None
    max_power: Optional[int] = None
    avg_speed: Optional[float] = None
    max_speed: Optional[float] = None
    avg_cadence: Optional[int] = None
    total_ascent: Optional[float] = None
    total_descent: Optional[float] = None


@dataclass
class SessionSummary:
    """Top-level session / activity summary from FIT file."""
    sport: Optional[str] = None
    sub_sport: Optional[str] = None
    start_time: Optional[datetime] = None
    total_elapsed_time: Optional[float] = None
    total_timer_time: Optional[float] = None      # moving time
    total_distance: Optional[float] = None
    avg_heart_rate: Optional[int] = None
    max_heart_rate: Optional[int] = None
    avg_power: Optional[int] = None
    max_power: Optional[int] = None
    normalized_power: Optional[int] = None
    avg_speed: Optional[float] = None
    max_speed: Optional[float] = None
    avg_cadence: Optional[int] = None
    max_cadence: Optional[int] = None
    total_ascent: Optional[float] = None
    total_descent: Optional[float] = None
    total_calories: Optional[int] = None
    avg_temperature: Optional[float] = None
    training_effect: Optional[float] = None
    anaerobic_training_effect: Optional[float] = None
    threshold_power: Optional[int] = None          # FTP stored in device


@dataclass
class ParsedActivity:
    """Complete parsed result from a FIT file."""
    session: SessionSummary = field(default_factory=SessionSummary)
    records: List[Record] = field(default_factory=list)
    laps: List[Lap] = field(default_factory=list)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _semicircles_to_degrees(value: Optional[int]) -> Optional[float]:
    """Convert Garmin semicircle coordinates to decimal degrees."""
    if value is None:
        return None
    return value * (180.0 / 2**31)


def _get_field(message, name: str, fallback: str = None) -> Any:
    """Safely extract a field value from a FIT message."""
    val = message.get_value(name)
    if val is not None:
        return val
    if fallback:
        return message.get_value(fallback)
    return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        v = int(value)
        return v if v < 65535 else None  # FIT uses 65535 as invalid
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_fit(path: str | Path) -> ParsedActivity:
    """Parse a .fit file and return structured data.

    Args:
        path: Path to the .fit file.

    Returns:
        ParsedActivity with session summary, second-by-second records, and laps.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"FIT file not found: {path}")

    fit = FitFile(str(path))
    fit.parse()

    result = ParsedActivity()

    for message in fit.get_messages():
        msg_name = message.name

        if msg_name == "record":
            rec = Record(
                timestamp=_get_field(message, "timestamp"),
                heart_rate=_safe_int(_get_field(message, "heart_rate")),
                power=_safe_int(_get_field(message, "power")),
                speed=_safe_float(_get_field(message, "enhanced_speed", "speed")),
                cadence=_safe_int(_get_field(message, "cadence")),
                temperature=_safe_float(_get_field(message, "temperature")),
                altitude=_safe_float(_get_field(message, "enhanced_altitude", "altitude")),
                position_lat=_semicircles_to_degrees(_get_field(message, "position_lat")),
                position_long=_semicircles_to_degrees(_get_field(message, "position_long")),
                distance=_safe_float(_get_field(message, "distance")),
                enhanced_speed=_safe_float(_get_field(message, "enhanced_speed")),
                enhanced_altitude=_safe_float(_get_field(message, "enhanced_altitude")),
            )
            result.records.append(rec)

        elif msg_name == "lap":
            lap = Lap(
                start_time=_get_field(message, "start_time"),
                total_elapsed_time=_safe_float(_get_field(message, "total_elapsed_time")),
                total_distance=_safe_float(_get_field(message, "total_distance")),
                avg_heart_rate=_safe_int(_get_field(message, "avg_heart_rate")),
                max_heart_rate=_safe_int(_get_field(message, "max_heart_rate")),
                avg_power=_safe_int(_get_field(message, "avg_power")),
                max_power=_safe_int(_get_field(message, "max_power")),
                avg_speed=_safe_float(_get_field(message, "enhanced_avg_speed", "avg_speed")),
                max_speed=_safe_float(_get_field(message, "enhanced_max_speed", "max_speed")),
                avg_cadence=_safe_int(_get_field(message, "avg_cadence")),
                total_ascent=_safe_float(_get_field(message, "total_ascent")),
                total_descent=_safe_float(_get_field(message, "total_descent")),
            )
            result.laps.append(lap)

        elif msg_name == "session":
            s = result.session
            s.sport = _get_field(message, "sport")
            s.sub_sport = _get_field(message, "sub_sport")
            s.start_time = _get_field(message, "start_time")
            s.total_elapsed_time = _safe_float(_get_field(message, "total_elapsed_time"))
            s.total_timer_time = _safe_float(_get_field(message, "total_timer_time"))
            s.total_distance = _safe_float(_get_field(message, "total_distance"))
            s.avg_heart_rate = _safe_int(_get_field(message, "avg_heart_rate"))
            s.max_heart_rate = _safe_int(_get_field(message, "max_heart_rate"))
            s.avg_power = _safe_int(_get_field(message, "avg_power"))
            s.max_power = _safe_int(_get_field(message, "max_power"))
            s.normalized_power = _safe_int(_get_field(message, "normalized_power"))
            s.avg_speed = _safe_float(_get_field(message, "enhanced_avg_speed", "avg_speed"))
            s.max_speed = _safe_float(_get_field(message, "enhanced_max_speed", "max_speed"))
            s.avg_cadence = _safe_int(_get_field(message, "avg_cadence"))
            s.max_cadence = _safe_int(_get_field(message, "max_cadence"))
            s.total_ascent = _safe_float(_get_field(message, "total_ascent"))
            s.total_descent = _safe_float(_get_field(message, "total_descent"))
            s.total_calories = _safe_int(_get_field(message, "total_calories"))
            s.avg_temperature = _safe_float(_get_field(message, "avg_temperature"))
            s.training_effect = _safe_float(_get_field(message, "total_training_effect"))
            s.anaerobic_training_effect = _safe_float(
                _get_field(message, "total_anaerobic_training_effect")
            )
            s.threshold_power = _safe_int(_get_field(message, "threshold_power"))

        elif msg_name == "file_id":
            result.raw_metadata["manufacturer"] = _get_field(message, "manufacturer")
            result.raw_metadata["product"] = _get_field(message, "garmin_product")
            result.raw_metadata["serial_number"] = _get_field(message, "serial_number")
            result.raw_metadata["time_created"] = _get_field(message, "time_created")

    return result


def power_series(activity: ParsedActivity) -> List[int]:
    """Extract a clean power series (0 for missing values)."""
    return [r.power or 0 for r in activity.records if r.timestamp is not None]


def hr_series(activity: ParsedActivity) -> List[int]:
    """Extract a clean heart rate series (0 for missing values)."""
    return [r.heart_rate or 0 for r in activity.records if r.timestamp is not None]


def speed_series(activity: ParsedActivity) -> List[float]:
    """Extract speed series in m/s."""
    return [r.speed or 0.0 for r in activity.records if r.timestamp is not None]


def cadence_series(activity: ParsedActivity) -> List[int]:
    """Extract cadence series."""
    return [r.cadence or 0 for r in activity.records if r.timestamp is not None]
