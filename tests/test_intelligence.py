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
import contact_research  # noqa: E402


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
            conn.execute("DELETE FROM hidden_market_contact_research")
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
        captured = {}
        def fake_call(messages, *args, **kwargs):
            captured["prompt"] = str(messages)
            return ('{"positioning_angle":"Reference the recurring mandates",'
                    '"contact_persona":"Technology recruitment consultant",'
                    '"recommended_channel":"email","opening_message":"Hello",'
                    '"evidence_to_reference":["Three recent roles"],'
                    '"questions_to_ask":["Any adjacent mandates?"],'
                    '"follow_up_sequence":["Follow up in five days"],'
                    '"cautions":["Do not imply an exclusive mandate"]}')
        llm_handler._call_unsloth = fake_call
        try:
            strategy = llm_handler.hidden_market_strategy(
                {"target_type": "recruiter", "name": "Hays", "sample_titles": ["Technology Manager"]},
                lane_context="Senior technology leadership",
                contact_research={"selected_candidate_id": "sean", "candidates": [{"candidate_id": "sean", "name": "Sean Mantri", "role": "Technology Recruitment Consultant", "confidence": "high", "confidence_score": 82, "sources": [{"url": "https://example.test/sean"}]}]},
            )
        finally:
            llm_handler._call_unsloth = original
        self.assertEqual("email", strategy["recommended_channel"])
        self.assertTrue(strategy["evidence_to_reference"])
        self.assertTrue(strategy["cautions"])
        self.assertIn("Sean Mantri", captured["prompt"])

    def test_contact_extraction_pairs_details_by_proximity_and_rejects_prose(self):
        records = db.extract_contact_records("""
            For more information about the role
            contact Sean Mantri on 0420 425 141
            josh.dmonte@talentinternational.com

            centre technologies with
            liam.lasslett@talentinternational.com
        """)
        names = {item["name"] for item in records}
        self.assertIn("Josh Dmonte", names)
        self.assertIn("Liam Lasslett", names)
        self.assertNotIn("About The Role", names)
        self.assertNotIn("Centre Technologies", names)

    def test_scraped_job_persists_structured_contact_records(self):
        added = db.add_job({
            "title": "Contact Record Test",
            "company": "Example Recruitment",
            "location": "Melbourne VIC",
            "url": "https://example.test/contact-record-test",
            "description": "For further information contact Jane Citizen\njane.citizen@example.com\n0400 111 222",
        }, "Seek", profile_id=1)
        self.assertTrue(added)
        with db.get_db_connection() as conn:
            row = conn.execute("SELECT contact_person, contact_records_json FROM jobs WHERE url = ?", ("https://example.test/contact-record-test",)).fetchone()
        self.assertEqual("Jane Citizen", row["contact_person"])
        self.assertIn("jane.citizen@example.com", row["contact_records_json"])

    def test_contact_research_ignores_noisy_labels_and_limits_visible_choices(self):
        target = {
            "name": "Talent International",
            "target_type": "recruiter",
            "target_key": "recruiter:domain:talentinternational.com",
            "evidence": [{
                "title": "Agile Lead",
                "url": "https://jobs.example/agile-lead",
                "contacts": [
                    {"name": "Sean Mantri on", "email": "josh.dmonte@talentinternational.com", "phone": "0420 425 141"},
                    {"name": "centre technologies", "email": "liam.lasslett@talentinternational.com"},
                    {"name": "about the role", "email": "chris.mackay@talentinternational.com"},
                    {"name": "Anita Fonseka on", "email": "anita.fonseka@talentinternational.com"},
                ],
            }],
        }

        def fake_search(query, limit=5):
            if "Josh Dmonte" in query:
                return [{"url": "https://www.talentinternational.com/team/josh-dmonte", "title": "Josh Dmonte - Talent International", "snippet": "Recruitment consultant", "source_type": "Public web result"}]
            return []

        research = contact_research.research_target_contacts(target, search_func=fake_search)
        names = {candidate["name"] for candidate in research["candidates"]}
        self.assertIn("Josh Dmonte", names)
        self.assertIn("Liam Lasslett", names)
        self.assertNotIn("Centre Technologies", names)
        self.assertNotIn("About The Role", names)
        self.assertFalse(research["conflicts"])
        self.assertLessEqual(len(research["visible_candidate_ids"]), 3)
        self.assertGreaterEqual(research["discarded_labels_count"], 3)
        saved = db.save_hidden_market_contact_research(1, target["target_type"], target["target_key"], target["name"], research)
        chosen = next(candidate for candidate in research["candidates"] if candidate["name"] == "Josh Dmonte")
        selected = db.select_hidden_market_contact(1, target["target_type"], target["target_key"], chosen["candidate_id"])
        self.assertEqual(chosen["candidate_id"], selected["research"]["selected_candidate_id"])
        self.assertFalse(selected["research"]["requires_selection"])
        self.assertTrue(saved["research"]["candidates"])


if __name__ == "__main__":
    unittest.main()
