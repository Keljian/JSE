"""Regression checks for the two-track document strategy.

Overqualification screening is a measured rejection cause on support-grade
roles, and the fix is to write the documents to a different brief. That only
works if the track is chosen correctly (one weak signal must not flip it), a
manual choice sticks, and the chosen track actually reaches the prompts.
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
import rich_application  # noqa: E402


class TrackDerivationTests(unittest.TestCase):
    def test_senior_title_stays_on_the_senior_track(self):
        result = db.document_track({"title": "Head of Technology", "salary": "$170,000"})
        self.assertEqual(result["track"], db.DOC_TRACK_SENIOR)
        self.assertEqual(result["source"], "derived")

    def test_a_single_weak_signal_does_not_flip_the_track(self):
        # A support-grade word in the title alone is noise: "IT Support Manager"
        # is a manager role. One signal must not strip the resume.
        result = db.document_track({"title": "IT Support Manager", "salary": "$150,000"})
        self.assertEqual(result["track"], db.DOC_TRACK_SENIOR)

    def test_two_weak_signals_together_flip_the_track(self):
        result = db.document_track({"title": "IT Support Officer", "salary": "$72,000"})
        self.assertEqual(result["track"], db.DOC_TRACK_STRIPPED)
        self.assertTrue(len(result["reasons"]) >= 2)

    def test_the_gate_saying_below_is_enough_on_its_own(self):
        # The gate looked at the whole ad against the whole resume; that
        # judgement outranks the keyword heuristics.
        result = db.document_track(
            {"title": "Technology Lead", "salary": "$150,000"},
            {"seniority_direction": "below"},
        )
        self.assertEqual(result["track"], db.DOC_TRACK_STRIPPED)
        self.assertTrue(any("Blocker gate" in reason for reason in result["reasons"]))

    def test_gate_saying_aligned_or_above_does_not_strip(self):
        for direction in ("aligned", "above", "unknown"):
            result = db.document_track({"title": "Head of Technology"}, {"seniority_direction": direction})
            self.assertEqual(result["track"], db.DOC_TRACK_SENIOR, direction)

    def test_reasons_are_always_populated(self):
        self.assertTrue(db.document_track({"title": "Head of Technology"})["reasons"])
        self.assertTrue(db.document_track({})["reasons"])


class TrackPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_track_test_")
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
            conn.execute("DELETE FROM jobs")
            conn.commit()
        db.add_job(
            {"title": "Head of Technology", "company": "Flavorite", "location": "Melbourne VIC",
             "url": "https://example.com/track", "description": "Lead the technology function."},
            "Seek", 1,
        )
        with db.get_db_connection() as conn:
            self.job_id = conn.execute("SELECT id FROM jobs WHERE url LIKE '%track%'").fetchone()["id"]

    def test_manual_track_overrides_derivation(self):
        self.assertEqual(db.resolve_document_track(self.job_id)["track"], db.DOC_TRACK_SENIOR)
        db.set_job_document_track(self.job_id, db.DOC_TRACK_STRIPPED)
        resolved = db.resolve_document_track(self.job_id)
        self.assertEqual(resolved["track"], db.DOC_TRACK_STRIPPED)
        self.assertEqual(resolved["source"], "manual")

    def test_clearing_returns_to_derived(self):
        db.set_job_document_track(self.job_id, db.DOC_TRACK_STRIPPED)
        db.set_job_document_track(self.job_id, "")
        self.assertEqual(db.resolve_document_track(self.job_id)["source"], "derived")

    def test_unknown_track_is_rejected(self):
        with self.assertRaises(ValueError):
            db.set_job_document_track(self.job_id, "middling")

    def test_the_stored_gate_direction_feeds_the_resolver(self):
        db.update_job_blocker_gate(self.job_id, "stretch", "Below ceiling.", {
            "verdict": "stretch", "seniority_direction": "below",
        })
        self.assertEqual(db.resolve_document_track(self.job_id)["track"], db.DOC_TRACK_STRIPPED)

    def test_bridge_command_round_trips(self):
        import python_bridge as bridge
        result = bridge.COMMANDS["jobs:setDocumentTrack"]({
            "job_id": self.job_id, "track": db.DOC_TRACK_STRIPPED
        })
        self.assertEqual(result["track"], db.DOC_TRACK_STRIPPED)
        self.assertEqual(result["resolved"]["source"], "manual")

        cleared = bridge.COMMANDS["jobs:setDocumentTrack"]({"job_id": self.job_id, "track": ""})
        self.assertIsNone(cleared["track"])
        self.assertEqual(cleared["resolved"]["source"], "derived")

    def test_detail_exposes_the_resolved_track(self):
        import python_bridge as bridge
        detail = bridge.COMMANDS["jobs:detail"]({"job_id": self.job_id})
        self.assertEqual(detail["job"]["document_track_resolved"]["track"], db.DOC_TRACK_SENIOR)


class TrackPromptTests(unittest.TestCase):
    """The track has to change the brief, not just the log line."""

    def test_stripped_resume_brief_targets_the_ad_scope(self):
        stripped = rich_application.resume_task("stripped_back")
        senior = rich_application.resume_task("senior")
        self.assertIn("STRIPPED BACK", stripped)
        self.assertIn("overqualified", stripped)
        self.assertIn("never misrepresent history", stripped)
        self.assertNotIn("STRIPPED BACK", senior)
        self.assertIn("FULL SENIOR", senior)

    def test_both_tracks_keep_the_base_resume_contract(self):
        for track in ("senior", "stripped_back"):
            brief = rich_application.resume_task(track)
            self.assertIn("Professional Summary", brief)
            self.assertIn("Output ONLY the resume Markdown", brief)

    def test_stripped_cover_letter_addresses_positioning(self):
        stripped = rich_application.cover_task("1 January 2026", "A Candidate", "stripped_back")
        senior = rich_application.cover_task("1 January 2026", "A Candidate", "senior")
        self.assertIn("POSITIONING", stripped)
        self.assertIn("below the candidate's demonstrated ceiling", stripped)
        self.assertNotIn("POSITIONING", senior)

    def test_unknown_track_falls_back_to_senior(self):
        self.assertEqual(rich_application.resume_task("nonsense"), rich_application.resume_task("senior"))
        self.assertEqual(
            rich_application.cover_task("1 January 2026", "A", "nonsense"),
            rich_application.cover_task("1 January 2026", "A", "senior"),
        )


if __name__ == "__main__":
    unittest.main()
