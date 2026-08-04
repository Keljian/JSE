"""Regression checks for the four ways a lane's own targeting was ignored.

All four were found together, from one job: a University of Melbourne workshop
Technician role, $100,847-$109,163, mechatronics and electronics duties, scored
42% and auto-rejected by a lane whose entire purpose is university technical
officer roles. None of the four was the model being wrong about the ad.

1. The positioning doctrine was global. It described the candidate's primary
   market and retired "university/council coordinator-grade roles" outright, so
   it capped the roles a secondary lane existed to find. It is now resolved per
   lane.
2. No scoring prompt ever saw the lane. Its titles, domains and seniority
   existed only as a token-overlap check, so the model judged level against the
   doctrine's primary track. They are now rendered into a lane brief.
3. Preference weighting silently did nothing. Terms were split on newlines
   only, and every populated lane stored them semicolon-delimited, so the whole
   field became one term that could never match.
4. The borderline rescue could not fire. It required a score at or above the
   keep floor, but the retired-track cap it exists to second-guess lands below
   that floor, and its exact-token matching could not see "Technician" in a lane
   hunting "Technical Officer".
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as db  # noqa: E402
import db_setup  # noqa: E402


# The ad that exposed all four, trimmed to the lines that carry the signal.
TECHNICIAN_AD = """Role type: Full-time; Continuing
Faculty: Engineering & Information Technology
Salary: UoM 6 - $100,847 - $109,163 p.a. plus 17% super
In this Technician role, you will support hands-on teaching and research across
busy workshops and makerspaces. You will provide technical advice, design and
build bespoke rigs, and coach students in safe, industry-aligned practice.
Bring proven expertise in one or more areas such as machining, fabrication,
mechatronics, electronics or woodwork. Demonstrate experience with additional
technologies like CAD, embedded systems, robotics, AI or automation.
Based at the University of Melbourne in Parkville, Melbourne.
"""

LANE = {
    "lane_intent": "University and TAFE technical officer roles across Greater Melbourne. "
                   "Teaching and research lab support, electronics/mechatronics, instrumentation, "
                   "workshop and equipment support. Secondary, reactive lane.",
    "target_titles": "Technical Officer; Senior Technical Officer; Laboratory Technical Officer; "
                     "Workshop Technical Officer; Engineering Technical Officer",
    "target_domains": "university technical officer; laboratory support; electronics; mechatronics",
    "seniority": "mid",
    "boost_terms": "technical officer; laboratory; electronics; mechatronics; instrumentation; "
                   "PCB; embedded; university; TAFE; workshop; Melbourne",
    "penalty_terms": "interstate; relocation; clearance",
}

LANE_TARGET_TEXT = " ".join([LANE["target_titles"], LANE["target_domains"], LANE["lane_intent"]])


class PreferenceTermTests(unittest.TestCase):
    """Weighting terms are free text; every separator a user reaches for works."""

    def setUp(self):
        import llm.analysis as analysis
        self.analysis = analysis

    def test_semicolons_commas_and_newlines_all_separate_terms(self):
        terms = self.analysis._preference_terms("technical officer; laboratory, electronics\nPCB")
        self.assertEqual(terms, ["technical officer", "laboratory", "electronics", "PCB"])

    def test_a_semicolon_list_is_not_treated_as_one_unmatchable_term(self):
        # The original bug in one line: splitting on newlines only left the
        # whole field as a single "term" no advertisement could ever contain.
        terms = self.analysis._preference_terms(LANE["boost_terms"])
        self.assertEqual(len(terms), 11)
        self.assertIn("technical officer", terms)

    def test_blank_and_missing_fields_yield_no_terms(self):
        for value in (None, "", "   ", ";;", " , \n "):
            self.assertEqual(self.analysis._preference_terms(value), [])


class PreferenceWeightTests(unittest.TestCase):
    """The weighting a user configures has to actually reach the score."""

    def setUp(self):
        import llm.analysis as analysis
        self.analysis = analysis
        self._original = analysis.db.get_lane_settings
        analysis.db.get_lane_settings = lambda _lane_id: dict(LANE)
        self.addCleanup(setattr, analysis.db, "get_lane_settings", self._original)

    def test_boost_terms_lift_a_matching_ad(self):
        score, boost, penalty = self.analysis._apply_preference_weight(42, TECHNICIAN_AD, 7)
        self.assertEqual(penalty, [])
        self.assertGreaterEqual(len(boost), 4)
        # Capped at +10 however many terms hit, and 42 was under the
        # auto-reject threshold this lift exists to clear.
        self.assertEqual(score, 52)
        self.assertGreaterEqual(score, db.AUTO_REJECT_THRESHOLD)

    def test_penalty_terms_subtract(self):
        score, _boost, penalty = self.analysis._apply_preference_weight(
            60, "Role requires relocation to a remote site.", 7)
        self.assertEqual(penalty, ["relocation"])
        self.assertEqual(score, 55)

    def test_weighting_stays_inside_the_score_range(self):
        low, _, _ = self.analysis._apply_preference_weight(2, "interstate relocation clearance", 7)
        high, _, _ = self.analysis._apply_preference_weight(98, TECHNICIAN_AD, 7)
        self.assertEqual(low, 0)
        self.assertEqual(high, 100)

    def test_the_prompt_shows_the_terms_individually(self):
        rendered = self.analysis._analysis_preferences(7)
        self.assertIn("technical officer; laboratory", rendered)


class PositioningDoctrineTests(unittest.TestCase):
    """A lane whose market is not the candidate's primary one needs its own view."""

    def setUp(self):
        import llm.prompts as prompts
        self.prompts = prompts

    def test_a_lane_without_an_override_uses_the_global_doctrine(self):
        for settings in (None, {}, {"positioning_doctrine": ""}, {"positioning_doctrine": "  "}):
            self.assertEqual(
                self.prompts.resolve_positioning_doctrine(settings),
                self.prompts.POSITIONING_DOCTRINE,
            )

    def test_a_lane_override_replaces_the_global_doctrine_entirely(self):
        resolved = self.prompts.resolve_positioning_doctrine({"positioning_doctrine": "TECHNICAL LANE VIEW"})
        self.assertEqual(resolved, "TECHNICAL LANE VIEW")
        # Not merely appended: the default's retired-track clause is the thing
        # the override exists to get rid of.
        self.assertNotIn("RETIRED", resolved)

    def test_system_prompts_carry_the_resolved_doctrine(self):
        prompt = self.prompts.with_doctrine(self.prompts.TRIAGE_SYSTEM_PROMPT_BASE,
                                            {"positioning_doctrine": "TECHNICAL LANE VIEW"})
        self.assertIn("first-pass classifier", prompt)
        self.assertIn("TECHNICAL LANE VIEW", prompt)
        self.assertNotIn("TRACK 1 — PRIMARY", prompt)

    def test_the_exported_constants_still_carry_the_default(self):
        # llm_handler and llm/__init__ re-export these; anything importing them
        # directly must keep getting a complete, doctrine-bearing prompt.
        for prompt in (self.prompts.TRIAGE_SYSTEM_PROMPT,
                       self.prompts.ANALYSIS_SYSTEM_PROMPT,
                       self.prompts.DEEP_GATEKEEPER_SYSTEM_PROMPT):
            self.assertIn("CANDIDATE POSITIONING", prompt)


