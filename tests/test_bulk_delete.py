"""Clear scraped listings and delete-by-filter.

Both are destructive, so the tests pin the two things that must never slip:
the set deleted is exactly the set previewed, and a job with application
history survives unless the caller explicitly opts in.
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
import python_bridge as bridge  # noqa: E402


class BulkDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_bulk_delete_test_")
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
            for table in ("interviews", "application_events", "application_outcomes",
                          "lane_opportunities", "job_postings", "jobs"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def _add(self, title, url, location="Melbourne VIC", source="Seek", **columns):
        db.add_job(
            {"title": title, "company": "Acme", "location": location,
             "url": url, "description": f"{title} at Acme."},
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

    def _ids(self):
        with db.get_db_connection() as conn:
            return {row["id"] for row in conn.execute("SELECT id FROM jobs").fetchall()}

    def test_commands_are_registered(self):
        for name in ("jobs:bulkDeletePreview", "jobs:bulkDelete"):
            self.assertIn(name, bridge.COMMANDS)

    def test_clear_scraped_keeps_worked_and_hand_entered_jobs(self):
        fresh = self._add("Business Analyst", "https://example.com/b1")
        rejected = self._add("Data Analyst", "https://example.com/b2", pipeline_stage="rejected", status="rejected")
        interested = self._add("Project Lead", "https://example.com/b3", pipeline_stage="interested")
        applied = self._add("IT Manager", "https://example.com/b4", pipeline_stage="applied", status="applied",
                            application_date="2026-10-01")
        manual = self._add("Referral role", "manual://abc", source="Manual")

        preview = bridge.COMMANDS["jobs:bulkDeletePreview"]({"mode": "scraped", "profile_id": 1})
        self.assertEqual(set(preview["job_ids"]), {fresh, rejected})
        result = bridge.COMMANDS["jobs:bulkDelete"]({"job_ids": preview["job_ids"]})
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(self._ids(), {interested, applied, manual})

    def test_clear_scraped_can_be_limited_to_new(self):
        fresh = self._add("Business Analyst", "https://example.com/c1")
        rejected = self._add("Data Analyst", "https://example.com/c2", pipeline_stage="rejected", status="rejected")
        preview = bridge.COMMANDS["jobs:bulkDeletePreview"]({"mode": "scraped", "profile_id": 1, "stages": ["new"]})
        self.assertEqual(preview["job_ids"], [fresh])
        self.assertNotIn(rejected, preview["job_ids"])

    def test_filter_delete_matches_the_board_and_protects_history(self):
        melbourne = self._add("Analyst", "https://example.com/d1", location="Melbourne VIC")
        melbourne_applied = self._add("Lead", "https://example.com/d2", location="Melbourne, Victoria",
                                      pipeline_stage="applied", status="applied", application_date="2026-09-01")
        manchester = self._add("Systems Analyst", "https://example.com/d3", location="Manchester, UK")
        filters = {"location": "Melbourne VIC"}

        board = {job["id"] for job in bridge.COMMANDS["jobs:list"]({"profile_id": 1, **filters})["jobs"]}
        preview = bridge.COMMANDS["jobs:bulkDeletePreview"]({"mode": "filter", "profile_id": 1, "filters": filters})
        self.assertEqual(set(preview["job_ids"]) | set(preview["protected_job_ids"]), board)
        self.assertEqual(preview["protected_job_ids"], [melbourne_applied])

        bridge.COMMANDS["jobs:bulkDelete"]({"job_ids": preview["job_ids"]})
        self.assertEqual(self._ids(), {melbourne_applied, manchester})
        self.assertNotIn(melbourne, self._ids())

    def test_history_is_rechecked_at_delete_time(self):
        job_id = self._add("Analyst", "https://example.com/e1")
        preview = bridge.COMMANDS["jobs:bulkDeletePreview"]({"mode": "scraped", "profile_id": 1})
        db.update_job_application(job_id, {"pipeline_stage": "applied", "application_date": "2026-10-10"})
        result = bridge.COMMANDS["jobs:bulkDelete"]({"job_ids": preview["job_ids"]})
        self.assertEqual(result, {"deleted": 0, "kept": 1})
        self.assertIn(job_id, self._ids())

    def test_opt_in_deletes_history_and_cascades(self):
        job_id = self._add("Lead", "https://example.com/f1", pipeline_stage="interviewing", status="interviewing")
        db.add_interview(job_id, {"title": "Panel"})
        preview = bridge.COMMANDS["jobs:bulkDeletePreview"]({"mode": "filter", "profile_id": 1, "filters": {"query": "Lead"}})
        self.assertEqual(preview["protected_job_ids"], [job_id])
        result = bridge.COMMANDS["jobs:bulkDelete"]({
            "job_ids": preview["job_ids"], "protected_job_ids": preview["protected_job_ids"], "allow_history": True,
        })
        self.assertEqual(result["deleted"], 1)
        with db.get_db_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM interviews WHERE job_id = ?", (job_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lane_opportunities WHERE legacy_job_id = ?", (job_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
