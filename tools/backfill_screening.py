"""Re-parse existing job rows into the screening columns.

Run:
    .venv\\Scripts\\python.exe tools\\backfill_screening.py            (dry run)
    .venv\\Scripts\\python.exe tools\\backfill_screening.py --apply
    .venv\\Scripts\\python.exe tools\\backfill_screening.py --commute --apply

The scrapers wrote whatever the ad showed into `salary`, which includes values
like "$5" and "$1300 - $1400". The raw text is never modified: this only fills
the parsed columns beside it, so a bad reading can always be checked against the
words it came from.

Pay is re-read offline and costs nothing. The commute pass is opt-in because it
geocodes, and a public geocoder allows one request a second: distinct location
strings are few and cached forever after the first run, but the first run over a
large table is not quick. Start with --limit.

Dry run by default. Nothing is written without --apply.
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database_manager as dbm  # noqa: E402
import screening  # noqa: E402
from db.connection import get_db_connection  # noqa: E402


def fetch(profile_id, limit, only_unscreened, commute):
    clauses = ["1 = 1"]
    params = []
    if profile_id:
        clauses.append("profile_id = ?")
        params.append(profile_id)
    if only_unscreened:
        clauses.append("screened_at IS NULL")
    if not commute:
        # A pay-only pass has nothing to say about a row that quotes no money.
        clauses.append("(salary IS NOT NULL AND TRIM(salary) <> '')")
    sql = ("SELECT id, title, company, actual_company, advertiser_company, location, "
           "salary, description FROM jobs WHERE " + " AND ".join(clauses) +
           " ORDER BY id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_db_connection() as conn:
        return conn.execute(sql, params).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write results (default: dry run)")
    parser.add_argument("--commute", action="store_true", help="also screen commute (geocodes)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N rows")
    parser.add_argument("--profile", type=int, default=1, help="lane id (0 = all lanes)")
    parser.add_argument("--all", action="store_true", help="include rows already screened")
    parser.add_argument("--verbose", action="store_true", help="print every row")
    args = parser.parse_args()

    settings = dbm.get_lane_settings(args.profile or 1)
    screener = screening.build(settings, log=lambda message: print(f"  {message}"))
    if args.commute and not screener.enabled:
        print("Commute screening is off (no resolvable home location); pay only.")
        args.commute = False

    rows = fetch(args.profile, args.limit, not args.all, args.commute)
    print(f"{len(rows)} row(s) to consider. {'Writing.' if args.apply else 'Dry run.'}\n")

    verdicts = Counter()
    read, unread = 0, 0
    for row in rows:
        if args.commute:
            result = screener.screen(row)
            verdicts[result["verdict"]] += 1
            if result.get("salary_min") is not None:
                read += 1
            else:
                unread += 1
        else:
            result = screener.salary_reading(row)
            if not result:
                unread += 1
                if args.verbose:
                    print(f"  {row['id']:6d} {str(row['salary'])[:24]:26s} -> no reading")
                continue
            read += 1
        if args.verbose:
            band = (f"{result.get('salary_currency') or ''} "
                    f"{result.get('salary_min')}-{result.get('salary_max')}"
                    if result.get("salary_min") is not None else "no pay reading")
            print(f"  {row['id']:6d} {str(row['title'])[:30]:32s} "
                  f"{result.get('verdict', '-'):8s} {band}")
        if args.apply:
            dbm.save_job_screening(row["id"], result)

    print(f"\npay read on {read} row(s), no reading on {unread}")
    if verdicts:
        print("commute verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))
    if not args.apply:
        print("\nDry run — nothing was written. Re-run with --apply.")


if __name__ == "__main__":
    main()
