"""Core training metrics — NP, TSS, IF, CTL/ATL/TSB, zones, eFTP, drift, TRIMP, VDOT."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Power-based metrics (Cycling)
# ═══════════════════════════════════════════════════════════════════════════════

def normalized_power(power_data: Sequence[int], window: int = 30) -> Optional[float]:
    """Calculate Normalized Power (NP).

    Algorithm (Coggan):
      1. Rolling 30-second average of power
      2. Raise each value to the 4th power
      3. Take the mean
      4. Take the 4th root

    Args:
        power_data: Second-by-second power values (watts).
        window: Rolling average window in seconds (default 30).

    Returns:
        NP in watts, or None if insufficient data.
    """
    if len(power_data) < window:
        return None

    # Rolling average
    rolling = []
    window_sum = sum(power_data[:window])
    rolling.append(window_sum / window)
    for i in range(window, len(power_data)):
        window_sum += power_data[i] - power_data[i - window]
        rolling.append(window_sum / window)

    if not rolling:
        return None

    # 4th power average, then 4th root
    fourth_power_sum = sum(v ** 4 for v in rolling)
    np = (fourth_power_sum / len(rolling)) ** 0.25
    return round(np, 1)


def intensity_factor(np_watts: float, ftp: float) -> Optional[float]:
    """Calculate Intensity Factor (IF = NP / FTP)."""
    if ftp <= 0:
        return None
    return round(np_watts / ftp, 3)


def training_stress_score(
    np_watts: float, ftp: float, duration_seconds: float
) -> Optional[float]:
    """Calculate Training Stress Score (TSS).

    TSS = (duration_s × NP × IF) / (FTP × 3600) × 100
    """
    if ftp <= 0 or duration_seconds <= 0:
        return None
    if_ = np_watts / ftp
    tss = (duration_seconds * np_watts * if_) / (ftp * 3600) * 100
    return round(tss, 1)


def xpower(power_data: Sequence[int], tau: float = 25.0) -> Optional[float]:
    """Calculate xPower (alternative to NP, used by some platforms).

    Uses exponentially-weighted moving average instead of simple rolling average.
    """
    if len(power_data) < 30:
        return None

    alpha = 1.0 - math.exp(-1.0 / tau)
    ewma = float(power_data[0])
    fourth_sum = 0.0
    count = 0

    for p in power_data:
        ewma = alpha * p + (1 - alpha) * ewma
        fourth_sum += ewma ** 4
        count += 1

    if count == 0:
        return None
    return round((fourth_sum / count) ** 0.25, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Fitness / Fatigue model (CTL / ATL / TSB)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DailyLoad:
    """Single day's training load entry."""
    day: date
    tss: float
    sport: str = "cycling"


@dataclass
class FitnessState:
    """CTL / ATL / TSB state for a given day."""
    day: date
    ctl: float       # Chronic Training Load (fitness, 42-day)
    atl: float       # Acute Training Load (fatigue, 7-day)
    tsb: float       # Training Stress Balance (form = CTL - ATL)
    ramp_rate: float  # CTL change per week


def compute_fitness_history(
    daily_loads: Sequence[DailyLoad],
    ctl_days: int = 42,
    atl_days: int = 7,
    initial_ctl: float = 0.0,
    initial_atl: float = 0.0,
) -> List[FitnessState]:
    """Compute CTL/ATL/TSB time series using exponentially weighted moving average.

    Args:
        daily_loads: Sorted list of (date, tss) pairs. Missing days = 0 TSS.
        ctl_days: CTL time constant (default 42 days).
        atl_days: ATL time constant (default 7 days).
        initial_ctl: Starting CTL value (for cold-start, seed from intervals.icu).
        initial_atl: Starting ATL value.

    Returns:
        List of FitnessState for each day in the range.
    """
    if not daily_loads:
        return []

    # Build a day→tss lookup
    tss_by_day: Dict[date, float] = {}
    for dl in daily_loads:
        tss_by_day[dl.day] = tss_by_day.get(dl.day, 0.0) + dl.tss

    start = min(tss_by_day.keys())
    end = max(max(tss_by_day.keys()), date.today())

    ctl_decay = 1.0 - math.exp(-1.0 / ctl_days)
    atl_decay = 1.0 - math.exp(-1.0 / atl_days)

    ctl = initial_ctl
    atl = initial_atl
    prev_ctl = ctl
    history: List[FitnessState] = []

    current = start
    day_count = 0
    while current <= end:
        tss = tss_by_day.get(current, 0.0)
        ctl = ctl + ctl_decay * (tss - ctl)
        atl = atl + atl_decay * (tss - atl)
        tsb = ctl - atl

        # Ramp rate = CTL change per week
        ramp_rate = 0.0
        if day_count >= 7:
            week_ago_idx = max(0, len(history) - 7)
            ramp_rate = round(ctl - history[week_ago_idx].ctl, 2)

        history.append(FitnessState(
            day=current,
            ctl=round(ctl, 2),
            atl=round(atl, 2),
            tsb=round(tsb, 2),
            ramp_rate=ramp_rate,
        ))

        prev_ctl = ctl
        current += timedelta(days=1)
        day_count += 1

    return history


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Power Zones
# ═══════════════════════════════════════════════════════════════════════════════

