"""Regression checks for roles discovered in more than one lane."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as db  # noqa: E402
import db_setup  # noqa: E402


class CrossLaneDedupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_cross_lane_test_")
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
            conn.execute("DELETE FROM local_llm_tasks")
            conn.execute("DELETE FROM search_hits")
            conn.execute("DELETE FROM lane_opportunities")
            conn.execute("DELETE FROM job_postings")
            conn.execute("DELETE FROM jobs")
            conn.commit()

    def test_same_role_across_lanes_reuses_job_and_creates_lane_opportunity(self):
        first = {
            "title": "Senior Product Manager",
            "company": "Example Co",
            "location": "Melbourne VIC",
            "url": "https://jobs.example.test/product-manager-lane-a",
            "description": "Own digital product discovery and delivery.",
            "search_keyword": "product manager",
        }
        second = {
            **first,
            "url": "https://jobs.example.test/product-manager-lane-b",
            "search_keyword": "technology product",
        }

        self.assertTrue(db.add_job(first, "Seek", profile_id=1))
        self.assertFalse(db.add_job(second, "Seek", profile_id=2))

        with db.get_db_connection() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            lanes = conn.execute(
                """
                SELECT lane_id
                FROM lane_opportunities
                ORDER BY lane_id
                """
            ).fetchall()
            self.assertEqual([1, 2], [row["lane_id"] for row in lanes])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0])

    def test_dedupe_database_merges_existing_cross_lane_duplicates(self):
        with db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                    (title, company, location, url, description, source, profile_id,
                     status, pipeline_stage, date_scraped, last_interaction_at, updated_at)
                VALUES
                    ('Senior Product Manager', 'Example Co', 'Melbourne VIC',
                     'https://jobs.example.test/existing-a',
                     'Own product discovery and delivery.', 'Seek', 1,
                     'new', 'new', datetime('now'), datetime('now'), datetime('now'))
                """
            )
            conn.execute(
                """
                INSERT INTO jobs
                    (title, company, location, url, description, source, profile_id,
                     status, pipeline_stage, date_scraped, last_interaction_at, updated_at)
                VALUES
                    ('Senior Product Manager', 'Example Co', 'Melbourne VIC',
                     'https://jobs.example.test/existing-b',
                     'Own product discovery and delivery.', 'Seek', 2,
                     'new', 'new', datetime('now'), datetime('now'), datetime('now'))
                """
            )
            conn.commit()

        self.assertEqual(1, db.dedupe_database())

        with db.get_db_connection() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            lanes = conn.execute(
                "SELECT lane_id FROM lane_opportunities ORDER BY lane_id"
            ).fetchall()
            self.assertEqual([1, 2], [row["lane_id"] for row in lanes])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
