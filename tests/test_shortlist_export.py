"""Regression checks for the triage packet export (jobs:exportShortlist).

The packet replaces a manual copy-paste handoff, so it is only worth anything
if it is complete: every survivor, with the ad text, the scores, the gate
verdict, and any warm path, in one file. These tests pin the contents, the
ordering, and the exclusion of roles the gate already ruled out.
"""
import json
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


class ShortlistExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_shortlist_test_")
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
                          "lane_opportunities", "job_postings", "jobs"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        self.out_dir = tempfile.mkdtemp(prefix="jse_shortlist_out_")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def _add(self, title, company, url, description="Job description body.", **columns):
        db.add_job(
            {"title": title, "company": company, "location": "Melbourne VIC",
             "url": url, "description": description},
            "Seek", 1,
        )
        with db.get_db_connection() as conn:
            job_id = conn.execute(
                "SELECT id FROM jobs WHERE url = ?", (db.normalize_job_url(url),)
            ).fetchone()["id"]
            if columns:
                assignments = ", ".join(f"{key} = ?" for key in columns)
                conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                             [*columns.values(), job_id])
            conn.commit()
        return job_id

    def _export(self, **overrides):
        payload = {"profile_id": 1, "output_dir": self.out_dir}
        payload.update(overrides)
        return bridge.COMMANDS["jobs:exportShortlist"](payload)

    def _read(self, result, suffix):
        path = next(Path(p) for p in result["files"] if p.endswith(suffix))
        return path.read_text(encoding="utf-8")

    def test_writes_both_formats_by_default(self):
        self._add("IT Manager", "Coldco", "https://example.com/s1")
        result = self._export()
        self.assertEqual(len(result["files"]), 2)
        self.assertTrue(any(p.endswith(".md") for p in result["files"]))
        self.assertTrue(any(p.endswith(".json") for p in result["files"]))
        for path in result["files"]:
            self.assertTrue(Path(path).exists())

    def test_format_can_be_narrowed(self):
        self._add("IT Manager", "Coldco", "https://example.com/s2")
        self.assertEqual(len(self._export(format="json")["files"]), 1)
        self.assertEqual(len(self._export(format="markdown")["files"]), 1)

    def test_packet_carries_the_decision_inputs(self):
        job_id = self._add(
            "Head of Technology", "Flavorite",
            "https://example.com/s3",
            description="Lead the technology function. Applicants must hold a current licence.",
            match_score=82, composite_score=84, salary="$160,000", location="Mentone VIC",
        )
        db.update_job_analysis(job_id, "Match Score: 82%\nFit Level: Strong", 82)
        db.update_job_blocker_gate(job_id, "stretch", "Named platform gaps.", {
            "verdict": "stretch",
            "named_gaps": ["Ad asks for Dynamics 365; resume shows Salesforce."],
            "hard_blockers": [],
        })
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite", role_title="COO", profile_id=1)

        result = self._export()
        payload = json.loads(self._read(result, ".json"))
        entry = payload["jobs"][0]

        self.assertEqual(entry["title"], "Head of Technology")
        self.assertEqual(entry["blocker_verdict"], "stretch")
        self.assertEqual(entry["named_gaps"], ["Ad asks for Dynamics 365; resume shows Salesforce."])
        self.assertEqual(entry["warm_path"][0]["name"], "Dana Lee")
        self.assertIn("must hold a current licence", entry["description"])
        self.assertIn("Match Score", entry["analysis"])
        self.assertEqual(entry["salary"], "$160,000")

        markdown = self._read(result, ".md")
        self.assertIn("Head of Technology", markdown)
        self.assertIn("Dana Lee (COO)", markdown)
        self.assertIn("Ad asks for Dynamics 365", markdown)
        self.assertIn("must hold a current licence", markdown)

    def test_gated_skips_are_excluded_unless_asked_for(self):
        keep = self._add("Keeper", "Coldco", "https://example.com/s4")
        blocked = self._add("Blocked", "Clinicalco", "https://example.com/s5")
        db.update_job_blocker_gate(blocked, "skip", "Registration gate.", {"verdict": "skip"})

        self.assertEqual(self._export()["job_ids"], [keep])
        self.assertCountEqual(self._export(include_blocked=True)["job_ids"], [keep, blocked])

    def test_warm_roles_lead_the_packet(self):
        cold = self._add("Cold", "Coldco", "https://example.com/s6", match_score=95, composite_score=95)
        warm = self._add("Warm", "Warmco", "https://example.com/s7", match_score=61, composite_score=61)
        db.set_job_channel(warm, db.CHANNEL_WARM_REFERRAL)
        self.assertEqual(self._export()["job_ids"], [warm, cold])

    def test_min_score_and_limit_are_respected(self):
        self._add("Low", "A", "https://example.com/s8", match_score=40)
        high = self._add("High", "B", "https://example.com/s9", match_score=90)
        self.assertEqual(self._export(min_score=60)["job_ids"], [high])

        for n in range(5):
            self._add(f"Bulk {n}", f"C{n}", f"https://example.com/s10{n}", match_score=70)
        self.assertEqual(self._export(limit=3)["count"], 3)

    def test_only_the_requested_stages_are_included(self):
        new_job = self._add("New role", "A", "https://example.com/s11")
        applied = self._add("Applied role", "B", "https://example.com/s12", pipeline_stage="applied")
        self.assertEqual(self._export()["job_ids"], [new_job])
        self.assertEqual(self._export(stages=["applied"])["job_ids"], [applied])

    def test_an_empty_shortlist_still_writes_a_readable_packet(self):
        result = self._export()
        self.assertEqual(result["count"], 0)
        markdown = self._read(result, ".md")
        self.assertIn("0 roles surviving triage", markdown)
        self.assertEqual(json.loads(self._read(result, ".json"))["jobs"], [])

    def test_output_dir_is_created_when_missing(self):
        self._add("IT Manager", "Coldco", "https://example.com/s13")
        nested = str(Path(self.out_dir) / "nested" / "packets")
        result = self._export(output_dir=nested)
        self.assertTrue(Path(nested).is_dir())
        self.assertTrue(all(Path(p).exists() for p in result["files"]))


if __name__ == "__main__":
    unittest.main()