class LaneBriefTests(unittest.TestCase):
    """What the lane is hunting has to be visible to the model scoring it."""

    def setUp(self):
        import llm.prompts as prompts
        self.brief = prompts.lane_brief

    def test_the_lane_targets_are_rendered(self):
        rendered = self.brief(LANE)
        self.assertIn("Technical Officer", rendered)
        self.assertIn("Target seniority: mid", rendered)
        self.assertIn("university technical officer", rendered)

    def test_the_brief_outranks_the_doctrine_for_level_judgements(self):
        rendered = self.brief(LANE)
        self.assertIn("THIS BRIEF WINS", rendered)
        self.assertIn("retired-track", rendered)

    def test_a_lane_that_states_nothing_adds_nothing_to_the_prompt(self):
        self.assertEqual(self.brief({}), "")
        self.assertEqual(self.brief(None), "")
        self.assertEqual(self.brief({"lane_intent": "  ", "seniority": ""}), "")


class LaneTitleMatchTests(unittest.TestCase):
    """The rescue has to recognise the lane's own roles by their advertised titles."""

    def setUp(self):
        import llm.analysis as analysis
        self.matches = lambda title: analysis._title_matches_lane(title, LANE_TARGET_TEXT)

    def test_a_one_word_title_matching_the_lane_is_recognised(self):
        # The case that broke it: "Technician" shares no exact token with
        # "Technical Officer", and a single-word title can never produce two
        # overlaps however the matching works.
        self.assertTrue(self.matches("Technician"))

    def test_ordinary_word_forms_of_a_target_term_count(self):
        for title in ("Senior Technician", "Teacher Electrical",
                      "Senior Laboratory Technician - Structures Cluster",
                      "Research Engineer FPGA and Embedded Communications"):
            self.assertTrue(self.matches(title), title)

    def test_off_lane_titles_are_not_rescued(self):
        for title in ("Accounts Payable Officer", "Sales Executive", "Registered Nurse",
                      "Chief Financial Officer", "Warehouse Storeperson"):
            self.assertFalse(self.matches(title), title)

    def test_one_shared_word_is_not_enough_for_a_longer_title(self):
        # "Officer" alone should not drag an unrelated role into full analysis.
        self.assertFalse(self.matches("Compliance Officer Workplace Relations"))

    def test_an_empty_title_matches_nothing(self):
        for title in ("", None, "  -  "):
            self.assertFalse(self.matches(title))


