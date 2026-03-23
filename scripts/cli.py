#!/usr/bin/env python3
"""TrainingEdge CLI — sync, analyze, validate, serve."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import database, sync, validator, intervals


def cmd_init(args):
    """Initialize database and auto-seed from Intervals.icu."""
    database.init_db()

    # Manual overrides (if provided)
    with database.get_db() as conn:
        if args.max_hr:
            database.set_setting(conn, "max_hr", str(args.max_hr))

    # Auto-seed from Intervals.icu
    if intervals.is_configured():
        print("Found Intervals.icu API key — auto-seeding CTL/ATL/FTP...")
        try:
            seed = intervals.auto_seed()
            print(f"  CTL: {seed.get('ctl', '—')}")
            print(f"  ATL: {seed.get('atl', '—')}")
            print(f"  TSB: {round(seed['ctl'] - seed['atl'], 1) if seed.get('ctl') and seed.get('atl') else '—'}")
            print(f"  FTP: {seed.get('ftp', '—')} W (from Intervals eFTP)")
            if seed.get('resting_hr'):
                print(f"  Resting HR: {seed.get('resting_hr')} bpm")
            if seed.get('weight_kg'):
                print(f"  Weight: {seed.get('weight_kg')} kg")
            print("  Done. All values seeded automatically.")
        except Exception as e:
            print(f"  Warning: auto-seed failed: {e}")
            print("  Falling back to defaults. You can re-run init after fixing the API key.")
    else:
        print("Intervals.icu API key not found.")
        print("  Run: garmin_coach.sh intervals-login")
        print("  Then re-run: python scripts/cli.py init")
        print("  (Using defaults: CTL=0, ATL=0, FTP=200)")

    from engine.auth import get_or_create_api_key
    api_key = get_or_create_api_key()
    print(f"\n  API Key: {api_key}")
    print(f"  Use: curl -H 'X-API-Key: {api_key}' http://localhost:8420/api/summary")

    print("\nDatabase initialized.")


def cmd_sync(args):
    """Sync recent activities from Garmin."""
    database.init_db()

    with database.get_db() as conn:
        ftp = float(database.get_setting(conn, "ftp") or "0") or None
        max_hr = int(float(database.get_setting(conn, "max_hr") or "190"))
        resting_hr = int(float(database.get_setting(conn, "resting_hr") or "50"))

    if args.ftp:
        ftp = args.ftp

    print(f"Syncing last {args.days} days (FTP={ftp}, MaxHR={max_hr}, RestHR={resting_hr})...")
    results = sync.sync_recent(
        days=args.days,
        activity_type=args.type,
        ftp=ftp,
        max_hr=max_hr,
        resting_hr=resting_hr,
        limit=args.limit,
    )
    print(f"\nSynced {len(results)} activities.")

    # Auto-validate against Intervals.icu (校验期)
    if intervals.is_configured() and results:
        print("\nAuto-validating against Intervals.icu...")
        try:
            val = intervals.auto_validate(days=args.days)
            print(f"  Validated: {val['validated']} | Passed: {val['passed']} | Rate: {val['pass_rate']}%")
            for d in val.get("details", []):
                status = "✅" if d["passed"] else "❌"
                print(f"    {status} {d['date']} {d['name']} — {d['summary']}")
        except Exception as e:
            print(f"  Validation skipped: {e}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


def cmd_activities(args):
    """List activities from local database."""
    database.init_db()
    with database.get_db() as conn:
        activities = database.list_activities(conn, sport=args.sport, days=args.days, limit=args.limit)

    if args.json:
        print(json.dumps(activities, ensure_ascii=False, indent=2, default=str))
    else:
        for act in activities:
            dist = f"{act['distance_m']/1000:.1f}km" if act.get('distance_m') else '—'
            dur = f"{act['total_timer_s']/60:.0f}min" if act.get('total_timer_s') else '—'
            np = f"NP={act['normalized_power']:.0f}W" if act.get('normalized_power') else ''
            tss = f"TSS={act['tss']:.0f}" if act.get('tss') else ''
            print(f"  {act['date']} | {act['name']:<30} | {dist:>8} | {dur:>6} | {np:>10} | {tss:>7}")


def cmd_fitness(args):
    """Show fitness history."""
    database.init_db()
    with database.get_db() as conn:
        history = database.list_fitness_history(conn, days=args.days)

    if args.json:
        print(json.dumps(history, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{'Date':<12} {'CTL':>6} {'ATL':>6} {'TSB':>6} {'Ramp':>6} {'TSS':>6}")
        print("-" * 50)
        for h in history:
            print(f"{h['date']:<12} {h['ctl'] or 0:>6.1f} {h['atl'] or 0:>6.1f} {h['tsb'] or 0:>6.1f} {h['ramp_rate'] or 0:>6.2f} {h['daily_tss'] or 0:>6.1f}")


def cmd_validate(args):
    """Show validation dashboard."""
    database.init_db()
    dashboard = validator.validation_dashboard(args.days)

    if args.json:
        print(json.dumps(dashboard, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Validation Dashboard ({args.days} days)")
        print(f"  Total activities: {dashboard['total_activities']}")
        print(f"  Validated:        {dashboard['total_validated']}")
        print(f"  Passed:           {dashboard['total_passed']}")
        print(f"  Pass rate:        {dashboard['pass_rate']}%")
        print(f"  Graduation ready: {'YES' if dashboard['graduation_ready'] else 'NO'}")
        print()

        for act in dashboard['activities']:
            val = act.get('validation')
            if val:
                status = '✅' if val.get('all_passed') else '❌'
                summary = val.get('summary', '')
                print(f"  {status} {act['date']} | {act['name']:<30} | {summary}")
            else:
                print(f"  ⬜ {act['date']} | {act['name']:<30} | not validated")


def cmd_serve(args):
    """Start the web server."""
    import uvicorn
    database.init_db()
    print(f"Starting TrainingEdge on http://0.0.0.0:{args.port}")
    uvicorn.run("api.app:app", host="0.0.0.0", port=args.port, reload=args.reload)


def main():
    parser = argparse.ArgumentParser(prog="training_edge", description="TrainingEdge CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # init — auto-seeds from Intervals.icu, only need manual max-hr override
    p_init = sub.add_parser("init", help="Initialize database (auto-seeds from Intervals.icu)")
    p_init.add_argument("--max-hr", type=int, help="Max heart rate (if not in Intervals)")
    p_init.set_defaults(func=cmd_init)

    # sync
    p_sync = sub.add_parser("sync", help="Sync recent activities from Garmin")
    p_sync.add_argument("--days", type=int, default=7)
    p_sync.add_argument("--type", default="all")
    p_sync.add_argument("--limit", type=int, default=20)
    p_sync.add_argument("--ftp", type=float)
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # activities
    p_act = sub.add_parser("activities", help="List activities from local DB")
    p_act.add_argument("--sport", help="Filter by sport type")
    p_act.add_argument("--days", type=int, default=30)
    p_act.add_argument("--limit", type=int, default=20)
    p_act.add_argument("--json", action="store_true")
    p_act.set_defaults(func=cmd_activities)

    # fitness
    p_fit = sub.add_parser("fitness", help="Show CTL/ATL/TSB history")
    p_fit.add_argument("--days", type=int, default=90)
    p_fit.add_argument("--json", action="store_true")
    p_fit.set_defaults(func=cmd_fitness)

    # validate
    p_val = sub.add_parser("validate", help="Validation dashboard")
    p_val.add_argument("--days", type=int, default=30)
    p_val.add_argument("--json", action="store_true")
    p_val.set_defaults(func=cmd_validate)

    # serve
    p_serve = sub.add_parser("serve", help="Start web dashboard")
    p_serve.add_argument("--port", type=int, default=8420)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
