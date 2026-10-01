"""Scraper staleness, orphaned runs, the enrichment queue, warm-contact import,
post-application follow-ups, the apply-speed dimension and the text archive.

These are the October 2026 review fixes. Each one guards against a failure that
was silent in production: a dead source reported as healthy, a scrape run stuck
at `running` for a week, a queue nobody drained, and a database that grew to
half a gigabyte of advert text nobody would read again.
"""
import csv
import gzip
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as db  # noqa: E402
import db_setup  # noqa: E402


class _DbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_maintenance_test_")
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
        with db.get_db_connection() as conn:
            for table in ("application_events", "application_outcomes", "warm_contacts",
                          "lane_opportunities", "job_postings", "jobs", "local_llm_tasks",
                          "scraper_runs", "scraper_health", "app_settings", "interviews"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def _add(self, title, company, url, source="Seek", description=None, **columns):
        db.add_job(
            {"title": title, "company": company, "location": "Melbourne VIC", "url": url,
             "description": description or f"{title} at {company}."},
            source, 1,
        )
        with db.get_db_connection() as conn:
            job_id = conn.execute(
                "SELECT id FROM jobs WHERE url = ?", (db.normalize_job_url(url),)
            ).fetchone()["id"]
            if columns:
                assignments = ", ".join(f"{key} = ?" for key in columns)
                conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", [*columns.values(), job_id])
            conn.commit()
        return job_id


def _utc(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


class ScraperStalenessTests(_DbTestCase):
    def _health_row(self, scraper_id, empty, last_success_days):
        with db.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO scraper_health (scraper_id, status, consecutive_empty, last_success_at) "
                "VALUES (?, 'healthy', ?, ?)",
                (scraper_id, empty, _utc(last_success_days) if last_success_days is not None else None),
            )
            conn.commit()

    def test_a_long_quiet_streak_reads_as_stale_with_a_reason(self):
        self._health_row("hiring_cafe", 70, 30)
        health = db.get_scraper_health("hiring_cafe")
        self.assertEqual("stale", health["status"])
        self.assertEqual(30, health["days_since_success"])
        self.assertIn("30 days", health["stale_reason"])

    def test_a_few_empty_keywords_are_not_stale(self):
        self._health_row("seek", 3, 30)
        self._health_row("deakin", 40, 2)
        self.assertEqual("healthy", db.get_scraper_health("seek")["status"])
        self.assertEqual("healthy", db.get_scraper_health("deakin")["status"])

    def test_one_success_clears_staleness_and_empty_runs_keep_it(self):
        self._health_row("knox", 133, 59)
        db.record_scraper_health("knox", "empty")
        self.assertEqual("stale", db.get_scraper_health("knox")["status"])
        db.record_scraper_health("knox", "success")
        health = db.get_scraper_health("knox")
        self.assertEqual("healthy", health["status"])
        self.assertEqual(0, health["consecutive_empty"])

    def test_all_health_puts_the_worrying_sources_first(self):
        self._health_row("seek", 0, 0)
        self._health_row("hiring_cafe", 70, 30)
        statuses = [h["status"] for h in db.get_all_scraper_health()]
        self.assertEqual(["stale", "healthy"], statuses)


class OrphanedRunTests(_DbTestCase):
    def test_dead_process_and_overdue_rows_are_closed_out(self):
        live = db.record_scraper_run(1, "profile", ["Seek"], "running")
        dead = db.record_scraper_run(1, "profile", ["Seek"], "running")
        with db.get_db_connection() as conn:
            conn.execute("UPDATE scraper_runs SET pid = 999999 WHERE id = ?", (dead,))
            old = conn.execute(
                "INSERT INTO scraper_runs (scope, status, started_at) VALUES ('profile', 'running', ?)",
                (_utc(2),),
            ).lastrowid
            conn.commit()
        closed = db.reconcile_orphaned_scraper_runs(process_alive=lambda pid: pid != 999999)
        self.assertEqual(sorted([dead, old]), sorted(closed))
        with db.get_db_connection() as conn:
            rows = {r["id"]: r for r in conn.execute("SELECT id, status, summary, finished_at FROM scraper_runs")}
        self.assertEqual("running", rows[live]["status"])
        self.assertEqual("interrupted", rows[dead]["status"])
        self.assertIn("exited", rows[dead]["summary"])
        self.assertIn("8 hours", rows[old]["summary"])
        self.assertIsNotNone(rows[old]["finished_at"])

    def test_this_process_counts_as_alive(self):
        import os
        self.assertTrue(db._process_alive(os.getpid()))
        self.assertFalse(db._process_alive(None))


class EnrichmentQueueTests(_DbTestCase):
    def test_new_jobs_do_not_queue_enrichment_unless_enabled(self):
        self._add("IT Manager", "Acme", "https://x.test/1")
        with db.get_db_connection() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM local_llm_tasks").fetchone()[0])
            conn.execute(
                "INSERT INTO app_settings (key, value_json) VALUES (?, ?)",
                (db.ENRICHMENT_QUEUE_SETTING, json.dumps("1")),
            )
            conn.commit()
        self._add("IT Lead", "Acme", "https://x.test/2")
        with db.get_db_connection() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM local_llm_tasks").fetchone()[0])

    def test_pending_backlog_is_archived_then_cleared(self):
        with db.get_db_connection() as conn:
            conn.executemany(
                "INSERT INTO local_llm_tasks (task_type, entity_type, entity_id, status, input_hash) VALUES (?, ?, ?, ?, ?)",
                [("job_extract", "job_posting", 1, "pending", "a"),
                 ("job_extract", "job_posting", 2, "pending", "b"),
                 ("job_extract", "job_posting", 3, "complete", "c")],
            )
            conn.commit()
        with tempfile.TemporaryDirectory() as folder:
            result = db.archive_and_clear_pending_local_llm_tasks(folder)
            self.assertEqual(2, result["archived"])
            with gzip.open(result["path"], "rt", encoding="utf-8") as handle:
                self.assertEqual({1, 2}, {row["entity_id"] for row in json.load(handle)})
        with db.get_db_connection() as conn:
            self.assertEqual(["complete"], [r[0] for r in conn.execute("SELECT status FROM local_llm_tasks")])


class LinkedInImportTests(_DbTestCase):
    def _export(self, folder, rows):
        path = Path(folder) / "Connections.csv"
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write("Notes:\n\"When exporting your connection data, you may notice...\"\n\n")
            writer = csv.writer(handle)
            writer.writerow(["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"])
            writer.writerows(rows)
        return path

    def test_connections_become_warm_paths_on_open_jobs(self):
        job_id = self._add("IT Operations Manager", "Bapcor Limited", "https://x.test/b")
        self._add("IT Manager", "Nobody Pty Ltd", "https://x.test/n")
        with tempfile.TemporaryDirectory() as folder:
            path = self._export(folder, [
                ["Dana", "Lee", "https://linkedin.com/in/dana", "", "Bapcor Limited", "Head of IT", "12 Mar 2024"],
                ["No", "Company", "https://linkedin.com/in/x", "", "", "", "1 Jan 2020"],
            ])
            result = db.import_linkedin_connections(path)
            again = db.import_linkedin_connections(path)
        self.assertEqual(1, result["imported"])
        self.assertEqual(1, result["new_contacts"])
        self.assertEqual(1, result["skipped_no_company"])
        self.assertEqual([job_id], [m["job_id"] for m in result["open_jobs_with_contact"]])
        self.assertEqual(0, again["new_contacts"])
        index = db.warm_contact_index(1)
        job = dict(db.get_job_details(job_id))
        self.assertEqual(["Dana Lee"], [c["name"] for c in db.warm_path_for_job(job, index)])

    def test_a_file_that_is_not_the_export_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "other.csv"
            path.write_text("name,company\nDana,Acme\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                db.import_linkedin_connections(path)


class FollowUpTests(_DbTestCase):
    def test_moving_to_applied_schedules_a_weekday_follow_up(self):
        job_id = self._add("IT Manager", "Acme", "https://x.test/f")
        db.update_job_application(job_id, {"pipeline_stage": "applied", "application_date": "2026-10-02"})
        job = db.get_job_details(job_id)
        self.assertEqual(db.FOLLOW_UP_ACTION, job["next_action"])
        # 2 Oct + 7 days is Friday 9 Oct.
        self.assertEqual("2026-10-09", job["next_action_date"])

    def test_follow_up_never_lands_on_a_weekend(self):
        # 3 Oct 2026 is a Saturday; +7 is Saturday 10 Oct, so Monday 12 Oct.
        self.assertEqual("2026-10-12", db.follow_up_date("2026-10-03"))

    def test_an_explicit_next_action_is_respected(self):
        job_id = self._add("IT Manager", "Acme", "https://x.test/g")
        db.update_job_application(job_id, {
            "pipeline_stage": "applied", "next_action": "Call Dana", "next_action_date": "2026-10-05",
        })
        job = db.get_job_details(job_id)
        self.assertEqual("Call Dana", job["next_action"])

    def test_re_saving_an_applied_job_does_not_reset_its_follow_up(self):
        job_id = self._add("IT Manager", "Acme", "https://x.test/h")
        db.update_job_application(job_id, {"pipeline_stage": "applied", "application_date": "2026-10-02"})
        db.update_job_application(job_id, {"next_action": "Chased by email", "next_action_date": "2026-10-20"})
        db.update_job_application(job_id, {"pipeline_stage": "applied"})
        self.assertEqual("Chased by email", db.get_job_details(job_id)["next_action"])


class ApplySpeedTests(unittest.TestCase):
    def test_bands(self):
        self.assertEqual("0-2 days", db._apply_speed_band("2026-09-01 10:00:00", "2026-09-03T09:00:00"))
        self.assertEqual("3-7 days", db._apply_speed_band("2026-09-01", "2026-09-08"))
        self.assertEqual("8+ days", db._apply_speed_band("2026-09-01", "2026-09-20"))
        self.assertEqual("unknown", db._apply_speed_band(None, "2026-09-20"))
        self.assertEqual("unknown", db._apply_speed_band("2026-09-20", "2026-09-01"))


class PrefilterWiringTests(_DbTestCase):
    """_apply_prefilter persists a reason for every skip and keeps audits in."""

    class _Model:
        enabled = True

        def __init__(self, verdicts):
            self.verdicts = verdicts

        def decide(self, job_id, title, description):
            verdict = self.verdicts.get(title)
            return None if verdict is None else {"verdict": verdict, "score": 9.0, "reason": f"Prefilter: {title}"}

    def test_skips_are_recorded_and_audits_still_analysed(self):
        from llm import analysis

        skip = self._add("Payroll Officer", "Clinic", "https://x.test/p1")
        audit = self._add("Office Coordinator", "Cafe", "https://x.test/p2")
        keep = self._add("IT Manager", "Acme", "https://x.test/p3")
        with db.get_db_connection() as conn:
            jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        original = analysis._lane_prefilter
        analysis._lane_prefilter = lambda profile_id, log: self._Model(
            {"Payroll Officer": "skip", "Office Coordinator": "audit"})
        try:
            kept, audits = analysis._apply_prefilter(jobs, 1, lambda message: None)
        finally:
            analysis._lane_prefilter = original
        self.assertEqual({audit, keep}, {job["id"] for job in kept})
        self.assertEqual({audit}, audits)
        with db.get_db_connection() as conn:
            rows = {r["id"]: r for r in conn.execute("SELECT id, prefilter_verdict, prefilter_reason FROM jobs")}
        self.assertEqual("skip", rows[skip]["prefilter_verdict"])
        self.assertEqual("Prefilter: Payroll Officer", rows[skip]["prefilter_reason"])
        self.assertEqual("audit", rows[audit]["prefilter_verdict"])
        self.assertIsNone(rows[keep]["prefilter_verdict"])


class TextArchiveTests(_DbTestCase):
    def test_old_rejected_text_moves_to_the_archive_and_comes_back(self):
        long_text = "Responsibilities include " + ("stakeholder management " * 200)
        old = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        rejected = self._add("Payroll Officer", "Clinic", "https://x.test/r", description=long_text,
                             pipeline_stage="rejected", status="rejected", date_scraped=old,
                             ai_analysis="Triage Match Score: 20%\n" + "reason " * 200, pdf_text="pdf body")
        other_text = "Lead the service desk and vendors. " * 120
        applied = self._add("IT Manager", "Acme", "https://x.test/a", description=other_text,
                            pipeline_stage="rejected_by_company", application_date="2026-06-01", date_scraped=old)
        third_text = "Coordinate rosters, catering and supplies. " * 120
        recent = self._add("Office Coordinator", "Cafe", "https://x.test/c", description=third_text,
                           pipeline_stage="rejected", status="rejected")

        dry = db.compact_old_job_text(older_than_days=60, dry_run=True)
        self.assertEqual(1, dry["tables"]["jobs"]["rows"])
        with db.get_db_connection() as conn:
            self.assertEqual(long_text, conn.execute("SELECT description FROM jobs WHERE id = ?", (rejected,)).fetchone()[0])

        db.compact_old_job_text(older_than_days=60, dry_run=False)
        with db.get_db_connection() as conn:
            rows = {r["id"]: r for r in conn.execute(
                "SELECT id, description, ai_analysis, pdf_text, text_archived_at, description_fingerprint FROM jobs")}
        self.assertTrue(rows[rejected]["description"].startswith(long_text[:1500]))
        self.assertIn("Full text archived", rows[rejected]["description"])
        self.assertLess(len(rows[rejected]["description"]), 1600)
        self.assertIsNone(rows[rejected]["pdf_text"])
        self.assertIsNotNone(rows[rejected]["text_archived_at"])
        self.assertIsNotNone(rows[rejected]["description_fingerprint"])
        self.assertEqual(other_text, rows[applied]["description"])
        self.assertEqual(third_text, rows[recent]["description"])
        self.assertTrue(db.text_archive_path().exists())

        db.restore_archived_job_text(rejected)
        with db.get_db_connection() as conn:
            row = conn.execute("SELECT description, pdf_text, text_archived_at FROM jobs WHERE id = ?", (rejected,)).fetchone()
        self.assertEqual(long_text, row["description"])
        self.assertEqual("pdf body", row["pdf_text"])
        self.assertIsNone(row["text_archived_at"])


if __name__ == "__main__":
    unittest.main()
