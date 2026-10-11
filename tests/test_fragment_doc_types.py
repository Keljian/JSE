"""The fragment engine keeps CV evidence and cover-letter evidence apart."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import corpus_miner  # noqa: E402
import database_manager as db  # noqa: E402
import db_setup  # noqa: E402
from llm import memory  # noqa: E402


def _fragment(theme, doc_type=None):
    return {"fragment_type": "achievement", "theme": theme, "claim": f"{theme} claim.", "doc_type": doc_type}


class FragmentStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_fragment_doc_type_test_")
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
            conn.execute("DELETE FROM candidate_fragments")
            conn.execute("DELETE FROM profile_memory_fragments")
            conn.commit()

    def _types(self, table="candidate_fragments"):
        with db.get_db_connection() as conn:
            return {row["theme"]: row["doc_type"] for row in conn.execute(f"SELECT theme, doc_type FROM {table}")}

    def test_doc_type_is_stored_and_merges_to_both(self):
        db.upsert_candidate_fragments(1, [_fragment("ERP rollout", "resume"), _fragment("Battery optimiser", "cover_letter")])
        self.assertEqual(self._types(), {"ERP rollout": "resume", "Battery optimiser": "cover_letter"})
        db.upsert_candidate_fragments(1, [_fragment("ERP rollout", "cover_letter")])
        self.assertEqual(self._types()["ERP rollout"], "both")

    def test_untagged_fragment_adopts_the_next_mining(self):
        db.upsert_profile_memory_fragments(1, [_fragment("Vendor governance")])
        self.assertIsNone(self._types("profile_memory_fragments")["Vendor governance"])
        db.upsert_profile_memory_fragments(1, [_fragment("Vendor governance", "resume")])
        self.assertEqual(self._types("profile_memory_fragments")["Vendor governance"], "resume")
        db.upsert_profile_memory_fragments(1, [_fragment("Vendor governance")])
        self.assertEqual(self._types("profile_memory_fragments")["Vendor governance"], "resume",
                         "an untagged re-mine must not erase a known type")

    def test_retrieval_by_document(self):
        db.upsert_candidate_fragments(1, [
            _fragment("Resume only", "resume"),
            _fragment("Letter only", "cover_letter"),
            _fragment("Both", "both"),
            _fragment("Untagged"),
        ])
        letter = {row["theme"] for row in db.get_lane_fragments(1, doc_type="cover_letter")}
        resume = {row["theme"] for row in db.get_lane_fragments(1, doc_type="resume")}
        everything = {row["theme"] for row in db.get_lane_fragments(1)}
        self.assertEqual(letter, {"Letter only", "Both", "Untagged"})
        self.assertEqual(resume, {"Resume only", "Both", "Untagged"})
        self.assertEqual(everything, {"Resume only", "Letter only", "Both", "Untagged"})
        self.assertEqual({row["theme"] for row in db.get_candidate_fragments(1, doc_type="resume")}, resume)


class MiningTests(unittest.TestCase):
    def test_resume_and_cover_letter_are_mined_in_separate_passes(self):
        calls = []

        def caller(system, user):
            calls.append((system, user))
            return '[{"fragment_type": "achievement", "theme": "T", "claim": "C"}]'

        with mock.patch.object(corpus_miner, "_fast_caller", return_value=(caller, "Test")):
            fragments, _ = corpus_miner.mine_documents([
                {"filename": "cv.docx", "text": "Resume text " * 20, "doc_type": "resume"},
                {"filename": "letter.docx", "text": "Letter text " * 20, "doc_type": "cover_letter"},
            ], {})
        self.assertEqual(len(calls), 2)
        self.assertIn("THESE DOCUMENTS ARE RESUMES", calls[0][0])
        self.assertIn("cv.docx", calls[0][1])
        self.assertNotIn("letter.docx", calls[0][1])
        self.assertIn("THESE DOCUMENTS ARE COVER LETTERS", calls[1][0])
        self.assertEqual([f["doc_type"] for f in fragments], ["resume", "cover_letter"])

    def test_untyped_documents_are_mined_as_resumes(self):
        with mock.patch.object(corpus_miner, "_fast_caller", return_value=(lambda s, u: "[]", "Test")):
            with mock.patch.object(corpus_miner, "_system_for", wraps=corpus_miner._system_for) as system_for:
                corpus_miner.mine_documents([{"filename": "cv.docx", "text": "Resume text"}], {})
        system_for.assert_called_once_with("resume")

    def test_kit_extraction_keeps_the_source_document(self):
        fragments = memory._normalise_memory_fragments([
            {"theme": "A", "claim": "a", "source_document": "cover letter"},
            {"theme": "B", "claim": "b", "source_document": "resume"},
            {"theme": "C", "claim": "c"},
        ])
        self.assertEqual([f["doc_type"] for f in fragments], ["cover_letter", "resume", None])

    def test_corpus_doc_types_map_to_fragment_doc_types(self):
        self.assertEqual(corpus_miner.fragment_doc_type("resume"), "resume")
        self.assertEqual(corpus_miner.fragment_doc_type("cover_letter"), "cover_letter")
        self.assertEqual(corpus_miner.fragment_doc_type("ksc_response"), "both")


if __name__ == "__main__":
    unittest.main()
