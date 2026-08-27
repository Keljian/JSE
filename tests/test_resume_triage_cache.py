"""The compact resume summary is built once and read by every triage.

That makes it the highest-leverage single LLM call in a sweep: 553 jobs were
scored against one 1000-token generation that the local endpoint truncated.
Free-text calls do not raise on truncation, so nothing noticed — and the
wreckage was written to the profile's cache, where it would have been served
to every later sweep until the resume itself changed.

The properties guarded here:

- Reasoning is turned off for the call, so the budget buys the answer.
- A truncated or unlabelled summary is never cached.
- It is retried once, then falls back to the resume itself rather than
  triaging against nothing.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import llm_handler  # noqa: E402
import llm.analysis as analysis  # noqa: E402


GOOD_SUMMARY = "\n".join([
    "TARGET ROLE FAMILIES: Senior IT leadership, Business systems, Delivery / project",
    "SENIORITY CEILING: Senior manager / head-of, but not C-suite.",
    "STRONGEST SKILLS: ERP programme delivery, vendor and contract management, "
    "service transition, team leadership, cyber uplift, budget ownership.",
    "DOMAIN STRENGTHS: Utilities (six years), local government, higher education.",
    "TRANSFERABLE ADJACENT ROLES: Programme manager, IT operations manager, "
    "business systems manager, transformation lead.",
    "CLEAR NON-FIT FAMILIES: Pure software engineering, clinical, sales / BD.",
    "RECENT ANCHORS: Led the SAP rollout at a regional water utility; rebuilt the "
    "service desk at a metropolitan council.",
])

# What the incident actually produced: the labels stop part-way through.
TRUNCATED_SUMMARY = GOOD_SUMMARY.split("TRANSFERABLE ADJACENT ROLES:")[0] + "TRANSFERABLE ADJ"


class ResumeTriageSummaryTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.logged = []
        patches = [
            mock.patch.object(analysis.db, "get_resume_triage_cache", return_value=None),
            mock.patch.object(analysis.db, "save_resume_triage_cache",
                              side_effect=lambda *args: self.saved.append(args)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, responses):
        replies = list(responses)
        calls = []

        def stub(messages=None, **kwargs):
            calls.append(kwargs)
            return replies.pop(0) if replies else ""

        original = analysis._call_scoring_ai
        try:
            llm_handler._call_scoring_ai = stub
            summary = analysis._get_resume_triage_summary(
                "RESUME TEXT", 1, self.logged.append,
            )
        finally:
            llm_handler._call_scoring_ai = original
        return summary, calls

    def test_the_call_asks_for_the_answer_not_the_reasoning(self):
        _summary, calls = self._run([GOOD_SUMMARY])
        self.assertTrue(calls[0]["no_reasoning"],
                        "thinking eats the budget before the summary is written")

    def test_a_good_summary_is_returned_and_cached(self):
        summary, calls = self._run([GOOD_SUMMARY])
        self.assertEqual(summary, GOOD_SUMMARY)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.saved), 1)

    def test_a_truncated_summary_is_retried_not_cached(self):
        summary, calls = self._run([TRUNCATED_SUMMARY, GOOD_SUMMARY])
        self.assertEqual(summary, GOOD_SUMMARY)
        self.assertEqual(len(calls), 2, "one bad generation should not decide the sweep")
        self.assertEqual([args[2] for args in self.saved], [GOOD_SUMMARY])

    def test_an_empty_response_falls_back_to_the_resume(self):
        summary, _calls = self._run(["", ""])
        self.assertIn("RESUME TEXT", summary)
        self.assertEqual(self.saved, [], "a fallback must not poison the cache")
        self.assertTrue(any("resume extract" in message.lower() for message in self.logged),
                        "the operator has to be told the sweep is triaging on less")

    def test_a_cached_summary_costs_no_call(self):
        with mock.patch.object(analysis.db, "get_resume_triage_cache", return_value=GOOD_SUMMARY):
            summary, calls = self._run([GOOD_SUMMARY])
        self.assertEqual(summary, GOOD_SUMMARY)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
