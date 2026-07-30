"""Regression checks for the hard-blocker gate.

The gate is the only stage in the pipeline allowed to return a decisive "don't
apply", so it carries two opposite risks: it must actually stop document
generation when it fires, and it must not become a new source of false
negatives. These tests pin both sides — the deterministic requirement
extractor, the verdict normalisation and downgrade rules, persistence, and the
document-generation guard.
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
    """The deterministic pre-pass that narrows what the gate LLM has to judge."""

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
        self.assertTrue(any("tertiary qualification" in line for line in mandatory))
        self.assertTrue(any("AHPRA registration" in line for line in credential))
        self.assertTrue(any("tertiary qualification" in line for line in credential))

    def test_ignores_desirable_framing(self):
        ad = (
            "Experience with Kubernetes is desirable.\n"
            "Ideally you will have led a team before.\n"
            "A PMP certification would be advantageous.\n"
        )
        mandatory, credential = self.extract(ad)
        self.assertEqual(mandatory, [])
        self.assertEqual(credential, [])

    def test_mandatory_duty_is_not_a_credential_gate(self):
        ad = "You must be comfortable working across multiple sites each week."
        mandatory, credential = self.extract(ad)
        self.assertEqual(len(mandatory), 1)
        self.assertEqual(credential, [])

    def test_deduplicates_and_respects_limit(self):
        ad = "\n".join(["Applicants must hold a current driver licence."] * 30)
        mandatory, _ = self.extract(ad)
        self.assertEqual(len(mandatory), 1)

        varied = "\n".join(f"Applicants must hold credential number {n}." for n in range(40))
        mandatory, _ = self.extract(varied, limit=5)
        self.assertEqual(len(mandatory), 5)

    def test_empty_input_is_safe(self):
        self.assertEqual(self.extract(None), ([], []))
        self.assertEqual(self.extract(""), ([], []))


class VerdictNormalisationTests(unittest.TestCase):
    """Safety rules that stop the gate becoming a false-negative machine."""

    def setUp(self):
        import llm_handler
        self.normalise = llm_handler._normalise_blocker_gate

    def test_confident_evidenced_skip_survives(self):
        result = self.normalise({
            "verdict": "skip",
            "confidence": "high",
            "hard_blockers": [{"requirement": "AHPRA registration is mandatory.", "why_unmet": "Not held."}],
            "reason": "Mandatory clinical registration.",
        })
        self.assertEqual(result["verdict"], "skip")
        self.assertIsNone(result["downgraded_from"])

    def test_skip_without_evidence_is_downgraded(self):
        result = self.normalise({"verdict": "skip", "confidence": "high", "hard_blockers": [], "reason": "Feels wrong."})
        self.assertEqual(result["verdict"], "stretch")
        self.assertEqual(result["downgraded_from"], "skip")

    def test_low_confidence_skip_is_downgraded_and_keeps_blockers_as_gaps(self):
        result = self.normalise({
            "verdict": "skip",
            "confidence": "low",
            "hard_blockers": [{"requirement": "Five years in local government.", "why_unmet": "Resume shows two."}],
            "reason": "Thin recruiter ad.",
        })
        self.assertEqual(result["verdict"], "stretch")
        self.assertEqual(result["downgraded_from"], "skip")
        self.assertTrue(any("local government" in gap for gap in result["named_gaps"]))

    def test_clear_with_gaps_becomes_stretch(self):
        result = self.normalise({
            "verdict": "clear",
            "confidence": "high",
            "named_gaps": ["No named ERP experience."],
        })
        self.assertEqual(result["verdict"], "stretch")

    def test_unrecognised_verdict_is_unknown(self):
        self.assertEqual(self.normalise({"verdict": "maybe"})["verdict"], "unknown")
        self.assertEqual(self.normalise({})["verdict"], "unknown")

    def test_verdict_aliases_are_accepted(self):
        self.assertEqual(self.normalise({"verdict": "stretch-with-named-gaps"})["verdict"], "stretch")
        self.assertEqual(self.normalise({"verdict": "clear_fit"})["verdict"], "clear")

    def test_string_blockers_are_tolerated(self):
        result = self.normalise({
            "verdict": "skip",
            "confidence": "high",
            "hard_blockers": ["Requires a security clearance the candidate does not hold."],
        })
        self.assertEqual(result["verdict"], "skip")
        self.assertEqual(result["hard_blockers"][0]["why_unmet"], "")

    def test_missing_confidence_defaults_to_low(self):
        # An absent confidence must not be read as high — a skip on an
        # unstated confidence gets downgraded like any other low-confidence one.
        result = self.normalise({
            "verdict": "skip",
            "hard_blockers": [{"requirement": "Must hold a licence.", "why_unmet": "Not held."}],
        })
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["verdict"], "stretch")


class _BlockerDbTestCase(unittest.TestCase):
    """Isolated database plus one gated job, shared by the DB-backed suites."""

    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_blocker_test_")
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
            {
                "title": "Clinical Services Manager",
                "company": "Example Health",
                "location": "Melbourne VIC",
                "url": "https://example.com/jobs/clinical-1",
                "description": "Applicants must hold current AHPRA registration.",
            },
            "Seek",
            1,
        )
        with db.get_db_connection() as conn:
            self.job_id = conn.execute(
                "SELECT id FROM jobs WHERE url LIKE '%clinical-1%'"
            ).fetchone()["id"]


class BlockerPersistenceTests(_BlockerDbTestCase):
    def test_verdict_round_trips_with_details(self):
        payload = {
            "verdict": "skip",
            "confidence": "high",
            "hard_blockers": [{"requirement": "AHPRA registration.", "why_unmet": "Not held."}],
            "named_gaps": [],
        }
        db.update_job_blocker_gate(self.job_id, "skip", "Mandatory clinical registration.", payload)
        gate = db.get_job_blocker_gate(self.job_id)
        self.assertEqual(gate["verdict"], "skip")
        self.assertEqual(gate["reason"], "Mandatory clinical registration.")
        self.assertEqual(gate["details"]["hard_blockers"][0]["requirement"], "AHPRA registration.")
        self.assertTrue(gate["checked_at"])

    def test_clear_removes_the_verdict_and_queues_reanalysis(self):
        with db.get_db_connection() as conn:
            conn.execute("UPDATE jobs SET analysis_signature = 'sig' WHERE id = ?", (self.job_id,))
            conn.commit()
        db.update_job_blocker_gate(self.job_id, "skip", "Blocked.", {"verdict": "skip"})
        db.clear_job_blocker_gate(self.job_id)
        gate = db.get_job_blocker_gate(self.job_id)
        self.assertEqual(gate["verdict"], "unknown")
        self.assertEqual(gate["reason"], "")
        self.assertIsNone(db.get_job_details(self.job_id)["analysis_signature"])

    def test_clear_can_leave_the_analysis_signature_alone(self):
        with db.get_db_connection() as conn:
            conn.execute("UPDATE jobs SET analysis_signature = 'sig' WHERE id = ?", (self.job_id,))
            conn.commit()
        db.clear_job_blocker_gate(self.job_id, reset_analysis=False)
        self.assertEqual(db.get_job_details(self.job_id)["analysis_signature"], "sig")

    def test_unserialisable_payload_does_not_break_persistence(self):
        db.update_job_blocker_gate(self.job_id, "stretch", "Gaps found.", {"bad": object()})
        gate = db.get_job_blocker_gate(self.job_id)
        self.assertEqual(gate["verdict"], "stretch")
        self.assertEqual(gate["details"], {})

    def test_pipeline_list_carries_the_verdict(self):
        db.update_job_blocker_gate(self.job_id, "skip", "Blocked.", {"verdict": "skip"})
        rows = db.get_pipeline_jobs({"compact": True, "profile_id": 1})
        match = [row for row in rows if row["id"] == self.job_id]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["blocker_verdict"], "skip")

    def test_normalize_verdict_fails_open(self):
        self.assertEqual(db.normalize_blocker_verdict("SKIP"), "skip")
        self.assertEqual(db.normalize_blocker_verdict("stretch-with-named-gaps"), "stretch")
        self.assertEqual(db.normalize_blocker_verdict(None), "unknown")
        self.assertEqual(db.normalize_blocker_verdict("nonsense"), "unknown")


class DocumentGuardTests(_BlockerDbTestCase):
    """A skip verdict has to actually stop document generation."""

    def _guard(self):
        # The guard lives in bridge.jobs; python_bridge is only the entrypoint
        # and the merged dispatch table.
        import bridge.jobs
        return bridge.jobs

    def test_skip_blocks_generation(self):
        bridge = self._guard()
        db.update_job_blocker_gate(self.job_id, "skip", "Mandatory clinical registration.", {"verdict": "skip"})
        job = db.get_job_details(self.job_id)
        with self.assertRaises(bridge.BlockerGateError) as caught:
            bridge.assert_blocker_gate_clear(job, {})
        self.assertIn("clinical registration", str(caught.exception))

    def test_override_allows_generation(self):
        bridge = self._guard()
        db.update_job_blocker_gate(self.job_id, "skip", "Mandatory clinical registration.", {"verdict": "skip"})
        job = db.get_job_details(self.job_id)
        bridge.assert_blocker_gate_clear(job, {"override_blocker": True})
        events = db.get_job_events(self.job_id)
        self.assertTrue(any("overridden" in str(row["title"]).lower() for row in events))

    def test_other_verdicts_do_not_block(self):
        bridge = self._guard()
        for verdict in ("stretch", "clear", "unknown"):
            db.update_job_blocker_gate(self.job_id, verdict, "Fine.", {"verdict": verdict})
            bridge.assert_blocker_gate_clear(db.get_job_details(self.job_id), {})

    def test_ungated_job_does_not_block(self):
        bridge = self._guard()
        bridge.assert_blocker_gate_clear(db.get_job_details(self.job_id), {})


class AnalysisShortCircuitTests(_BlockerDbTestCase):
    """A skip verdict must stop the pipeline before the expensive full analysis."""

    def _ctx(self, log_lines):
        import llm_handler
        return {
            "log": log_lines.append,
            "resume_text": "Technology leadership resume.",
            "resume_summary": "TARGET ROLE FAMILIES: Senior IT leadership.",
            "preference_context": "No extra lane weighting terms have been set.",
            "lane_target_text": "Head of IT, IT Manager",
            "fragment_context": "",
            "system_prompt": llm_handler.ANALYSIS_SYSTEM_PROMPT,
            "profile_id": 1,
        }

    def _run(self, verdict, blockers, gaps=()):
        """Run one job through the pipeline with the LLM stages stubbed.

        Returns the prompts passed to the full-analysis call — empty when the
        gate short-circuited. A recorded prompt, rather than a raised
        assertion, is used to detect the short-circuit because
        _analyze_single_job deliberately swallows exceptions from the analysis
        stage, so a raising stub would be silently absorbed.
        """
        import llm_handler
        log_lines = []
        analysis_calls = []
        job = db.get_job_details(self.job_id)
        originals = {
            name: getattr(llm_handler, name)
            for name in ("_triage_job", "_run_blocker_gate", "_call_scoring_ai")
        }
        llm_handler._triage_job = lambda *a, **k: (72, "Adjacent senior role.", True)
        llm_handler._run_blocker_gate = lambda *a, **k: {
            "verdict": verdict,
            "confidence": "high",
            "hard_blockers": list(blockers),
            "named_gaps": list(gaps),
            "domain_match": "Clinical services; not practised.",
            "seniority_match": "Level is plausible.",
            "reason": "Mandatory clinical registration.",
            "downgraded_from": None,
        }

        def _record(messages=None, **kwargs):
            analysis_calls.append(messages or [])
            return '{"match_score": 70, "fit_level": "Moderate", "suitability_summary": "Stub."}'

        llm_handler._call_scoring_ai = _record
        try:
            llm_handler._analyze_single_job(job, self._ctx(log_lines))
        finally:
            for name, value in originals.items():
                setattr(llm_handler, name, value)
        return analysis_calls

    def test_skip_persists_verdict_and_skips_full_analysis(self):
        import llm_handler
        blockers = [{"requirement": "AHPRA registration is mandatory.", "why_unmet": "Not held."}]
        analysis_calls = self._run("skip", blockers)
        self.assertEqual(analysis_calls, [], "full analysis ran despite a skip verdict")

        gate = db.get_job_blocker_gate(self.job_id)
        self.assertEqual(gate["verdict"], "skip")

        job = db.get_job_details(self.job_id)
        # Capped at the keep floor: visible for review, never promoted, and
        # deliberately not below the auto-reject line.
        self.assertEqual(job["match_score"], llm_handler.BLOCKER_SKIP_SCORE_CAP)
        self.assertGreaterEqual(job["match_score"], db.AUTO_REJECT_THRESHOLD)
        self.assertIn("Hard-Blocker Gate", job["ai_analysis"])
        self.assertIn("AHPRA registration", job["ai_analysis"])

    def test_skip_does_not_auto_reject_the_job(self):
        blockers = [{"requirement": "AHPRA registration is mandatory.", "why_unmet": "Not held."}]
        self._run("skip", blockers)
        job = db.get_job_details(self.job_id)
        self.assertNotIn(db.normalize_stage(job["pipeline_stage"]), {"rejected", "archived"})

    def test_stretch_reaches_full_analysis_and_injects_named_gaps(self):
        # Positive control for the test above: without a skip the pipeline must
        # reach the analysis call, and the gate's named gaps must be in the
        # prompt so the analyser has to answer them instead of reframing.
        gaps = ["Ad asks for hands-on Dynamics 365; resume shows Salesforce."]
        analysis_calls = self._run("stretch", [], gaps)
        self.assertTrue(analysis_calls, "full analysis did not run for a stretch verdict")
        prompt = "\n".join(message["content"] for message in analysis_calls[0])
        self.assertIn("NAMED GAPS", prompt)
        self.assertIn("Dynamics 365", prompt)
        self.assertEqual(db.get_job_blocker_gate(self.job_id)["verdict"], "stretch")


if __name__ == "__main__":
    unittest.main()
