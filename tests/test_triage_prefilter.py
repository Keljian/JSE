"""The learned pre-triage filter: training guards, the audit sample, and the
analysis-loop wiring that must never drop a job without a reason."""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import triage_prefilter as tp  # noqa: E402


def _corpus(n=1200, seed=7, noise_keeper_titles=()):
    """Ads whose score is decided by their title family, like the real lanes."""
    rnd = random.Random(seed)
    rows = []
    for job_id in range(1, n + 1):
        roll = rnd.random()
        if roll < 0.45:
            title, body, score = "Registered Nurse", "Ward patient care clinical shifts ahpra", rnd.randint(5, 30)
        elif roll < 0.75:
            title, body, score = "Project Coordinator", "Coordinate projects reporting stakeholders", rnd.randint(40, 58)
        else:
            title, body, score = "IT Operations Manager", "Lead infrastructure service delivery vendors", rnd.randint(62, 90)
        rows.append((job_id, title, body, score))
    for offset, title in enumerate(noise_keeper_titles):
        rows.append((n + 1 + offset, title, "Ward patient care clinical shifts ahpra", 85))
    return rows


class TrainingGuardTests(unittest.TestCase):
    def test_a_clean_corpus_trains_a_model_that_skips_only_rejects(self):
        model = tp.train(_corpus())
        self.assertTrue(model.enabled, model.reason)
        self.assertEqual(0, model.stats["keepers_lost"])
        self.assertGreaterEqual(model.stats["skip_precision"], tp.MIN_SKIP_PRECISION)
        nurse = model.decide(5000, "Registered Nurse", "Ward patient care clinical shifts ahpra")
        manager = model.decide(5001, "IT Operations Manager", "Lead infrastructure service delivery vendors")
        self.assertIsNotNone(nurse)
        self.assertIsNone(manager)

    def test_too_little_history_leaves_the_lane_unfiltered(self):
        model = tp.train(_corpus(n=100))
        self.assertFalse(model.enabled)
        self.assertIn("not enough", model.reason)
        self.assertIsNone(model.decide(1, "Registered Nurse", "ward"))

    def test_a_protected_score_inside_the_reject_family_blocks_the_cut(self):
        # High scorers that look exactly like rejects: no safe cut-off exists.
        noisy = [f"Registered Nurse {i}" for i in range(60)]
        model = tp.train(_corpus(noise_keeper_titles=noisy))
        if model.enabled:
            self.assertEqual(0, model.stats["keepers_lost"])
        else:
            self.assertTrue(model.reason)


class AuditTests(unittest.TestCase):
    def test_audit_selection_is_stable_and_near_the_target_share(self):
        picks = [tp.is_audit(i) for i in range(20000)]
        self.assertEqual(picks, [tp.is_audit(i) for i in range(20000)])
        share = sum(picks) / len(picks)
        self.assertTrue(0.035 < share < 0.065, share)

    def test_an_audited_reject_is_analysed_not_skipped(self):
        model = tp.train(_corpus())
        audited = next(i for i in range(10000, 20000) if tp.is_audit(i))
        decision = model.decide(audited, "Registered Nurse", "Ward patient care clinical shifts ahpra")
        self.assertEqual("audit", decision["verdict"])
        self.assertIn("Prefilter", decision["reason"])


if __name__ == "__main__":
    unittest.main()