class RescueReachabilityTests(unittest.TestCase):
    """The rescue must reach the scores it was written to second-guess."""

    def setUp(self):
        import llm.analysis as analysis
        self.analysis = analysis

    def _rescued(self, score, title, flags=None):
        return (
            score >= self.analysis.TRIAGE_RESCUE_FLOOR
            and not self.analysis._has_hard_knockout(flags)
            and self.analysis._title_matches_lane(title, LANE_TARGET_TEXT)
        )

    def test_the_floor_sits_below_the_keep_threshold(self):
        # The retired-track cap lands at 40 with keep=false. A rescue gated on
        # the keep floor could never fire for it, which is what happened.
        from llm.prompts import TRIAGE_KEEP_THRESHOLD
        self.assertLess(self.analysis.TRIAGE_RESCUE_FLOOR, TRIAGE_KEEP_THRESHOLD)
        self.assertLess(self.analysis.TRIAGE_RESCUE_FLOOR, db.AUTO_REJECT_THRESHOLD)

    def test_an_on_lane_role_capped_by_the_retired_track_is_rescued(self):
        self.assertTrue(self._rescued(42, "Technician"))
        self.assertTrue(self._rescued(40, "Technician"))

    def test_a_genuine_no_is_left_alone(self):
        self.assertFalse(self._rescued(12, "Technician"))

    def test_a_stated_credential_gate_still_stands(self):
        # Level is a matter of strategy; a mandatory registration the resume
        # cannot evidence is not, and no lane brief makes it eligible.
        flags = {"flags": [{"type": "credential_gate", "confidence": "high",
                            "requirement": "Current AHPRA registration is mandatory."}]}
        self.assertFalse(self._rescued(42, "Technician", flags))

    def test_a_low_confidence_gate_or_a_level_flag_does_not_block_the_rescue(self):
        soft = {"flags": [{"type": "credential_gate", "confidence": "low", "requirement": "Degree preferred."}]}
        level = {"flags": [{"type": "seniority_below", "confidence": "high", "requirement": "UoM 6 band."}]}
        self.assertTrue(self._rescued(42, "Technician", soft))
        self.assertTrue(self._rescued(42, "Technician", level))

    def test_missing_flags_are_not_a_knockout(self):
        for flags in (None, {}, {"flags": []}):
            self.assertFalse(self.analysis._has_hard_knockout(flags))


