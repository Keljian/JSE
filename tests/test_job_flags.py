"""Regression checks for job flags.

Flags replaced an earlier design that returned a skip/stretch/clear verdict and
blocked document generation on a skip. That was the wrong shape: a model does
not have the standing to decide which roles are worth an application, and being
overruled by one is worse than getting no help at all. Flags surface the same
detection as observations instead.

The properties that matter now are the opposite of a gate's:

- Nothing branches on a flag. No blocked generation, no capped score, no role
  dropped from a listing.
- A flag names the ad's own requirement, or it is dropped as noise.
- Low confidence is kept and labelled rather than discarded, because the person
  reading decides.
- Flagging costs no extra LLM call: it rides along with triage.
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


class RequirementExtractionTests(unittest.TestCase):
    """The deterministic pre-pass that keeps credential checks cheap."""

    def setUp(self):
        import llm_handler
        self.extract = llm_handler._extract_mandatory_requirements

    def test_pulls_mandatory_lines_and_flags_credential_gates(self):
        ad = (
            "About the role. You will lead a small technology team.\n"
            "Applicants must hold current AHPRA registration.\n"
            "Experience with Dynamics 365 is highly regarded.\n"
            "A tertiary qualification in engineering is essential.\n"
        )
        mandatory, credential = self.extract(ad)
        self.assertTrue(any("AHPRA registration" in line for line in mandatory))
        self.assertTrue(any("AHPRA registration" in line for line in credential))
        self.assertTrue(any("tertiary qualification" in line for line in credential))

    def test_ignores_desirable_framing(self):
        ad = (
            "Experience with Kubernetes is desirable.\n"
            "Ideally you will have led a team before.\n"
            "A PMP certification would be advantageous.\n"
        )
        self.assertEqual(self.extract(ad), ([], []))

    def test_mandatory_duty_is_not_a_credential_gate(self):
        mandatory, credential = self.extract("You must be comfortable working across multiple sites each week.")
        self.assertEqual(len(mandatory), 1)
        self.assertEqual(credential, [])

    def test_deduplicates_and_respects_limit(self):
        self.assertEqual(len(self.extract("\n".join(["Applicants must hold a licence."] * 30))[0]), 1)
        varied = "\n".join(f"Applicants must hold credential number {n}." for n in range(40))
        self.assertEqual(len(self.extract(varied, limit=5)[0]), 5)

    def test_empty_input_is_safe(self):
        self.assertEqual(self.extract(None), ([], []))
        self.assertEqual(self.extract(""), ([], []))


class FlagNormalisationTests(unittest.TestCase):
    def setUp(self):
        import llm_handler
        self.normalise = llm_handler._normalise_job_flags

    def test_evidenced_flags_survive(self):
        result = self.normalise({
            "flags": [{
                "type": "credential_gate",
                "requirement": "AHPRA registration is mandatory.",
                "detail": "Not held.",
                "confidence": "high",
            }],
            "flag_summary": "Registration gate.",
        })
        self.assertEqual(len(result["flags"]), 1)
        self.assertEqual(result["flags"][0]["label"], "Credential gate")
        self.assertEqual(result["flags"][0]["source"], "auto")

    def test_a_flag_without_a_requirement_is_dropped(self):
        # Unevidenced flags are noise, and noise is what makes people stop
        # reading the evidenced ones.
        result = self.normalise({"flags": [{"type": "domain_mismatch", "detail": "feels wrong"}]})
        self.assertEqual(result["flags"], [])

    def test_low_confidence_is_kept_not_discarded(self):
        # The opposite of the old gate, which downgraded low-confidence skips.
        # Nothing is thrown away on the reader's behalf.
        result = self.normalise({
            "flags": [{"type": "credential_gate", "requirement": "Needs NV1.", "confidence": "low"}],
        })
        self.assertEqual(len(result["flags"]), 1)
        self.assertEqual(result["flags"][0]["confidence"], "low")

    def test_unknown_type_falls_back_rather_than_dropping_the_observation(self):
        result = self.normalise({"flags": [{"type": "vibes", "requirement": "Something odd."}]})
        self.assertEqual(result["flags"][0]["type"], "evidence_gap")

    def test_missing_confidence_defaults_to_low(self):
        result = self.normalise({"flags": [{"type": "evidence_gap", "requirement": "No ERP experience."}]})
        self.assertEqual(result["flags"][0]["confidence"], "low")

    def test_seniority_direction_is_normalised(self):
        self.assertEqual(self.normalise({"seniority_direction": "below"})["seniority_direction"], "below")
        self.assertEqual(self.normalise({"seniority_direction": "sideways"})["seniority_direction"], "unknown")
        self.assertEqual(self.normalise({})["seniority_direction"], "unknown")

    def test_summary_is_always_populated(self):
        self.assertEqual(self.normalise({})["summary"], "Nothing stood out.")
        self.assertIn("1 flag", self.normalise({
            "flags": [{"type": "evidence_gap", "requirement": "x"}]
        })["summary"])

    def test_no_flags_is_a_valid_answer(self):
        result = self.normalise({"flags": [], "flag_summary": "Nothing stood out."})
        self.assertEqual(result["flags"], [])


class _FlagDbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_flags_test_")
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
            {"title": "Clinical Services Manager", "company": "Example Health",
             "location": "Melbourne VIC", "url": "https://example.com/jobs/clinical-1",
             "description": "Applicants must hold current AHPRA registration."},
            "Seek", 1,
        )
        with db.get_db_connection() as conn:
            self.job_id = conn.execute("SELECT id FROM jobs WHERE url LIKE '%clinical-1%'").fetchone()["id"]

    def _record(self, **overrides):
        record = {
            "flags": [{
                "type": "credential_gate",
                "requirement": "AHPRA registration is mandatory.",
                "detail": "Not held.",
                "confidence": "high",
            }],
            "summary": "Registration gate.",
            "seniority_direction": "aligned",
        }
        record.update(overrides)
        return record


class FlagPersistenceTests(_FlagDbTestCase):
    def test_flags_round_trip(self):
        db.update_job_flags(self.job_id, self._record())
        stored = db.get_job_flags(self.job_id)
        self.assertEqual(len(stored["flags"]), 1)
        self.assertEqual(stored["flags"][0]["requirement"], "AHPRA registration is mandatory.")
        self.assertEqual(stored["summary"], "Registration gate.")
        self.assertTrue(stored["checked_at"])

    def test_types_are_denormalised_for_filtering(self):
        db.update_job_flags(self.job_id, self._record(flags=[
            {"type": "credential_gate", "requirement": "a"},
            {"type": "domain_mismatch", "requirement": "b"},
            {"type": "credential_gate", "requirement": "c"},
        ]))
        with db.get_db_connection() as conn:
            types = conn.execute("SELECT job_flags_types FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0]
        self.assertEqual(sorted(types.split(",")), ["credential_gate", "domain_mismatch"])

    def test_a_manual_flag_survives_reanalysis(self):
        # Re-analysis cannot re-derive "the recruiter would not name the client";
        # only the person knows, so their flags are not overwritten.
        db.add_job_flag(self.job_id, "evidence_gap", "Recruiter would not name the client.")
        db.update_job_flags(self.job_id, self._record())
        requirements = [flag["requirement"] for flag in db.get_job_flags(self.job_id)["flags"]]
        self.assertIn("Recruiter would not name the client.", requirements)
        self.assertIn("AHPRA registration is mandatory.", requirements)

    def test_dismissing_removes_only_that_flag(self):
        db.update_job_flags(self.job_id, self._record(flags=[
            {"type": "credential_gate", "requirement": "Needs AHPRA."},
            {"type": "evidence_gap", "requirement": "No ERP experience."},
        ]))
        db.dismiss_job_flag(self.job_id, "Needs AHPRA.")
        remaining = [flag["requirement"] for flag in db.get_job_flags(self.job_id)["flags"]]
        self.assertEqual(remaining, ["No ERP experience."])

    def test_clearing_removes_manual_flags_too(self):
        db.add_job_flag(self.job_id, "evidence_gap", "Mine.")
        db.clear_job_flags(self.job_id)
        self.assertEqual(db.get_job_flags(self.job_id)["flags"], [])

    def test_flags_never_change_the_score_or_stage(self):
        # The core property: a flag is an observation. Recording one must not
        # move the job in the pipeline or touch its score.
        with db.get_db_connection() as conn:
            conn.execute("UPDATE jobs SET match_score = 82, pipeline_stage = 'interested' WHERE id = ?",
                         (self.job_id,))
            conn.commit()
        db.update_job_flags(self.job_id, self._record())
        job = db.get_job_details(self.job_id)
        self.assertEqual(job["match_score"], 82)
        self.assertEqual(job["pipeline_stage"], "interested")

    def test_unreadable_stored_json_degrades_to_no_flags(self):
        with db.get_db_connection() as conn:
            conn.execute("UPDATE jobs SET job_flags_json = 'not json' WHERE id = ?", (self.job_id,))
            conn.commit()
        self.assertEqual(db.get_job_flags(self.job_id)["flags"], [])


class NothingBlocksTests(_FlagDbTestCase):
    """Document generation must proceed regardless of flags."""

    def test_document_generation_has_no_flag_guard(self):
        import bridge.jobs
        self.assertFalse(hasattr(bridge.jobs, "assert_blocker_gate_clear"),
                         "the blocking guard is gone; flags do not gate generation")
        self.assertTrue(hasattr(bridge.jobs, "report_job_flags"))

    def test_reporting_flags_raises_nothing(self):
        import bridge.jobs
        db.update_job_flags(self.job_id, self._record())
        bridge.jobs.report_job_flags(db.get_job_details(self.job_id))

    def test_no_blocking_error_type_remains(self):
        import bridge.runtime
        import python_bridge
        self.assertFalse(hasattr(bridge.runtime, "BlockerGateError"))
        self.assertFalse(hasattr(python_bridge, "BlockerGateError"))

    def test_shortlist_keeps_flagged_roles(self):
        # The packet exists so a human can decide; pre-filtering it would make
        # the decision for them.
        import python_bridge
        db.update_job_flags(self.job_id, self._record())
        out = tempfile.mkdtemp(prefix="jse_flag_shortlist_")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        result = python_bridge.COMMANDS["jobs:exportShortlist"]({"profile_id": 1, "output_dir": out})
        self.assertIn(self.job_id, result["job_ids"])

    def test_shortlist_can_be_narrowed_on_request(self):
        import python_bridge
        db.update_job_flags(self.job_id, self._record())
        out = tempfile.mkdtemp(prefix="jse_flag_shortlist2_")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        result = python_bridge.COMMANDS["jobs:exportShortlist"]({
            "profile_id": 1, "output_dir": out, "exclude_flags": ["credential_gate"],
        })
        self.assertNotIn(self.job_id, result["job_ids"])


class TriageCarriesFlagsTests(_FlagDbTestCase):
    """Flagging rides along with triage rather than costing a second call."""

    def _run_triage(self, response):
        import llm_handler
        import llm.analysis as analysis
        calls = []

        def stub(messages=None, **kwargs):
            calls.append(messages or [])
            return response

        original = analysis._call_scoring_ai
        try:
            llm_handler._call_scoring_ai = stub
            result = analysis._triage_job("RESUME SUMMARY", "Applicants must hold NV1 clearance.",
                                          "IT Manager", 1, lambda message: None)
        finally:
            llm_handler._call_scoring_ai = original
        return result, calls

    def test_triage_returns_flags_in_a_single_call(self):
        (score, reason, keep, flags), calls = self._run_triage(
            '{"match_score": 72, "reason": "Adjacent.", "keep": true,'
            ' "flags": [{"type": "credential_gate", "requirement": "NV1 clearance required.",'
            ' "detail": "Not held.", "confidence": "high"}],'
            ' "seniority_direction": "aligned", "flag_summary": "Clearance gate."}'
        )
        self.assertEqual(len(calls), 1, "flagging must not cost a second LLM call")
        self.assertEqual(score, 72)
        self.assertTrue(keep)
        self.assertEqual(len(flags["flags"]), 1)
        self.assertEqual(flags["summary"], "Clearance gate.")

    def test_the_full_ad_and_extracted_requirements_reach_the_prompt(self):
        _, calls = self._run_triage('{"match_score": 50, "reason": "x", "keep": true, "flags": []}')
        prompt = "\n".join(message["content"] for message in calls[0])
        self.assertIn("FULL JOB ADVERTISEMENT", prompt)
        self.assertIn("NV1 clearance", prompt)
        self.assertIn("MANDATORY REQUIREMENT LINES", prompt)

    def test_unparseable_triage_fails_open_with_no_flags(self):
        (score, reason, keep, flags), _ = self._run_triage("not json at all")
        self.assertEqual(score, 100, "triage must fail open, not fail closed")
        self.assertTrue(keep)
        self.assertIsNone(flags)


if __name__ == "__main__":
    unittest.main()
