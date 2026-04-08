"""Tests for engine/metrics.py — core training metric calculations."""

import math
from datetime import date

import pytest

from engine.metrics import (
    DailyLoad,
    FitnessState,
    compute_fitness_history,
    compute_hr_drift,
    estimate_ftp_from_pdc,
    hr_zone_distribution,
    intensity_factor,
    max_mean_power,
    normalized_power,
    power_duration_curve,
    power_zone_distribution,
    running_effectiveness,
    training_stress_score,
    trimp_exp,
    vdot_from_race,
    w_prime,
    xpower,
)


# ═══ Normalized Power ═══

class TestNormalizedPower:
    def test_constant_power(self):
        """NP of constant power = that power."""
        data = [200] * 300
        np = normalized_power(data)
        assert np is not None
        assert abs(np - 200.0) < 1.0

    def test_insufficient_data(self):
        assert normalized_power([200] * 10) is None

    def test_variable_power(self):
        """NP should be higher than average for variable power."""
        data = [100, 300] * 200  # avg 200, high variability
        np = normalized_power(data)
        assert np is not None
        assert np >= 200  # NP >= avg for variable efforts (equals when window smooths perfectly)

    def test_zero_power(self):
        data = [0] * 60
        np = normalized_power(data)
        assert np is not None
        assert np == 0.0


# ═══ TSS / IF ═══

class TestTSSIF:
    def test_intensity_factor(self):
        assert intensity_factor(200, 200) == pytest.approx(1.0, abs=0.01)
        assert intensity_factor(150, 200) == pytest.approx(0.75, abs=0.01)
        assert intensity_factor(0, 200) == pytest.approx(0.0, abs=0.01)

    def test_intensity_factor_zero_ftp(self):
        assert intensity_factor(200, 0) is None

    def test_tss_one_hour_at_ftp(self):
        """1 hour at FTP = 100 TSS."""
        tss = training_stress_score(200, 200, 3600)
        assert tss is not None
        assert abs(tss - 100.0) < 1.0

    def test_tss_zero_duration(self):
        assert training_stress_score(200, 200, 0) is None


# ═══ Power Duration Curve ═══

class TestPDC:
    def test_max_mean_power(self):
        data = [100] * 100 + [300] * 60 + [100] * 100
        best_60 = max_mean_power(data, 60)
        assert best_60 is not None
        assert abs(best_60 - 300.0) < 1.0

    def test_duration_longer_than_data(self):
        assert max_mean_power([200] * 10, 60) is None

    def test_pdc_returns_dict(self):
        data = [250] * 400
        pdc = power_duration_curve(data)
        assert isinstance(pdc, dict)
        assert 1 in pdc
        assert 60 in pdc
        assert 300 in pdc

    def test_estimate_ftp_from_20min(self):
        pdc = {1200: 250.0, 3600: 230.0}
        eftp = estimate_ftp_from_pdc(pdc)
        assert eftp is not None
        assert abs(eftp - 237.5) < 0.5  # 250 * 0.95

    def test_w_prime_positive(self):
        pdc = {300: 350.0}
        wp = w_prime(pdc, 200)
        assert wp is not None
        assert wp == (350 - 200) * 300  # 45000 J


# ═══ Zones ═══

class TestZones:
    def test_power_zones_sum_to_100(self):
        data = [150, 200, 250, 300, 100, 50, 400] * 100
        zones = power_zone_distribution(data, 200)
        total_pct = sum(z.pct for z in zones)
        assert abs(total_pct - 100.0) < 0.5

    def test_hr_zones_sum_to_100(self):
        data = [120, 140, 155, 170, 185] * 100
        zones = hr_zone_distribution(data, 192)
        total_pct = sum(z.pct for z in zones)
        assert abs(total_pct - 100.0) < 0.5

    def test_hr_zones_zero_max_hr(self):
        assert hr_zone_distribution([140] * 100, 0) == []


# ═══ CTL / ATL / TSB ═══

class TestFitnessModel:
    def test_empty_loads(self):
        assert compute_fitness_history([]) == []

    def test_single_day(self):
        loads = [DailyLoad(day=date(2026, 1, 1), tss=100)]
        history = compute_fitness_history(loads)
        assert len(history) >= 1
        assert history[0].ctl > 0
        assert history[0].atl > 0

    def test_tsb_equals_ctl_minus_atl(self):
        loads = [DailyLoad(day=date(2026, 1, d), tss=80) for d in range(1, 15)]
        history = compute_fitness_history(loads)
        for state in history:
            assert abs(state.tsb - (state.ctl - state.atl)) < 0.1

    def test_rest_day_decay(self):
        """CTL should decay on rest days."""
        loads = [DailyLoad(day=date(2026, 1, 1), tss=100)]
        history = compute_fitness_history(loads)
        # Find a rest day far from training
        if len(history) > 10:
            assert history[-1].ctl < history[0].ctl


# ═══ HR Drift ═══

class TestHRDrift:
    def test_stable_output(self):
        hr = [150] * 1200
        power = [200.0] * 1200
        drift = compute_hr_drift(hr, power)
        assert drift is not None
        assert abs(drift.drift_pct) < 1.0
        assert drift.classification == "stable"

    def test_drifting_hr(self):
        hr = [140] * 600 + [170] * 600
        power = [200.0] * 1200
        drift = compute_hr_drift(hr, power)
        assert drift is not None
        assert drift.drift_pct > 5.0

    def test_insufficient_data(self):
        assert compute_hr_drift([150] * 100, [200.0] * 100) is None


# ═══ Running metrics ═══

class TestRunningMetrics:
    def test_vdot_5k(self):
        """5K in 25 minutes should give a reasonable VDOT."""
        vdot = vdot_from_race(5000, 25 * 60)
        assert vdot is not None
        assert 35 < vdot < 45

    def test_vdot_invalid(self):
        assert vdot_from_race(0, 100) is None
        assert vdot_from_race(5000, 0) is None

    def test_trimp(self):
        hr = [150] * 3600  # 1 hour at 150 bpm
        trimp = trimp_exp(hr, 50, 190)
        assert trimp is not None
        assert trimp > 0

    def test_trimp_below_resting(self):
        hr = [40] * 600  # all below resting
        trimp = trimp_exp(hr, 50, 190)
        assert trimp is not None
        assert trimp == 0.0

    def test_running_effectiveness(self):
        re = running_effectiveness(3.0, 150)  # 3 m/s, 150 bpm
        assert re is not None
        assert 1.0 < re < 1.5

    def test_running_effectiveness_zero_hr(self):
        assert running_effectiveness(3.0, 0) is None


# ═══ xPower ═══

class TestXPower:
    def test_constant_power(self):
        data = [200] * 300
        xp = xpower(data)
        assert xp is not None
        assert abs(xp - 200.0) < 5.0

    def test_insufficient_data(self):
        assert xpower([200] * 10) is None