class LaneReachesTheScoringPromptTests(unittest.TestCase):
    """End to end: the lane's brief and doctrine reach the model, in one call."""

    def setUp(self):
        db_setup.setup_database()
        import llm.analysis as analysis
        self.analysis = analysis
        self.calls = []

        lane = dict(LANE, positioning_doctrine="TECHNICAL LANE VIEW: mid-level university "
                                               "technical officer roles are the target.")
        original_settings = analysis.db.get_lane_settings
        analysis.db.get_lane_settings = lambda _lane_id: lane
        self.addCleanup(setattr, analysis.db, "get_lane_settings", original_settings)

        original_call = analysis._call_scoring_ai

        def stub(messages=None, **_kwargs):
            self.calls.append(messages or [])
            return ('{"match_score": 58, "reason": "On-lane technical officer role.", "keep": true,'
                    ' "flags": [], "seniority_direction": "aligned", "flag_summary": "Nothing stood out."}')

        analysis._call_scoring_ai = stub
        self.addCleanup(setattr, analysis, "_call_scoring_ai", original_call)

    def _triage(self):
        return self.analysis._triage_job("SENIORITY CEILING: mid-level technical specialist.",
                                         TECHNICIAN_AD, "Technician", 7, lambda _message: None)

    def test_the_lane_brief_reaches_the_triage_prompt(self):
        self._triage()
        user_prompt = self.calls[0][1]["content"]
        self.assertIn("ACTIVE LANE BRIEF", user_prompt)
        self.assertIn("Technical Officer", user_prompt)
        self.assertIn("THIS BRIEF WINS", user_prompt)

    def test_the_lane_doctrine_replaces_the_default_in_the_system_prompt(self):
        self._triage()
        system_prompt = self.calls[0][0]["content"]
        self.assertIn("TECHNICAL LANE VIEW", system_prompt)
        self.assertNotIn("TRACK 1 — PRIMARY", system_prompt)

    def test_the_retired_track_caps_defer_to_the_lane(self):
        self._triage()
        system_prompt = self.calls[0][0]["content"]
        self.assertIn("LANE CHECK", system_prompt)

    def test_flagging_still_costs_no_extra_call(self):
        self._triage()
        self.assertEqual(len(self.calls), 1)


class LaneDoctrinePersistenceTests(unittest.TestCase):
    """The override is a document; storing it must not flatten it."""

    def setUp(self):
        db_setup.setup_database()
        # add_profile returns a bool, not an id; look the row back up by name.
        db.add_profile("Doctrine persistence lane", "resume.docx")
        self.lane_id = db.get_profile_by_name("Doctrine persistence lane")["id"]
        self.addCleanup(db.delete_profile, self.lane_id)

    def test_a_multi_line_doctrine_survives_a_round_trip(self):
        doctrine = "TECHNICAL LANE\n\n- Target: technical officer roles.\n- Band: $95k-$115k."
        db.update_lane_settings(self.lane_id, {"positioning_doctrine": doctrine})
        self.assertEqual(db.get_lane_settings(self.lane_id)["positioning_doctrine"], doctrine)

    def test_the_default_is_blank_so_the_global_doctrine_applies(self):
        self.assertEqual(db.get_lane_settings(self.lane_id)["positioning_doctrine"], "")

    def test_saving_other_lane_settings_does_not_clear_it(self):
        db.update_lane_settings(self.lane_id, {"positioning_doctrine": "TECHNICAL LANE"})
        db.update_lane_settings(self.lane_id, {"seniority": "mid"})
        settings = db.get_lane_settings(self.lane_id)
        self.assertEqual(settings["positioning_doctrine"], "TECHNICAL LANE")
        self.assertEqual(settings["seniority"], "mid")


if __name__ == "__main__":
    unittest.main()
