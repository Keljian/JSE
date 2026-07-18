"""Regression checks for the Funnel Insights outcome feedback loop.

Covers: outcome-snapshot creation on the applied transition, the backfill
migration, role_key linking of re-advertised duplicates, conversion-prior
clamping around the auto-reject threshold, and lane deletion sparing jobs that
carry interview history.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as db  # noqa: E402
import db_setup  # noqa: E402


def _make_job(title, company, url, description, source="Seek", profile_id=1, score=None):
    added = db.add_job(
        {
            "title": title,
            "company": company,
            "location": "Melbourne VIC",
            "url": url,
            "description": description,
        },
        source,
        profile_id,
    )
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE url = ?", (db.normalize_job_url(url),)).fetchone()
        job_id = row["id"] if row else None
        if job_id and score is not None:
            conn.execute("UPDATE jobs SET match_score = ? WHERE id = ?", (score, job_id))
            conn.commit()
    return added, job_id


class FunnelOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_funnel_test_")
        cls.original_db_file = db.DB_FILE
        cls.original_setup_db_file = db_setup.DB_FILE
        cls.db_file = str(Path(cls.test_data) / "job_applications.db")
        db.DB_FILE = cls.db_file
        db_setup.DB_FILE = cls.db_file
        db._wal_enabled = False
        db_setup.setup_database()

    @classmethod
    def tearDownClass(cls):
        db.DB_FILE = cls.original_db_file
        db_setup.DB_FILE = cls.original_setup_db_file
        db._wal_enabled = False
        shutil.rmtree(cls.test_data, ignore_errors=True)

    def setUp(self):
        # A second lane so lane-deletion has a fallback to reassign into.
        with db.get_db_connection() as conn:
            if not conn.execute("SELECT id FROM profiles WHERE id = 2").fetchone():
                conn.execute("INSERT INTO profiles (id, name, resume_path) VALUES (2, 'Second', '')")
            for table in (
                "application_outcomes", "interviews", "application_events",
                "lane_opportunities", "job_postings", "jobs",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM app_settings WHERE key IN (?, ?, ?)",
                         (db.FUNNEL_CONVERSION_PRIORS_KEY, db.FUNNEL_INSIGHTS_CACHE_KEY, db._OUTCOME_BACKFILL_FLAG))
            conn.commit()

    # -- item 1: snapshot on the applied transition --------------------------
    def test_snapshot_created_on_applied_transition(self):
        _, job_id = _make_job("IT Manager", "Acme Co", "https://x.test/it-manager",
                              "Lead IT operations and vendor governance. " * 8, score=80)
        db.update_job_application(job_id, {"pipeline_stage": "applied", "application_date": "2026-07-01"})
        with db.get_db_connection() as conn:
            row = conn.execute(
                "SELECT outcome, role_key, snapshot_json FROM application_outcomes WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("pending", row["outcome"])
        self.assertTrue(row["role_key"])

    def test_interview_advances_outcome_and_ghost_does_not_regress(self):
        _, job_id = _make_job("Systems Engineer", "Beta Co", "https://x.test/sys-eng",
                              "Operate cloud platforms and infrastructure. " * 8, score=75)
        db.update_job_application(job_id, {"pipeline_stage": "applied", "application_date": "2026-07-01"})
        db.add_interview(job_id, {"interview_date": "2026-07-05"})
        with db.get_db_connection() as conn:
            row = conn.execute("SELECT outcome, interview_rounds FROM application_outcomes WHERE job_id = ?", (job_id,)).fetchone()
        self.assertEqual("interview", row["outcome"])
        self.assertEqual(1, row["interview_rounds"])
        # A later ghost sweep must not regress an interview outcome.
        with db.get_db_connection() as conn:
            db.set_application_outcome(conn, job_id, db.OUTCOME_GHOSTED)
            conn.commit()
            row = conn.execute("SELECT outcome FROM application_outcomes WHERE job_id = ?", (job_id,)).fetchone()
        self.assertEqual("interview", row["outcome"])

    # -- item 3: role_key linking of re-advertised duplicates ---------------
    def test_readvertised_roles_share_role_key(self):
        shared = ("Manage the hybrid cloud platforms function across Azure and AWS with vendor "
                  "governance, service delivery, stakeholder engagement and continuous improvement. ")
        _, first = _make_job("Technical Lead", "Clicks IT Recruitment", "https://x.test/6353",
                            shared + "Round one of two interviews.")
        _, second = _make_job("Head of Digital and Technology", "Clicks IT Recruitment", "https://x.test/22508",
                            shared + "Newly created strategic leadership position for our client.")
        _, other = _make_job("Data Analyst", "Different Bank", "https://x.test/other",
                            "Build Power BI dashboards and SQL data models for finance reporting. " * 5)
        for jid in (first, second, other):
            db.update_job_application(jid, {"pipeline_stage": "applied", "application_date": "2026-07-01"})
        with db.get_db_connection() as conn:
            keys = {r["job_id"]: r["role_key"]
                    for r in conn.execute("SELECT job_id, role_key FROM application_outcomes").fetchall()}
        self.assertEqual(keys[first], keys[second])
        self.assertNotEqual(keys[first], keys[other])

    # -- item 6: conversion-prior clamping ----------------------------------
    def test_prior_never_crosses_auto_reject_threshold_on_its_own(self):
        priors = {
            "baseline_rate": 0.05,
            "dimensions": {
                "employer_type": {
                    "direct_employer": {"support": 10, "rate": 0.6, "delta": 10},
                    "recruiter": {"support": 10, "rate": 0.0, "delta": -10},
                }
            },
        }
        below = {"title": "X", "company": "C", "advertiser_company": "C",
                 "employer_type": "direct_employer", "source": "Seek"}
        # Strong positive prior on a below-threshold base must stay below.
        adjusted = db.composite_score_with_prior(40, None, below, priors)
        self.assertLess(adjusted, db.AUTO_REJECT_THRESHOLD)
        above = {"title": "X", "company": "C", "advertiser_company": "C",
                 "employer_type": "recruiter", "source": "Seek"}
        # Strong negative prior on an at-threshold base must stay at/above.
        adjusted = db.composite_score_with_prior(db.AUTO_REJECT_THRESHOLD + 1, None, above, priors)
        self.assertGreaterEqual(adjusted, db.AUTO_REJECT_THRESHOLD)

    def test_prior_below_min_outcomes_has_no_effect(self):
        priors = {
            "baseline_rate": 0.05,
            "dimensions": {"source": {"Seek": {"support": 2, "rate": 1.0, "delta": 10}}},
        }
        job = {"title": "X", "company": "C", "advertiser_company": "C", "employer_type": "x", "source": "Seek"}
        # support 2 < MIN_PRIOR_OUTCOMES -> delta ignored, composite == base.
        self.assertEqual(
            db.calculate_composite_score(70, None),
            db.composite_score_with_prior(70, None, job, priors),
        )

    # -- item 1: lane deletion spares jobs with history ---------------------
    def test_lane_deletion_spares_jobs_with_interviews(self):
        _, keep = _make_job("Interviewed Role", "KeepCo", "https://x.test/keep",
                           "Role that reached interview stage with history. " * 6, profile_id=1)
        _, drop = _make_job("Fresh Role", "DropCo", "https://x.test/drop",
                          "A brand new untouched role in this lane awaiting review. " * 6, profile_id=1)
        db.update_job_application(keep, {"pipeline_stage": "applied", "application_date": "2026-07-01"})
        db.add_interview(keep, {"interview_date": "2026-07-05"})

        db.delete_profile(1)

        with db.get_db_connection() as conn:
            keep_row = conn.execute("SELECT profile_id FROM jobs WHERE id = ?", (keep,)).fetchone()
            drop_row = conn.execute("SELECT id FROM jobs WHERE id = ?", (drop,)).fetchone()
            outcome_row = conn.execute("SELECT job_id FROM application_outcomes WHERE job_id = ?", (keep,)).fetchone()
            interview_row = conn.execute("SELECT id FROM interviews WHERE job_id = ?", (keep,)).fetchone()
        self.assertIsNotNone(keep_row, "job with interview history must survive lane deletion")
        self.assertEqual(2, keep_row["profile_id"], "surviving job reassigned to fallback lane")
        self.assertIsNone(drop_row, "historyless job in the deleted lane is removed")
        self.assertIsNotNone(outcome_row, "outcome snapshot must survive lane deletion")
        self.assertIsNotNone(interview_row, "interview rows must survive lane deletion")
        # Restore lane 1 for other tests' fallbacks.
        with db.get_db_connection() as conn:
            conn.execute("INSERT INTO profiles (id, name, resume_path) VALUES (1, 'General', '')")
            conn.commit()

    # -- item 1: backfill migration -----------------------------------------
    def test_backfill_reconstructs_outcomes_from_history(self):
        # A legacy applied job with no outcome row, plus an orphan interview whose
        # jobs row was hard-deleted by the old cascade.
        _, legacy = _make_job("Legacy Applied", "OldCo", "https://x.test/legacy",
                             "A legacy applied role with no snapshot yet recorded here. " * 6)
        with db.get_db_connection() as conn:
            conn.execute("UPDATE jobs SET pipeline_stage = 'applied', status = 'applied', application_date = '2026-06-01' WHERE id = ?", (legacy,))
            # Orphan interview: job 99999 does not exist in jobs.
            conn.execute("INSERT INTO interviews (job_id, round_number, interview_date) VALUES (99999, 1, '2026-05-01')")
            conn.execute("DELETE FROM application_outcomes")
            conn.execute("DELETE FROM app_settings WHERE key = ?", (db._OUTCOME_BACKFILL_FLAG,))
            conn.commit()

        created = db.backfill_application_outcomes()
        self.assertGreaterEqual(created, 2)
        with db.get_db_connection() as conn:
            legacy_row = conn.execute("SELECT outcome FROM application_outcomes WHERE job_id = ?", (legacy,)).fetchone()
            orphan_row = conn.execute("SELECT outcome FROM application_outcomes WHERE job_id = 99999").fetchone()
        self.assertIsNotNone(legacy_row)
        self.assertEqual("pending", legacy_row["outcome"])
        self.assertIsNotNone(orphan_row, "orphan interview history must be preserved")
        self.assertEqual("interview", orphan_row["outcome"])
        # Second run is idempotent (gated by the flag).
        self.assertEqual(0, db.backfill_application_outcomes())


if __name__ == "__main__":
    unittest.main()