# Coggan classic 7-zone model (% of FTP)
POWER_ZONE_BOUNDS = {
    "z1": (0.00, 0.55),     # Active Recovery
    "z2": (0.55, 0.75),     # Endurance
    "z3": (0.75, 0.90),     # Tempo
    "z4": (0.90, 1.05),     # Threshold
    "z5": (1.05, 1.20),     # VO2max
    "z6": (1.20, 1.50),     # Anaerobic Capacity
    "z7": (1.50, 99.9),     # Neuromuscular
}


@dataclass
class ZoneTime:
    """Time spent in a single zone."""
    zone: str
    seconds: float
    pct: float
    watts_low: float
    watts_high: float


def power_zone_distribution(
    power_data: Sequence[int], ftp: float, zone_model: Dict[str, Tuple[float, float]] = None
) -> List[ZoneTime]:
    """Calculate time-in-zone distribution from second-by-second power.

    Args:
        power_data: Power values (1 value = 1 second).
        ftp: Functional Threshold Power in watts.
        zone_model: Optional custom zone boundaries (pct of FTP).

    Returns:
        List of ZoneTime, one per zone.
    """
    if zone_model is None:
        zone_model = POWER_ZONE_BOUNDS

    zones = {z: 0.0 for z in zone_model}
    total = len(power_data)

    for p in power_data:
        if p <= 0:
            zones["z1"] += 1
            continue
        ratio = p / ftp
        placed = False
        for zone_name, (lo, hi) in zone_model.items():
            if lo <= ratio < hi:
                zones[zone_name] += 1
                placed = True
                break
        if not placed:
            zones["z7"] += 1  # above everything

    result = []
    for zone_name, (lo, hi) in zone_model.items():
        secs = zones[zone_name]
        result.append(ZoneTime(
            zone=zone_name,
            seconds=secs,
            pct=round(secs / total * 100, 1) if total > 0 else 0.0,
            watts_low=round(lo * ftp),
            watts_high=round(min(hi, 10.0) * ftp),
        ))
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Heart Rate Zones
# ═══════════════════════════════════════════════════════════════════════════════

# 5-zone HR model (% of max HR)
HR_ZONE_BOUNDS = {
    "z1": (0.00, 0.60),     # Recovery
    "z2": (0.60, 0.70),     # Easy Aerobic
    "z3": (0.70, 0.80),     # Aerobic
    "z4": (0.80, 0.90),     # Threshold
    "z5": (0.90, 1.00),     # VO2max / Anaerobic
}


def hr_zone_distribution(
    hr_data: Sequence[int], max_hr: int, zone_model: Dict[str, Tuple[float, float]] = None
) -> List[ZoneTime]:
    """Calculate time-in-zone distribution from second-by-second heart rate."""
    if zone_model is None:
        zone_model = HR_ZONE_BOUNDS
    if max_hr <= 0:
        return []

    zones = {z: 0.0 for z in zone_model}
    total = len([h for h in hr_data if h > 0])

    for hr in hr_data:
        if hr <= 0:
            continue
        ratio = hr / max_hr
        placed = False
        for zone_name, (lo, hi) in zone_model.items():
            if lo <= ratio < hi:
                zones[zone_name] += 1
                placed = True
                break
        if not placed:
            zones["z5"] += 1

    result = []
    for zone_name, (lo, hi) in zone_model.items():
        secs = zones[zone_name]
        result.append(ZoneTime(
            zone=zone_name,
            seconds=secs,
            pct=round(secs / total * 100, 1) if total > 0 else 0.0,
            watts_low=round(lo * max_hr),
            watts_high=round(min(hi, 1.0) * max_hr),
        ))
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Power Duration Curve & eFTP
# ═══════════════════════════════════════════════════════════════════════════════

# Standard durations to compute (seconds)
PDC_DURATIONS = [1, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 2400, 3600, 5400, 7200]


def max_mean_power(power_data: Sequence[int], duration: int) -> Optional[float]:
    """Find the max average power for a given duration window.

    Uses sliding window approach. O(n) time.
    """
    n = len(power_data)
    if n < duration or duration <= 0:
        return None

    window_sum = sum(power_data[:duration])
    best = window_sum

    for i in range(duration, n):
        window_sum += power_data[i] - power_data[i - duration]
        if window_sum > best:
            best = window_sum

    return round(best / duration, 1)


