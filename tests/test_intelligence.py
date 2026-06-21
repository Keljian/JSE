"""Integration checks for the local Intelligence workspace."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_DATA = tempfile.mkdtemp(prefix="jse_intelligence_test_")
os.environ["JSE_DATA_DIR"] = TEST_DATA

import db_setup  # noqa: E402
import database_manager as db  # noqa: E402
import llm_handler  # noqa: E402


class IntelligenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_setup.setup_database()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self):
        with db.get_db_connection() as conn:
            conn.execute("DELETE FROM hidden_market_leads")
            conn.execute("DELETE FROM hidden_market_strategies")
            conn.execute("DELETE FROM market_intelligence_snapshots")
            conn.execute("DELETE FROM jobs")
            for index in range(3):
                conn.execute(
                    """
                    INSERT INTO jobs
                        (title, company, advertiser_company, employer_type, location, url,
                         description, source, profile_id, match_score, salary, date_scraped, updated_at)
                    VALUES (?, 'Hays', 'Hays', 'recruiter', 'Melbourne VIC', ?, ?,
                            'Seek', 1, 75, '$160,000', datetime('now'), datetime('now'))
                    """,
                    (
                        f"Senior Technology Manager {index}",
                        f"https://example.test/jobs/{index}",
                        "Our client needs stakeholder management, vendor management, cloud leadership and hybrid work. Contact our recruitment consultant.",
                    ),
                )
            conn.commit()

    def test_ranked_targets_retain_evidence_and_market_signals(self):
        intelligence = db.get_hidden_market_intel(1, False, 60)
        target = intelligence["recruiters"][0]
        self.assertEqual(3, len(target["evidence"]))
        self.assertGreater(target["opportunity_score"], 0)
        self.assertEqual("high", target["confidence"])
        self.assertTrue(target["classification_reasons"])
        self.assertTrue(intelligence["signals"]["skills"])
        self.assertTrue(intelligence["signals"]["salary_bands"])
        self.assertTrue(intelligence["snapshot_history"])

    def test_saved_strategy_flows_into_tracked_lead_and_outcomes(self):
        db.save_hidden_market_strategy(
            1,
            "recruiter",
            "Hays",
            {"positioning_angle": "Reference recent mandates", "recommended_channel": "email"},
        )
        lead = db.add_hidden_market_lead(
            1,
            "recruiter",
            "Hays",
            opportunity_score=78,
            score_reasons=["Strong recurring demand"],
        )
        self.assertEqual("email", lead["outreach_channel"])
        self.assertEqual("Reference recent mandates", lead["strategy"]["positioning_angle"])
        db.update_hidden_market_lead(lead["id"], {"status": "done", "outcome": "converted"})
        performance = db.get_hidden_market_stats(1, False, 30)
        self.assertEqual(1, performance["funnel"]["converted"])
        self.assertTrue(performance["type_performance"])
        self.assertTrue(performance["channel_performance"])
        self.assertTrue(performance["score_calibration"])

    def test_ai_strategy_is_structured_and_evidence_bound(self):
        original = llm_handler._call_unsloth
        llm_handler._call_unsloth = lambda *args, **kwargs: (
            '{"positioning_angle":"Reference the recurring mandates",'
            '"contact_persona":"Technology recruitment consultant",'
            '"recommended_channel":"email","opening_message":"Hello",'
            '"evidence_to_reference":["Three recent roles"],'
            '"questions_to_ask":["Any adjacent mandates?"],'
            '"follow_up_sequence":["Follow up in five days"],'
            '"cautions":["Do not imply an exclusive mandate"]}'
        )
        try:
            strategy = llm_handler.hidden_market_strategy(
                {"target_type": "recruiter", "name": "Hays", "sample_titles": ["Technology Manager"]},
                lane_context="Senior technology leadership",
            )
        finally:
            llm_handler._call_unsloth = original
        self.assertEqual("email", strategy["recommended_channel"])
        self.assertTrue(strategy["evidence_to_reference"])
        self.assertTrue(strategy["cautions"])


if __name__ == "__main__":
    unittest.main()
