"""Fit analysis reads the lane's base cover letter as well as the resume."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import docx  # noqa: E402

import database_manager as db  # noqa: E402
import db_setup  # noqa: E402
import python_bridge as bridge  # noqa: E402
from bridge import documents  # noqa: E402


class CoverLetterEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_cover_letter_test_")
        cls.original_db_file = db.DB_FILE
        cls.original_setup_db_file = db_setup.DB_FILE
        cls.db_file = str(Path(cls.test_data) / "job_applications.db")
        db.DB_FILE = cls.db_file
        db_setup.DB_FILE = cls.db_file
        db._wal_enabled = False
        db_setup.setup_database()
        cls.original_cwd = os.getcwd()
        os.chdir(cls.test_data)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.original_cwd)
        db.DB_FILE = cls.original_db_file
        db_setup.DB_FILE = cls.original_setup_db_file
        db._wal_enabled = False
        shutil.rmtree(cls.test_data, ignore_errors=True)

    def _lane(self, name):
        resume = Path(self.test_data) / f"{name}.docx"
        document = docx.Document()
        document.add_paragraph("Resume: led ERP rollout at Acme.")
        document.save(str(resume))
        db.add_lane(name, str(resume))
        return db.get_profile_by_name(name)["id"]

    def test_without_a_cover_letter_the_text_is_the_resume(self):
        lane_id = self._lane("No letter")
        self.assertEqual(documents.read_fit_evidence_text(lane_id), documents.read_resume_text(lane_id))

    def test_cover_letter_projects_reach_the_analysis_text(self):
        lane_id = self._lane("With letter")
        letter = Path(self.test_data) / "letter.txt"
        letter.write_text("I built a solar battery optimiser as a personal project.", encoding="utf-8")
        profile = db.get_profile_by_id(lane_id)
        bridge.COMMANDS["profiles:update"]({
            "profile_id": lane_id, "name": profile["name"], "resume_path": profile["resume_path"],
            "cover_letter_path": str(letter),
        })
        text = documents.read_fit_evidence_text(lane_id)
        self.assertIn("led ERP rollout", text)
        self.assertIn("COVER LETTER", text)
        self.assertIn("solar battery optimiser", text)

        bridge.COMMANDS["profiles:update"]({
            "profile_id": lane_id, "name": profile["name"], "resume_path": profile["resume_path"],
            "cover_letter_path": "",
        })
        self.assertNotIn("solar battery optimiser", documents.read_fit_evidence_text(lane_id))

    def test_a_missing_cover_letter_never_stops_analysis(self):
        lane_id = self._lane("Moved letter")
        db.set_profile_cover_letter(lane_id, str(Path(self.test_data) / "gone.docx"))
        self.assertEqual(documents.read_fit_evidence_text(lane_id), documents.read_resume_text(lane_id))


if __name__ == "__main__":
    unittest.main()
