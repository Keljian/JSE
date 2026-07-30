"""Regression checks for channel warmth as a first-class job dimension.

Channel warmth answers "how does this application reach the employer", which is
a different question from "how well does the resume match". It is only useful if
it survives three trips: derivation (including an explicit override), ranking
(warmth outranks the score), and the dashboard mix nudge.
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


class _WarmthDbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_warmth_test_")
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
            for table in ("application_events", "application_outcomes", "warm_contacts",
                          "lane_opportunities", "job_postings", "jobs"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def _add(self, title, company, url, source="Seek", **columns):
        db.add_job(
            {
                "title": title,
                "company": company,
                "location": "Melbourne VIC",
                "url": url,
                "description": f"{title} at {company}.",
            },
            source,
            1,
        )
        with db.get_db_connection() as conn:
            job_id = conn.execute(
                "SELECT id FROM jobs WHERE url = ?", (db.normalize_job_url(url),)
            ).fetchone()["id"]
            if columns:
                assignments = ", ".join(f"{key} = ?" for key in columns)
                conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                             [*columns.values(), job_id])
            conn.commit()
        return job_id


class ChannelDerivationTests(_WarmthDbTestCase):
    def test_stored_channel_overrides_derivation(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/a")
        self.assertEqual(db.application_channel(dict(db.get_job_details(job_id))), db.CHANNEL_BOARD)
        db.set_job_channel(job_id, db.CHANNEL_WARM_REFERRAL)
        self.assertEqual(db.application_channel(dict(db.get_job_details(job_id))), db.CHANNEL_WARM_REFERRAL)

    def test_clearing_the_channel_returns_to_derived(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/b")
        db.set_job_channel(job_id, db.CHANNEL_WARM_REFERRAL)
        db.set_job_channel(job_id, "")
        self.assertIsNone(db.get_job_details(job_id)["channel"])
        self.assertEqual(db.application_channel(dict(db.get_job_details(job_id))), db.CHANNEL_BOARD)

    def test_unknown_channel_is_rejected(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/c")
        with self.assertRaises(ValueError):
            db.set_job_channel(job_id, "carrier_pigeon")

    def test_recruiter_and_hidden_market_derivation_still_apply(self):
        recruiter = self._add("IT Manager", "Agency", "https://example.com/d", employer_type="recruiter")
        hidden = self._add("IT Manager", "Target", "https://example.com/e", source=db.HIDDEN_MARKET_SOURCE)
        self.assertEqual(db.application_channel(dict(db.get_job_details(recruiter))), db.CHANNEL_RECRUITER)
        self.assertEqual(db.application_channel(dict(db.get_job_details(hidden))), db.CHANNEL_DIRECT_OUTREACH)


class WarmthRankTests(_WarmthDbTestCase):
    def test_warm_channels_rank_highest(self):
        for channel in db.WARM_CHANNELS:
            self.assertEqual(db.channel_warmth({"channel": channel}), db.WARMTH_WARM)

    def test_named_contact_on_the_job_counts_as_named(self):
        self.assertEqual(db.channel_warmth({"source": "Seek", "contact_person": "Dana Lee"}), db.WARMTH_NAMED)
        self.assertEqual(db.channel_warmth({"source": "Seek", "contact_email": "dana@example.com"}), db.WARMTH_NAMED)

    def test_a_known_warm_path_promotes_a_cold_job_to_named(self):
        job = {"source": "Seek", "company": "Flavorite"}
        self.assertEqual(db.channel_warmth(job), db.WARMTH_COLD)
        self.assertEqual(db.channel_warmth(job, warm_path=[{"name": "Dana Lee"}]), db.WARMTH_NAMED)

    def test_plain_board_job_is_cold(self):
        self.assertEqual(db.channel_warmth({"source": "Seek", "company": "Coldco"}), db.WARMTH_COLD)


class WarmPathMatchingTests(_WarmthDbTestCase):
    def test_index_matches_on_the_real_employer_behind_a_recruiter_ad(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite Tomatoes", role_title="COO", profile_id=1)
        index = db.warm_contact_index(1)
        job = {"company": "Confidential", "actual_company": "Flavorite Tomatoes", "advertiser_company": "Agency"}
        path = db.warm_path_for_job(job, index)
        self.assertEqual(len(path), 1)
        self.assertEqual(path[0]["name"], "Dana Lee")

    def test_matching_is_punctuation_and_case_insensitive(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite Tomatoes", profile_id=1)
        index = db.warm_contact_index(1)
        self.assertTrue(db.warm_path_for_job({"company": "FLAVORITE  TOMATOES!"}, index))

    def test_no_contacts_means_no_path(self):
        self.assertEqual(db.warm_path_for_job({"company": "Coldco"}, db.warm_contact_index(1)), [])
        self.assertEqual(db.warm_path_for_job({"company": "Coldco"}, None), [])

    def test_annotation_marks_stored_versus_derived(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/f")
        jobs = [dict(db.get_job_details(job_id))]
        db.annotate_channel_warmth(jobs, db.warm_contact_index(1))
        self.assertEqual(jobs[0]["channel_source"], "derived")
        self.assertEqual(jobs[0]["channel"], db.CHANNEL_BOARD)
        self.assertEqual(jobs[0]["warmth"], db.WARMTH_COLD)

        db.set_job_channel(job_id, db.CHANNEL_WARM_REFERRAL)
        jobs = [dict(db.get_job_details(job_id))]
        db.annotate_channel_warmth(jobs, db.warm_contact_index(1))
        self.assertEqual(jobs[0]["channel_source"], "stored")
        self.assertEqual(jobs[0]["warmth"], db.WARMTH_WARM)
        self.assertEqual(jobs[0]["warmth_label"], "Warm")

    def test_annotation_carries_the_named_path(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite", role_title="COO", profile_id=1)
        job_id = self._add("IT Manager", "Flavorite", "https://example.com/g")
        jobs = [dict(db.get_job_details(job_id))]
        db.annotate_channel_warmth(jobs, db.warm_contact_index(1))
        self.assertEqual(jobs[0]["warmth"], db.WARMTH_NAMED)
        self.assertEqual(jobs[0]["warm_path"][0]["name"], "Dana Lee")
        self.assertEqual(jobs[0]["warm_path"][0]["role_title"], "COO")


class WarmthRankingTests(_WarmthDbTestCase):
    def test_warmth_outranks_the_campaign_score(self):
        # The whole point of the dimension: a moderate warm role beats a
        # stronger cold one, because the cold one is judged side by side
        # against better-matched candidates and the warm one is not.
        ranked = db._sort_campaign_candidates([
            {"id": 1, "campaign_score": 85, "warmth": db.WARMTH_COLD},
            {"id": 2, "campaign_score": 70, "warmth": db.WARMTH_WARM},
            {"id": 3, "campaign_score": 78, "warmth": db.WARMTH_NAMED},
        ])
        self.assertEqual([job["id"] for job in ranked], [2, 3, 1])

    def test_score_still_orders_within_a_warmth_tier(self):
        ranked = db._sort_campaign_candidates([
            {"id": 1, "campaign_score": 60, "warmth": db.WARMTH_WARM},
            {"id": 2, "campaign_score": 90, "warmth": db.WARMTH_WARM},
        ])
        self.assertEqual([job["id"] for job in ranked], [2, 1])

    def test_missing_warmth_is_treated_as_cold(self):
        ranked = db._sort_campaign_candidates([
            {"id": 1, "campaign_score": 60},
            {"id": 2, "campaign_score": 60, "warmth": db.WARMTH_WARM},
        ])
        self.assertEqual([job["id"] for job in ranked], [2, 1])

    def test_campaign_scoring_annotates_warmth(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite", profile_id=1)
        job_id = self._add("IT Manager", "Flavorite", "https://example.com/h")
        scored = db.score_campaign_job(db.get_job_details(job_id), db.warm_contact_index(1))
        self.assertEqual(scored["warmth"], db.WARMTH_NAMED)
        self.assertTrue(scored["warm_path"])


class ChannelMixTests(_WarmthDbTestCase):
    def _apply(self, job_id, channel=None, days_ago=1):
        from datetime import datetime, timedelta
        applied = (datetime.now() - timedelta(days=days_ago)).date().isoformat()
        with db.get_db_connection() as conn:
            conn.execute("UPDATE jobs SET application_date = ?, channel = ? WHERE id = ?",
                         (applied, channel, job_id))
            conn.commit()

    def test_all_cold_applications_trip_the_nudge(self):
        for n in range(6):
            self._apply(self._add(f"Role {n}", f"Coldco {n}", f"https://example.com/mix{n}"))
        mix = db.get_channel_mix(1)
        self.assertEqual(mix["applications"], 6)
        self.assertEqual(mix["warm_applications"], 0)
        self.assertEqual(mix["cold_share"], 1.0)
        self.assertTrue(mix["skewed_cold"])

    def test_a_healthy_warm_share_does_not_nudge(self):
        for n in range(4):
            self._apply(self._add(f"Cold {n}", f"Coldco {n}", f"https://example.com/hm{n}"))
        for n in range(2):
            self._apply(self._add(f"Warm {n}", f"Warmco {n}", f"https://example.com/hw{n}"),
                        channel=db.CHANNEL_WARM_REFERRAL)
        mix = db.get_channel_mix(1)
        self.assertEqual(mix["warm_applications"], 2)
        self.assertFalse(mix["skewed_cold"])

    def test_too_little_history_does_not_nudge(self):
        for n in range(3):
            self._apply(self._add(f"Role {n}", f"Coldco {n}", f"https://example.com/few{n}"))
        mix = db.get_channel_mix(1)
        self.assertEqual(mix["applications"], 3)
        self.assertFalse(mix["skewed_cold"], "nudged on too small a sample to mean anything")

    def test_applications_outside_the_window_are_excluded(self):
        self._apply(self._add("Old", "Coldco", "https://example.com/old"), days_ago=90)
        mix = db.get_channel_mix(1, days=30)
        self.assertEqual(mix["applications"], 0)

    def test_untapped_warm_paths_are_surfaced(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite", profile_id=1)
        self._add("IT Manager", "Flavorite", "https://example.com/untapped")
        self._add("IT Manager", "Coldco", "https://example.com/notuntapped")
        mix = db.get_channel_mix(1)
        self.assertEqual(mix["untapped_count"], 1)
        self.assertEqual(mix["untapped_warm_paths"][0]["company"], "Flavorite")
        self.assertEqual(mix["untapped_warm_paths"][0]["contacts"], ["Dana Lee"])

    def test_a_job_already_on_a_warm_channel_is_not_untapped(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite", profile_id=1)
        job_id = self._add("IT Manager", "Flavorite", "https://example.com/already")
        db.set_job_channel(job_id, db.CHANNEL_WARM_REFERRAL)
        self.assertEqual(db.get_channel_mix(1)["untapped_count"], 0)

    def test_dashboard_exposes_the_mix(self):
        self.assertIn("channel_mix", db.get_dashboard(1))


if __name__ == "__main__":
    unittest.main()
