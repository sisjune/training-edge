"""Validation module — compare our computed metrics against Intervals.icu during 校验期.

Key insight: FTP differs between us (229W) and Intervals.icu (198W).
TSS/IF/CTL/ATL all depend on FTP, so we must normalize to the SAME FTP
before comparing. The real test is whether our NP, zone logic, and
rolling-average algorithms produce the same results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import database


@dataclass
class FieldComparison:
    """Comparison result for a single metric field."""
    field: str
    ours: Optional[float]
    theirs: Optional[float]
    diff: Optional[float]
    tolerance: float
    passed: bool
    note: str = ""


@dataclass
class ValidationResult:
    """Full validation result for one activity."""
    activity_id: int
    activity_date: str
    activity_name: str
    comparisons: List[FieldComparison] = field(default_factory=list)
    all_passed: bool = False
    summary: str = ""


def _recalc_with_ftp(np: Optional[float], duration_s: Optional[float], ftp: float):
    """Recalculate IF and TSS using a given FTP, from raw NP and duration."""
    if np is None or ftp <= 0:
        return None, None
    if_val = np / ftp
    tss = None
    if duration_s and duration_s > 0:
        tss = (duration_s * np * if_val) / (ftp * 3600) * 100
    return round(if_val, 4), round(tss, 2) if tss is not None else None


def validate_activity(
    activity_id: int,
    intervals_data: Dict[str, Any],
) -> ValidationResult:
    """Compare our computed metrics against Intervals.icu data.

    For FTP-independent metrics (NP, avg_power, avg_hr):
        Compare directly.
    For FTP-dependent metrics (TSS, IF):
        Recalculate using Intervals' FTP so we compare algorithm, not FTP choice.
    For fitness metrics (CTL, ATL):
        Compare but with loose tolerance since they accumulate FTP-based TSS differences.
    """
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
        if not activity:
            return ValidationResult(
                activity_id=activity_id,
                activity_date="",
                activity_name="",
                summary=f"Activity {activity_id} not found in our database.",
            )

        # Extract Intervals values
        ivals_np = intervals_data.get("np")
        ivals_tss = intervals_data.get("tss")
        ivals_if_pct = intervals_data.get("intensity_pct")  # e.g., 74.75
        ivals_ctl = intervals_data.get("ctl")
        ivals_atl = intervals_data.get("atl")
        ivals_ftp = intervals_data.get("ftp")
        ivals_avg_hr = intervals_data.get("avg_hr")

        # Convert IF from percentage to decimal if needed
        ivals_if = None
        if ivals_if_pct is not None:
            ivals_if = ivals_if_pct / 100.0 if ivals_if_pct > 2.0 else ivals_if_pct

        # Our raw values
        our_np = activity.get("normalized_power")
        our_duration_s = activity.get("total_timer_s") or activity.get("total_elapsed_s")
        our_avg_hr = activity.get("avg_hr")

        # For FTP-dependent metrics: recalculate using Intervals' FTP
        # This isolates algorithm differences from FTP differences
        our_if_rebased = None
        our_tss_rebased = None
        ftp_note = ""
        if ivals_ftp and ivals_ftp > 0 and our_np:
            our_if_rebased, our_tss_rebased = _recalc_with_ftp(our_np, our_duration_s, ivals_ftp)
            ftp_note = f" (rebased to FTP={ivals_ftp}W)"

        # Store intervals data for reference
        intervals_mapped = {
            "intervals_np": ivals_np,
            "intervals_tss": ivals_tss,
            "intervals_ctl": ivals_ctl,
            "intervals_atl": ivals_atl,
            "intervals_if": ivals_if,
        }
        database.upsert_activity(conn, {
            "id": activity_id,
            **{k: v for k, v in intervals_mapped.items() if v is not None},
        })

        # Build comparisons
        comparisons = []

        # 1. NP — FTP-independent, direct comparison
        _add_comparison(comparisons, "NP", our_np, ivals_np, 3.0, "W")

        # 2. IF — recalculated with same FTP
        _add_comparison(comparisons, "IF", our_if_rebased, ivals_if, 0.03, ftp_note)

        # 3. TSS — recalculated with same FTP
        _add_comparison(comparisons, "TSS", our_tss_rebased, ivals_tss, 5.0, ftp_note)

        # 4. Avg HR — FTP-independent
        _add_comparison(comparisons, "Avg HR", our_avg_hr, ivals_avg_hr, 2.0, "bpm")

        # 5. CTL/ATL — informational only, NOT counted toward pass/fail.
        # Reason: CTL/ATL accumulate from full TSS history (months/years).
        # We only have ~30 days of data, so a gap is expected and not a bug.
        fitness = conn.execute(
            "SELECT ctl, atl FROM fitness_history WHERE date = ?",
            (activity.get("date", ""),)
        ).fetchone()
        our_ctl = fitness["ctl"] if fitness else None
        our_atl = fitness["atl"] if fitness else None
        _add_comparison(comparisons, "CTL (参考)", our_ctl, ivals_ctl, 999.0, " (仅供参考)")
        _add_comparison(comparisons, "ATL (参考)", our_atl, ivals_atl, 999.0, " (仅供参考)")

        # Determine pass/fail — only count non-informational fields
        scorable = [c for c in comparisons
                    if c.ours is not None and c.theirs is not None
                    and "参考" not in c.field]
        all_passed = all(c.passed for c in scorable) if scorable else False

        passed_count = sum(1 for c in scorable if c.passed)
        total_count = len(scorable)
        summary = f"{passed_count}/{total_count} checks passed"

        # Store validation JSON
        validation_json = json.dumps({
            "comparisons": [
                {
                    "field": c.field, "ours": c.ours, "theirs": c.theirs,
                    "diff": c.diff, "tolerance": c.tolerance,
                    "passed": c.passed, "note": c.note,
                }
                for c in comparisons
            ],
            "all_passed": all_passed,
            "summary": summary,
            "intervals_ftp": ivals_ftp,
        })
        database.upsert_activity(conn, {
            "id": activity_id,
            "validation_json": validation_json,
        })

        return ValidationResult(
            activity_id=activity_id,
            activity_date=activity.get("date", ""),
            activity_name=activity.get("name", ""),
            comparisons=comparisons,
            all_passed=all_passed,
            summary=summary,
        )


def _add_comparison(
    comparisons: List[FieldComparison],
    name: str,
    ours: Optional[float],
    theirs: Optional[float],
    tolerance: float,
    unit_or_note: str = "",
) -> None:
    """Helper to add a field comparison."""
    diff = None
    passed = False
    note = ""

    if ours is not None and theirs is not None:
        diff = round(abs(ours - theirs), 3)
        passed = diff <= tolerance
        if passed:
            note = f"✅ diff={diff}{unit_or_note} (tol={tolerance})"
        else:
            note = f"❌ diff={diff}{unit_or_note} > tol={tolerance}"
    elif ours is None and theirs is None:
        note = "⏭ both N/A"
    elif ours is None:
        note = "⚠️ our value missing"
    elif theirs is None:
        note = "⚠️ intervals value missing"

    comparisons.append(FieldComparison(
        field=name,
        ours=round(ours, 3) if ours is not None else None,
        theirs=round(theirs, 3) if theirs is not None else None,
        diff=diff,
        tolerance=tolerance,
        passed=passed,
        note=note,
    ))


def validation_dashboard(days: int = 30) -> Dict[str, Any]:
    """Get validation status for recent activities."""
    with database.get_db() as conn:
        activities = database.list_activities(conn, days=days)

        results = []
        total_validated = 0
        total_passed = 0

        for act in activities:
            val_json = act.get("validation_json")
            if val_json:
                val = json.loads(val_json)
                total_validated += 1
                if val.get("all_passed"):
                    total_passed += 1
                results.append({
                    "id": act["id"],
                    "date": act["date"],
                    "name": act["name"],
                    "sport": act["sport"],
                    "validation": val,
                })
            else:
                results.append({
                    "id": act["id"],
                    "date": act["date"],
                    "name": act["name"],
                    "sport": act["sport"],
                    "validation": None,
                })

        return {
            "total_activities": len(activities),
            "total_validated": total_validated,
            "total_passed": total_passed,
            "pass_rate": round(total_passed / total_validated * 100, 1) if total_validated > 0 else 0,
            "graduation_ready": total_passed >= 10 and (total_passed / total_validated >= 0.9 if total_validated > 0 else False),
            "activities": results,
        }
