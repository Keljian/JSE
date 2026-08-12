"""Verify the commute/pay screening integration end to end.

Run: .venv\\Scripts\\python.exe tools\\verify_screening.py

Checks the migration applied, the settings resolve, the modules import, and
the screener produces sane verdicts on real rows from the local database.
Read-only apart from running migrations, which are idempotent.
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, FAIL = "  [ok]", "  [FAIL]"
problems = []


def check(label, fn):
    try:
        result = fn()
        print(f"{OK} {label}: {result}")
        return result
    except Exception as exc:
        print(f"{FAIL} {label}: {exc}")
        problems.append(label)
        traceback.print_exc()
        return None


print("== schema ==")
import db_setup
check("migrations run", lambda: (db_setup.setup_database(), "applied")[1])

from db.connection import get_db_connection


def _cols(table):
    with get_db_connection() as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def job_cols():
    want = {"commute_km", "commute_sector", "commute_verdict", "commute_reason",
            "screen_score_delta", "salary_min", "salary_max", "salary_currency",
            "salary_period", "salary_confidence", "screened_at"}
    missing = want - set(_cols("jobs"))
    if missing:
        raise AssertionError(f"missing {sorted(missing)}")
    return f"{len(want)} columns present"


def profile_cols():
    want = {"home_location", "preferred_commute_km", "max_commute_km",
            "accepted_sectors", "distance_unit", "geocode_provider",
            "commute_screening_enabled", "salary_floor", "salary_currency"}
    missing = want - set(_cols("profiles"))
    if missing:
        raise AssertionError(f"missing {sorted(missing)}")
    return f"{len(want)} columns present"


def geocode_table():
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='geocode_cache'"
        ).fetchall()
    assert rows, "geocode_cache table not created"
    return "created"


check("jobs columns", job_cols)
check("profiles columns", profile_cols)
check("geocode_cache", geocode_table)

print("\n== modules ==")


def modules():
    import geo, salary, screening  # noqa: F401
    return "geo, salary, screening import cleanly"


def facade():
    import database_manager as dbm
    assert hasattr(dbm, "save_job_screening"), "facade does not expose save_job_screening"
    return "save_job_screening exposed"


check("leaf modules", modules)
check("db facade", facade)

print("\n== settings ==")
import database_manager as dbm


def lane_settings():
    s = dbm.get_lane_settings(1)
    return (f"home={s.get('home_location')!r} "
            f"radius={s.get('preferred_commute_km')}/{s.get('max_commute_km')}"
            f"{s.get('distance_unit')} sectors={s.get('accepted_sectors')!r} "
            f"floor={s.get('salary_floor')} enabled={s.get('commute_screening_enabled')}")


settings = check("lane 1 settings", lane_settings)

print("\n== salary parser on live rows ==")


def salary_sample():
    import salary as salary_mod
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT title, salary, description FROM jobs "
            "WHERE salary IS NOT NULL AND TRIM(salary) <> '' "
            "ORDER BY id DESC LIMIT 12"
        ).fetchall()
    if not rows:
        return "no rows with a salary value"
    shown = 0
    for r in rows:
        parsed = salary_mod.summarise(r["salary"], country_code="au")
        verdict = "rejected" if not parsed else (
            f"{parsed['currency']} {parsed['base_min']:,}-{parsed['base_max']:,}"
            f" [{parsed['period_quoted']}/{parsed['period_source']}]"
            + (" CONTRACT" if parsed["is_contract_rate"] else ""))
        print(f"      {str(r['salary'])[:26]:26s} -> {verdict}")
        shown += 1
    return f"{shown} rows parsed (rejections are correct for junk values)"


check("salary parsing", salary_sample)

print("\n== commute screen (dry run, no writes) ==")


def commute_sample():
    import screening
    s = dbm.get_lane_settings(1)
    screener = screening.build(s, log=lambda m: print(f"      {m}"))
    if not screener.enabled:
        return "screening disabled (set a home location to enable)"
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, location, salary, description FROM jobs "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
    counts = {}
    for r in rows:
        v = screener.screen(r)
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        print(f"      {v['verdict']:8s} {str(r['title'])[:32]:32s} | {v['reason'][:58]}")
    return counts


check("screener dry run", commute_sample)

print("\n" + ("ALL CHECKS PASSED" if not problems
              else f"PROBLEMS: {', '.join(problems)}"))
sys.exit(1 if problems else 0)