def power_duration_curve(power_data: Sequence[int], durations: Sequence[int] = None) -> Dict[int, Optional[float]]:
    """Compute max mean power for a set of standard durations.

    Args:
        power_data: Second-by-second power.
        durations: List of durations in seconds (default: standard set).

    Returns:
        Dict mapping duration (seconds) → max average power (watts).
    """
    if durations is None:
        durations = PDC_DURATIONS

    return {d: max_mean_power(power_data, d) for d in durations}


def estimate_ftp_from_pdc(pdc: Dict[int, Optional[float]]) -> Optional[float]:
    """Estimate FTP from power duration curve.

    Method 1: 95% of 20-minute best (classic Coggan)
    Method 2: If no 20-min data, use 20-min extrapolation from available data
    """
    # Try 20-minute power × 0.95
    p20 = pdc.get(1200)
    if p20 is not None:
        return round(p20 * 0.95, 1)

    # Fallback: 60-minute power (pure FTP definition)
    p60 = pdc.get(3600)
    if p60 is not None:
        return round(p60, 1)

    return None


def w_prime(pdc: Dict[int, Optional[float]], ftp: float) -> Optional[float]:
    """Estimate W' (W-prime, anaerobic work capacity) in joules.

    Simple method: W' = (P5min - FTP) × 300
    More accurate methods exist (3-parameter CP model) but this is a reasonable start.
    """
    p5 = pdc.get(300)
    if p5 is None or p5 <= ftp:
        return None
    return round((p5 - ftp) * 300, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: HR Drift / Decoupling
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DriftResult:
    """Heart rate drift analysis result."""
    method: str                  # "hr_power" or "hr_speed"
    drift_pct: float             # positive = HR drifted up relative to output
    first_half_ratio: float
    second_half_ratio: float
    classification: str          # "stable", "moderate", "high"
    note: str


def compute_hr_drift(
    hr_data: Sequence[int],
    output_data: Sequence[float],
    method: str = "hr_power",
) -> Optional[DriftResult]:
    """Compute cardiac drift / decoupling.

    Compares HR:output ratio between first and second half of the activity.

    Args:
        hr_data: Second-by-second heart rate.
        output_data: Second-by-second power (watts) or speed (m/s).
        method: "hr_power" or "hr_speed".

    Returns:
        DriftResult or None if insufficient data.
    """
    # Pair up valid data points
    pairs = [(hr, out) for hr, out in zip(hr_data, output_data) if hr > 0 and out > 0]
    if len(pairs) < 600:  # minimum 10 minutes
        return None

    mid = len(pairs) // 2
    first_half = pairs[:mid]
    second_half = pairs[mid:]

    avg_hr_1 = sum(h for h, _ in first_half) / len(first_half)
    avg_out_1 = sum(o for _, o in first_half) / len(first_half)
    avg_hr_2 = sum(h for h, _ in second_half) / len(second_half)
    avg_out_2 = sum(o for _, o in second_half) / len(second_half)

    if avg_out_1 <= 0 or avg_out_2 <= 0:
        return None

    ratio_1 = avg_hr_1 / avg_out_1
    ratio_2 = avg_hr_2 / avg_out_2

    if ratio_1 <= 0:
        return None

    drift_pct = ((ratio_2 / ratio_1) - 1.0) * 100.0
    abs_drift = abs(drift_pct)

    if abs_drift <= 5:
        classification = "stable"
        note = "心率漂移很低，说明有氧耐力表现稳定。"
    elif abs_drift <= 8:
        classification = "moderate"
        note = "心率有轻度漂移，关注配速和补给。"
    else:
        classification = "high"
        note = "心率漂移偏大，检查前半程配速、补给和累积疲劳。"

    if method == "hr_speed":
        note += "（使用 HR/速度 作为代理指标，因为没有功率数据）"

    return DriftResult(
        method=method,
        drift_pct=round(drift_pct, 2),
        first_half_ratio=round(ratio_1, 4),
        second_half_ratio=round(ratio_2, 4),
        classification=classification,
        note=note,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: Running metrics — TRIMP, VDOT, Running Effectiveness
# ═══════════════════════════════════════════════════════════════════════════════

def trimp_exp(
    hr_data: Sequence[int],
    resting_hr: int,
    max_hr: int,
    gender: str = "male",
) -> Optional[float]:
    """Calculate exponential TRIMP (Banister model).

    TRIMP = sum over each minute of:
        duration_min × delta_HR_ratio × 0.64 × e^(1.92 × delta_HR_ratio)  [male]
        duration_min × delta_HR_ratio × 0.86 × e^(1.67 × delta_HR_ratio)  [female]

    Where delta_HR_ratio = (HR - HRrest) / (HRmax - HRrest)
    """
    hr_range = max_hr - resting_hr
    if hr_range <= 0:
        return None

    if gender == "male":
        a, b = 0.64, 1.92
    else:
        a, b = 0.86, 1.67

    total = 0.0
    for hr in hr_data:
        if hr <= resting_hr:
            continue
        delta = (hr - resting_hr) / hr_range
        delta = min(delta, 1.0)  # cap at max HR
        total += (1.0 / 60.0) * delta * a * math.exp(b * delta)

    return round(total, 1)


def vdot_from_race(distance_m: float, time_seconds: float) -> Optional[float]:
    """Estimate VDOT from a race or time trial result.

    Based on Jack Daniels' running formula.
    Uses the simplified Daniels/Gilbert equation.
    """
    if distance_m <= 0 or time_seconds <= 0:
        return None

    time_min = time_seconds / 60.0
    velocity = distance_m / time_min  # m/min

    # Percent VO2max utilized (based on duration)
    pct_max = (0.8 + 0.1894393 * math.exp(-0.012778 * time_min)
               + 0.2989558 * math.exp(-0.1932605 * time_min))

    # VO2 cost of running at this velocity
    vo2 = -4.60 + 0.182258 * velocity + 0.000104 * velocity ** 2

    if pct_max <= 0:
        return None

    vdot = vo2 / pct_max
    return round(vdot, 1)


def race_prediction(vdot: float, target_distance_m: float) -> Optional[float]:
    """Predict race time for a given distance based on VDOT.

    Uses Riegel's formula as a practical approximation:
        T2 = T1 × (D2/D1)^1.06

    But here we use a direct VDOT-based approach for more accuracy.
    Returns predicted time in seconds.
    """
    # Iterative solver: find time that produces this VDOT for target distance
    # Simple binary search
    low, high = 60.0, 86400.0  # 1 min to 24 hours

    for _ in range(100):
        mid = (low + high) / 2
        estimated_vdot = vdot_from_race(target_distance_m, mid)
        if estimated_vdot is None:
            return None
        if estimated_vdot > vdot:
            low = mid
        else:
            high = mid

    return round((low + high) / 2, 0)


def running_effectiveness(speed_mps: float, hr_bpm: int) -> Optional[float]:
    """Running Effectiveness index = speed (m/min) / HR.

    Higher is better. Typical range: 0.9 - 1.15 for trained runners.
    """
    if hr_bpm <= 0 or speed_mps <= 0:
        return None
    speed_mpm = speed_mps * 60.0  # m/min
    return round(speed_mpm / hr_bpm, 3)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: Carbohydrate estimation
# ═══════════════════════════════════════════════════════════════════════════════

def estimate_carbs_used(
    power_data: Sequence[int],
    hr_data: Sequence[int],
    max_hr: int,
    weight_kg: float = 62.0,
) -> Optional[float]:
    """Estimate carbohydrate usage during exercise.

    Model: Based on total energy expenditure and intensity-dependent substrate
    utilization (higher intensity → higher carb fraction).

    Simplified Jeukendrup model:
        - Energy (kJ) = sum of power (watts × seconds) / 1000
        - At ~60% VO2max: ~50% carbs, ~50% fat
        - At ~85% VO2max: ~75% carbs, ~25% fat
        - Linear interpolation between intensity levels
    """
    if not power_data or max_hr <= 0:
        return None

    total_kj = sum(p for p in power_data) / 1000.0  # total work in kJ
    valid_hr = [h for h in hr_data if h > 0]
    if not valid_hr:
        return None

    avg_hr = sum(valid_hr) / len(valid_hr)
    intensity = avg_hr / max_hr  # fraction of max HR

    # Carb fraction based on intensity (approximate)
    # Based on published substrate utilization curves
    if intensity < 0.5:
        carb_fraction = 0.30
    elif intensity < 0.65:
        carb_fraction = 0.30 + (intensity - 0.50) / 0.15 * 0.20   # 30% → 50%
    elif intensity < 0.80:
        carb_fraction = 0.50 + (intensity - 0.65) / 0.15 * 0.25   # 50% → 75%
    else:
        carb_fraction = 0.75 + (intensity - 0.80) / 0.20 * 0.15   # 75% → 90%

    carb_fraction = min(carb_fraction, 0.95)

    # Gross mechanical efficiency ~22-25% for cycling
    # Total metabolic energy ≈ work / 0.24
    total_metabolic_kj = total_kj / 0.24
    carb_kj = total_metabolic_kj * carb_fraction
    carb_grams = carb_kj / 4.184 / 4.0  # 4 kcal/g, 4.184 kJ/kcal → simplified

    # Actually: 1g carbs = 4 kcal = 16.74 kJ
    carb_grams = carb_kj / 16.74

    return round(carb_grams, 0)
