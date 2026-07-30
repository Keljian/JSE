"""Regression checks for the band-weighted targeting strategy.

Covers: seniority-band classification against titles that really produced (or
failed to produce) interviews, per-dimension prior clamping, the 60/40 composite
reweighting against the historical interview set, the near-miss outcome states,
orphaned-snapshot recovery, and channel backfill.

The score fixtures in CompositeReweightingTests are the real (match, fragment)
pairs read from the live database on 2026-07-30, so the regression measures the
actual ranking change rather than a synthetic one.
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


class SeniorityBandTests(unittest.TestCase):
    """Band assignment is tested against labelled real titles, not assumed.

    The bridging list drives a ±25 score prior, so a misclassification is now
    expensive. Every title below is one that actually appeared in
    application_outcomes.
    """

    # Titles that produced interviews. All but "Head of Digital & Technology"
    # are bridging; that one is the exec-band interview and is labelled honestly
    # rather than being forced into bridging to flatter the fixture.
    INTERVIEWED = {
        "Technical Manager": "bridging",
        "Technical Lead": "bridging",
        "IT System Engineer": "bridging",
        "Senior Technical Solution Team Leader": "bridging",
        "Manager - Hybrid Cloud Platforms": "bridging",
        "Vendor Manager (IT Portfolio), RMIT Procurement": "bridging",
        "Business Systems and Data Operations Manager": "bridging",
        "Senior Business Analyst": "bridging",
        "Head of Digital & Technology": "exec",
    }

    # The 1.4%-converting manager-lead bucket and the 0%-converting IC bucket.
    LOW_YIELD = {
        "IT Manager": "manager-lead",
        "Head of ICT": "exec",
        "Infrastructure Manager": "manager-lead",
        "Portfolio Manager": "manager-lead",
        "IT Operations Manager": "manager-lead",
        "Group IT Manager": "manager-lead",
        "Digital Transformation Manager": "manager-lead",
        "General Manager Information Technology": "exec",
        "Network Administrator": "ic",
        "Cloud Engineer": "ic",
        "Data Analyst": "ic",
    }

    def test_interviewed_titles_classify_as_labelled(self):
        for title, expected in self.INTERVIEWED.items():
            with self.subTest(title=title):
                self.assertEqual(expected, db._seniority_band(title))

    def test_low_yield_titles_classify_as_labelled(self):
        for title, expected in self.LOW_YIELD.items():
            with self.subTest(title=title):
                self.assertEqual(expected, db._seniority_band(title))

    def test_documented_edge_cases_are_intentional(self):
        # Both are called out in the _SENIORITY_BRIDGING comment as deliberate.
        # If either flips, the comment is now lying and needs revisiting.
        self.assertEqual("bridging", db._seniority_band("Manager Platform Integration"))
        self.assertEqual("bridging", db._seniority_band("Infrastructure Team Leader"))

    def test_empty_title_is_unknown(self):
        self.assertEqual("unknown", db._seniority_band(""))
        self.assertEqual("unknown", db._seniority_band(None))


class PriorClampingTests(unittest.TestCase):
    """The seniority band gets more authority than the other dimensions, and the
    auto-reject guard survives the widening."""

    def _priors(self, band_rate, band_delta, baseline=0.058):
        return {
            "baseline_rate": baseline,
            "dimensions": {
                "seniority_band": {
                    "bridging": {"support": 28, "rate": band_rate, "delta": band_delta, "clamp": 25},
                },
                "source": {
                    "Seek": {"support": 100, "rate": 0.05, "delta": -1, "clamp": 10},
                },
            },
        }

    def test_seniority_band_clamp_is_wider_than_default(self):
        self.assertEqual(10, db.prior_clamp_for("source"))
        self.assertEqual(10, db.prior_clamp_for("employer_type"))
        self.assertEqual(25, db.prior_clamp_for("seniority_band"))

    def test_derived_band_delta_can_exceed_the_default_clamp(self):
        # A 25% band rate against a 5.8% baseline must be expressible as more
        # than the old flat +/-10; that was the whole point of item 1.
        records = []
        for _ in range(21):
            records.append({"reached_interview": False, "dimensions": {"seniority_band": "manager-lead"}})
        for _ in range(7):
            records.append({"reached_interview": True, "dimensions": {"seniority_band": "bridging"}})
        for _ in range(21):
            records.append({"reached_interview": False, "dimensions": {"seniority_band": "bridging"}})
        baseline = sum(1 for r in records if r["reached_interview"]) / len(records)
        priors = db._derive_conversion_priors(records, baseline)
        bridging = priors["dimensions"]["seniority_band"]["bridging"]
        self.assertGreater(bridging["delta"], db.DEFAULT_PRIOR_CLAMP)
        self.assertLessEqual(bridging["delta"], db.PRIOR_CLAMP_BY_DIMENSION["seniority_band"])

    def test_every_dimension_delta_respects_its_own_clamp(self):
        records = [
            {"reached_interview": i < 20, "dimensions": {
                "seniority_band": "bridging", "source": "Seek", "advertiser": "A",
                "employer_type": "direct_employer", "channel": "board"}}
            for i in range(20)
        ]
        priors = db._derive_conversion_priors(records, 0.0)
        for dim, buckets in priors["dimensions"].items():
            for value, bucket in buckets.items():
                with self.subTest(dimension=dim, value=value):
                    self.assertLessEqual(abs(bucket["delta"]), db.prior_clamp_for(dim))

    def test_weighted_delta_reduces_to_mean_when_clamps_are_equal(self):
        priors = {
            "baseline_rate": 0.05,
            "dimensions": {
                "source": {"Seek": {"support": 10, "rate": 0.3, "delta": 8, "clamp": 10}},
                "employer_type": {"direct_employer": {"support": 10, "rate": 0.1, "delta": 2, "clamp": 10}},
            },
        }
        job = {"title": "Network Administrator", "company": "C", "advertiser_company": "C",
               "employer_type": "direct_employer", "source": "Seek"}
        self.assertEqual(5, db.conversion_prior_delta(job, priors))

    def test_band_authority_is_not_diluted_by_narrower_dimensions(self):
        priors = self._priors(0.25, 19)
        job = {"title": "Technical Lead", "company": "C", "advertiser_company": "C",
               "employer_type": "direct_employer", "source": "Seek"}
        delta = db.conversion_prior_delta(job, priors)
        # Plain mean of (19, -1) would be 9 — the old behaviour, which cannot
        # express the band evidence. Clamp-weighted, the band keeps most of it.
        self.assertGreater(delta, 9)
        self.assertLessEqual(delta, 25)

    def test_wider_clamp_still_cannot_cross_auto_reject_on_its_own(self):
        priors = self._priors(0.25, 25)
        below = {"title": "Technical Lead", "company": "C", "advertiser_company": "C",
                 "employer_type": "direct_employer", "source": "Seek"}
        adjusted = db.composite_score_with_prior(db.AUTO_REJECT_THRESHOLD - 5, None, below, priors)
        self.assertLess(adjusted, db.AUTO_REJECT_THRESHOLD,
                        "a +25 band prior must not rescue a job across the auto-reject line")

        negative = {
            "baseline_rate": 0.058,
            "dimensions": {"seniority_band": {"ic": {"support": 27, "rate": 0.0, "delta": -25, "clamp": 25}}},
        }
        above = {"title": "Network Administrator", "company": "C", "advertiser_company": "C",
                 "employer_type": "direct_employer", "source": "Seek"}
        adjusted = db.composite_score_with_prior(db.AUTO_REJECT_THRESHOLD + 2, None, above, negative)
        self.assertGreaterEqual(adjusted, db.AUTO_REJECT_THRESHOLD,
                                "a -25 band prior must not condemn a job across the auto-reject line")

    def test_band_alone_never_rejects(self):
        """The low-yield flag is advisory; it must not be an auto-reject."""
        priors = {
            "baseline_rate": 0.058,
            "dimensions": {"seniority_band": {
                "manager-lead": {"support": 82, "rate": 0.0122, "delta": -25, "clamp": 25}}},
        }
        job = {"title": "IT Manager", "company": "C", "advertiser_company": "C",
               "employer_type": "direct_employer", "source": "Seek"}
        score = db.composite_score_with_prior(90, None, job, priors)
        self.assertGreaterEqual(score, db.AUTO_REJECT_THRESHOLD)

    def test_explanation_names_the_contributing_dimension(self):
        priors = self._priors(0.25, 19)
        job = {"title": "Technical Lead", "company": "C", "advertiser_company": "C",
               "employer_type": "direct_employer", "source": "Seek", "match_score": 70,
               "fragment_score": 80}
        explanation = db.explain_composite_score(job, priors)
        dims = {reason["dimension"] for reason in explanation["reasons"]}
        self.assertIn("seniority_band", dims)
        self.assertEqual("bridging", explanation["seniority_band"])


class CompositeReweightingTests(unittest.TestCase):
    """The historical interview set must not rank worse under 60/40.

    Population and score pairs are the real applied cohort read from the live
    database on 2026-07-30.
    """

    # (job_id, match_score, fragment_score) for the jobs that reached interview.
    # 12354 was never scored and is therefore not rankable; it is excluded here
    # and asserted separately.
    INTERVIEW_SET = [
        (6267, 72, None), (6353, 82, 85), (8610, 88, None), (12344, 76, 85),
        (22508, 82, 85), (28307, 82, 85), (28328, 82, 85),
    ]
    # Real (match, fragment) pairs of applied jobs that did NOT reach interview.
    NON_INTERVIEW = [
        (82, None), (82, None), (82, None), (88, None), (88, None), (88, None),
        (88, None), (82, None), (82, None), (82, None), (88, None), (0, None),
        (82, 85), (82, None), (63, None), (82, 72), (72, 65), (72, 72), (76, 72),
        (72, 65), (76, 85), (82, 85), (82, 85), (82, 75), (82, 85), (72, None),
        (82, None), (82, 85), (82, 85), (82, 85), (82, 85), (80, 85), (82, 85),
        (80, 75), (82, 85), (76, 85), (72, 65), (82, 75), (82, 85), (80, 85),
        (82, 85), (72, None), (76, 85), (82, 85), (72, 75), (72, 85), (72, None),
        (82, 85), (82, 85), (74, 85), (72, 65), (82, 85), (82, None), (76, 85),
        (82, 85), (80, 75), (72, 85), (74, 85), (82, 85), (72, 85), (76, 85),
        (76, 85), (76, 85), (82, 75), (35, None), (76, 85), (82, 85), (72, 65),
        (82, 85), (82, 85), (72, 85), (82, 85), (82, 85), (72, 65), (82, 85),
        (82, 85), (74, 85), (82, 85), (74, 85), (72, 85), (72, 75), (82, 85),
        (82, 85), (82, 92), (82, 85), (82, 65), (82, 85), (82, 85), (72, 85),
        (82, 85), (82, 85), (82, 85), (82, 75), (82, 65), (76, 85), (82, 85),
        (82, 85), (82, 85), (72, 85), (74, 75), (82, 85), (82, 85), (82, 92),
        (82, 85), (82, 85), (82, 85), (82, 85),
    ]

    @staticmethod
    def _blend(match, fragment, match_weight, fragment_weight):
        if match is None:
            return None
        if fragment is None:
            return int(round(float(match)))
        return int(round(match_weight * float(match) + fragment_weight * float(fragment)))

    def _ranks(self, match_weight, fragment_weight):
        scores = [self._blend(m, f, match_weight, fragment_weight) for m, f in self.NON_INTERVIEW]
        scores += [self._blend(m, f, match_weight, fragment_weight) for _, m, f in self.INTERVIEW_SET]
        # Competition rank: 1 + how many score strictly higher. Stable under
        # ties, unlike index-in-a-sorted-list.
        return {
            job_id: 1 + sum(1 for s in scores if s > self._blend(m, f, match_weight, fragment_weight))
            for job_id, m, f in self.INTERVIEW_SET
        }

    def test_weights_are_named_constants_summing_to_one(self):
        self.assertAlmostEqual(
            1.0, db.COMPOSITE_MATCH_WEIGHT + db.COMPOSITE_FRAGMENT_WEIGHT, places=6)
        self.assertLessEqual(db.COMPOSITE_MATCH_WEIGHT, 0.60,
                             "item 2 caps match_score at 60% of the composite")

    def test_calculate_composite_uses_the_named_weights(self):
        self.assertEqual(
            int(round(db.COMPOSITE_MATCH_WEIGHT * 80 + db.COMPOSITE_FRAGMENT_WEIGHT * 90)),
            db.calculate_composite_score(80, 90),
        )

    def test_missing_fragment_score_falls_back_to_match(self):
        self.assertEqual(77, db.calculate_composite_score(77, None))
        self.assertIsNone(db.calculate_composite_score(None, 90))

    def test_interview_set_does_not_rank_worse_under_new_weighting(self):
        old = self._ranks(0.80, 0.20)
        new = self._ranks(db.COMPOSITE_MATCH_WEIGHT, db.COMPOSITE_FRAGMENT_WEIGHT)
        for job_id, _, _ in self.INTERVIEW_SET:
            with self.subTest(job=job_id):
                self.assertLessEqual(
                    new[job_id], old[job_id],
                    f"job {job_id} fell from rank {old[job_id]} to {new[job_id]}",
                )

    def test_interview_set_composite_scores_do_not_fall(self):
        for job_id, match, fragment in self.INTERVIEW_SET:
            with self.subTest(job=job_id):
                self.assertGreaterEqual(
                    self._blend(match, fragment, db.COMPOSITE_MATCH_WEIGHT, db.COMPOSITE_FRAGMENT_WEIGHT),
                    self._blend(match, fragment, 0.80, 0.20),
                )

    def test_unscored_interview_job_is_not_invented_a_score(self):
        # Job 12354 (Senior Technical Solution Team Leader) reached interview but
        # was never analysed. Scoring must leave it None rather than guessing.
        self.assertIsNone(db.calculate_composite_score(None, None))


class _OutcomeDatabaseTestCase(unittest.TestCase):
    """Shared throwaway-database harness for the migration-level tests."""

    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_targeting_test_")
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
            for table in (
                "application_outcomes", "interviews", "application_events",
                "lane_opportunities", "job_postings", "jobs", "warm_contacts",
                "hidden_market_leads",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "DELETE FROM app_settings WHERE key IN (?, ?, ?, ?, ?)",
                (db.FUNNEL_CONVERSION_PRIORS_KEY, db.FUNNEL_INSIGHTS_CACHE_KEY,
                 db._OUTCOME_BACKFILL_FLAG, db._ORPHAN_REPAIR_FLAG, db._CHANNEL_BACKFILL_FLAG),
            )
            conn.commit()

    def _make_applied_job(self, title, company, url, source="Seek", employer_type=None):
        db.add_job(
            {"title": title, "company": company, "location": "Melbourne VIC",
             "url": url, "description": f"{title} at {company}. " * 10},
            source, 1,
        )
        with db.get_db_connection() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE url = ?", (db.normalize_job_url(url),)
            ).fetchone()
            job_id = row["id"] if row else None
            if job_id and employer_type:
                conn.execute("UPDATE jobs SET employer_type = ? WHERE id = ?", (employer_type, job_id))
                conn.commit()
        db.update_job_application(job_id, {"pipeline_stage": "applied", "application_date": "2026-07-01"})
        return job_id


class NearMissOutcomeTests(_OutcomeDatabaseTestCase):
    """final_round / runner_up must rank above interview and stick."""

    def test_new_states_are_in_the_vocabulary_and_rank_above_interview(self):
        self.assertIn(db.OUTCOME_FINAL_ROUND, db.APPLICATION_OUTCOMES)
        self.assertIn(db.OUTCOME_RUNNER_UP, db.APPLICATION_OUTCOMES)
        self.assertGreater(db._OUTCOME_RANK[db.OUTCOME_FINAL_ROUND], db._OUTCOME_RANK[db.OUTCOME_INTERVIEW])
        self.assertGreater(db._OUTCOME_RANK[db.OUTCOME_RUNNER_UP], db._OUTCOME_RANK[db.OUTCOME_FINAL_ROUND])
        self.assertGreater(db._OUTCOME_RANK[db.OUTCOME_OFFER], db._OUTCOME_RANK[db.OUTCOME_RUNNER_UP])

    def test_near_miss_states_count_as_positive_conversions(self):
        self.assertIn(db.OUTCOME_FINAL_ROUND, db._POSITIVE_OUTCOMES)
        self.assertIn(db.OUTCOME_RUNNER_UP, db._POSITIVE_OUTCOMES)

    def test_runner_up_does_not_regress_to_declined(self):
        job_id = self._make_applied_job("Technical Lead", "Acme", "https://x.test/near-miss")
        db.record_application_outcome_detail(
            job_id, outcome=db.OUTCOME_RUNNER_UP, interview_stage_reached=3,
            loss_reason="Second by a very small margin; competitor had direct sector experience.",
        )
        with db.get_db_connection() as conn:
            db.set_application_outcome(conn, job_id, db.OUTCOME_DECLINED)
            conn.commit()
        outcome = db.get_application_outcome(job_id)
        self.assertEqual(db.OUTCOME_RUNNER_UP, outcome["outcome"])
        self.assertEqual(3, outcome["interview_stage_reached"])
        self.assertIn("small margin", outcome["loss_reason"])

    def test_stage_reached_advances_but_never_regresses(self):
        job_id = self._make_applied_job("Technical Manager", "Beta", "https://x.test/stage")
        db.record_application_outcome_detail(job_id, outcome=db.OUTCOME_INTERVIEW, interview_stage_reached=2)
        db.record_application_outcome_detail(job_id, interview_stage_reached=1)
        self.assertEqual(2, db.get_application_outcome(job_id)["interview_stage_reached"])

    def test_invalid_outcome_and_channel_are_rejected(self):
        job_id = self._make_applied_job("Technical Lead", "Gamma", "https://x.test/invalid")
        with self.assertRaises(ValueError):
            db.record_application_outcome_detail(job_id, outcome="nearly")
        with self.assertRaises(ValueError):
            db.record_application_outcome_detail(job_id, channel="carrier_pigeon")

    def test_funnel_reports_both_conversion_rates(self):
        interviewed = self._make_applied_job("Technical Lead", "Acme", "https://x.test/i1")
        final = self._make_applied_job("Technical Manager", "Beta", "https://x.test/i2")
        self._make_applied_job("Network Administrator", "Gamma", "https://x.test/i3")
        db.record_application_outcome_detail(interviewed, outcome=db.OUTCOME_INTERVIEW)
        db.record_application_outcome_detail(final, outcome=db.OUTCOME_FINAL_ROUND, interview_stage_reached=3)

        insights = db.compute_funnel_insights(store=False)
        self.assertEqual(3, insights["total_applications"])
        self.assertEqual(2, insights["total_interviews"])
        self.assertEqual(1, insights["total_final_rounds"])
        # application -> interview and interview -> final round are separate
        # rates with separate causes; both must be reported.
        self.assertAlmostEqual(2 / 3, insights["baseline_rate"], places=3)
        self.assertAlmostEqual(1 / 2, insights["final_round_rate"], places=3)


class ChannelTests(_OutcomeDatabaseTestCase):
    def test_scraped_applications_default_to_board(self):
        job_id = self._make_applied_job("Technical Lead", "Acme", "https://x.test/c1")
        self.assertEqual(db.CHANNEL_BOARD, db.get_application_outcome(job_id)["channel"])

    def test_recruiter_advertised_roles_are_the_recruiter_channel(self):
        job_id = self._make_applied_job(
            "Technical Lead", "Clicks IT", "https://x.test/c2", employer_type="recruiter")
        self.assertEqual(db.CHANNEL_RECRUITER, db.get_application_outcome(job_id)["channel"])

    def test_manual_source_is_left_unattributed_rather_than_guessed(self):
        # An externally-logged application could be any channel. Guessing would
        # pollute the board-vs-warm comparison the dimension exists to make.
        self.assertEqual("unknown", db.application_channel({"source": db.MANUAL_SOURCE}))
        # "unknown" is a display bucket, never a stored value — the column stays
        # NULL so "not attributed yet" is distinguishable from a real channel.
        self.assertIsNone(db._stored_channel({"source": db.MANUAL_SOURCE}))
        job_id = self._make_applied_job("Technical Lead", "Delta", "https://x.test/c4",
                                        source=db.MANUAL_SOURCE)
        self.assertIsNone(db.get_application_outcome(job_id)["channel"])

    def test_hidden_market_source_is_direct_outreach(self):
        self.assertEqual(
            db.CHANNEL_DIRECT_OUTREACH,
            db.application_channel({"source": db.HIDDEN_MARKET_SOURCE}),
        )

    def test_backfill_sets_board_and_skips_manual(self):
        board = self._make_applied_job("Technical Lead", "Acme", "https://x.test/b1")
        manual = self._make_applied_job("Technical Lead", "Beta", "https://x.test/b2",
                                        source=db.MANUAL_SOURCE)
        with db.get_db_connection() as conn:
            conn.execute("UPDATE application_outcomes SET channel = NULL")
            conn.execute(
                "UPDATE application_outcomes SET snapshot_json = REPLACE(snapshot_json, '\"channel\"', '\"was_channel\"')")
            conn.commit()

        updated = db.backfill_outcome_channels()
        self.assertGreaterEqual(updated, 1)
        self.assertEqual(db.CHANNEL_BOARD, db.get_application_outcome(board)["channel"])
        self.assertIsNone(db.get_application_outcome(manual)["channel"])
        # Flag-gated: a second pass is a no-op.
        self.assertEqual(0, db.backfill_outcome_channels())

    def test_channel_can_be_corrected_by_the_user(self):
        job_id = self._make_applied_job("Technical Lead", "Beta", "https://x.test/c3",
                                        source=db.MANUAL_SOURCE)
        db.record_application_outcome_detail(job_id, channel=db.CHANNEL_WARM_REFERRAL)
        outcome = db.get_application_outcome(job_id)
        self.assertEqual(db.CHANNEL_WARM_REFERRAL, outcome["channel"])
        # Written to the snapshot too, so aggregation agrees whichever it reads.
        self.assertEqual(db.CHANNEL_WARM_REFERRAL, outcome["snapshot"]["channel"])


class OrphanRecoveryTests(_OutcomeDatabaseTestCase):
    """A deleted job's dimensions are recovered, not bucketed as `unknown`."""

    def _orphan_outcome(self, job_id, snapshot=None):
        import json
        with db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO application_outcomes
                    (job_id, role_key, snapshot_json, applied_at, outcome, interview_rounds)
                VALUES (?, ?, ?, '2026-07-01', 'interview', 1)
                """,
                (job_id, f"rk-{job_id}",
                 json.dumps(snapshot or {"orphaned": True, "title": None, "seniority_band": "unknown"})),
            )
            conn.commit()

    def test_recovers_dimensions_from_job_postings(self):
        with db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO job_postings
                    (legacy_job_id, title, company, url, source, advertiser_company, employer_type, location)
                VALUES (99001, 'Technical Lead', 'Clicks IT', 'https://x.test/o1', 'Seek', 'Clicks IT', 'recruiter', 'Melbourne VIC')
                """
            )
            conn.commit()
        self._orphan_outcome(99001)

        self.assertEqual(1, db.repair_orphaned_outcome_snapshots())
        outcome = db.get_application_outcome(99001)
        self.assertEqual("Technical Lead", outcome["snapshot"]["title"])
        self.assertEqual("bridging", outcome["snapshot"]["seniority_band"])
        self.assertEqual("job_postings", outcome["snapshot"]["recovered_from"])
        self.assertEqual(db.CHANNEL_RECRUITER, outcome["channel"])

    def test_recovers_title_and_company_from_events_when_posting_is_gone(self):
        with db.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
                (99002, "prompt", "External LLM prompt saved",
                 r"C:\JSE\applications\IT_System_Engineer_external_llm_prompt.md"),
            )
            # The stored company blob is routinely truncated mid-string, so this
            # is scraped rather than JSON-parsed.
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
                (99002, "company", "Company intelligence updated",
                 '{"advertiser_company": "Chobani", "employer_type": "direct_employer", "evidence": ["truncated'),
            )
            conn.commit()
        self._orphan_outcome(99002)

        self.assertEqual(1, db.repair_orphaned_outcome_snapshots())
        snapshot = db.get_application_outcome(99002)["snapshot"]
        self.assertEqual("IT System Engineer", snapshot["title"])
        self.assertEqual("Chobani", snapshot["company"])
        self.assertEqual("bridging", snapshot["seniority_band"])

    def test_unrecoverable_rows_are_excluded_not_bucketed_as_unknown(self):
        self._orphan_outcome(99003)  # no posting, no events
        recovered = self._make_applied_job("Technical Lead", "Acme", "https://x.test/o3")
        db.repair_orphaned_outcome_snapshots()

        insights = db.compute_funnel_insights(store=False)
        # The application still counts in the headline total...
        self.assertEqual(2, insights["total_applications"])
        # ...but contributes to no dimension, and the gap is reported.
        self.assertEqual(1, insights["excluded_unresolved"])
        band_values = {seg["value"] for seg in insights["dimensions"]["seniority_band"]["segments"]}
        self.assertNotIn("unknown", band_values)
        self.assertIsNotNone(db.get_application_outcome(recovered))

    def test_repair_is_flag_gated_and_idempotent(self):
        with db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO job_postings (legacy_job_id, title, company, url, source)
                VALUES (99004, 'Vendor Manager', 'RMIT', 'https://x.test/o4', 'Seek')
                """
            )
            conn.commit()
        self._orphan_outcome(99004)
        self.assertEqual(1, db.repair_orphaned_outcome_snapshots())
        self.assertEqual(0, db.repair_orphaned_outcome_snapshots())


class WarmChannelTests(_OutcomeDatabaseTestCase):
    def test_warm_contacts_are_separate_from_the_candidate_people_table(self):
        db.upsert_warm_contact("Dana Example", organisation="Acme Co", role_title="Head of IT")
        with db.get_db_connection() as conn:
            people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        # `people` is the candidate's own identity (profiles.person_id ->
        # candidate_fragments). Seeding contacts into it would scope the
        # candidate's memory fragments to strangers.
        self.assertEqual(1, people, "warm contacts must not be written into `people`")
        self.assertEqual(1, len(db.list_warm_contacts()))

    def test_upsert_is_idempotent_and_never_overwrites_a_typed_value(self):
        db.upsert_warm_contact("Dana Example", organisation="Acme Co", email="dana@acme.test")
        db.upsert_warm_contact("Dana Example", organisation="Acme Co", email="wrong@guess.test",
                               phone="0400 000 000", origin="contact_research")
        contacts = db.list_warm_contacts(organisation="Acme Co")
        self.assertEqual(1, len(contacts))
        self.assertEqual("dana@acme.test", contacts[0]["email"])
        self.assertEqual("0400 000 000", contacts[0]["phone"])

    def test_contact_requires_a_name(self):
        with self.assertRaises(ValueError):
            db.upsert_warm_contact("   ", organisation="Acme Co")

    def test_idle_week_is_detectable(self):
        activity = db.get_warm_channel_activity(profile_id=1, days=7)
        self.assertTrue(activity["idle"])
        self.assertEqual(0, activity["total_activity"])

        db.add_hidden_market_lead(1, "employer", "Acme Co", action="Direct approach")
        activity = db.get_warm_channel_activity(profile_id=1, days=7)
        self.assertFalse(activity["idle"])
        self.assertEqual(1, activity["new_leads"])
        self.assertEqual(1, activity["open_leads"])


class TargetingSummaryTests(_OutcomeDatabaseTestCase):
    def test_summary_reports_allocation_by_band_and_channel(self):
        self._make_applied_job("Technical Lead", "Acme", "https://x.test/t1")
        manager = self._make_applied_job("IT Manager", "Beta", "https://x.test/t2")
        self._make_applied_job("IT Operations Manager", "Gamma", "https://x.test/t3")
        db.record_application_outcome_detail(manager, outcome=db.OUTCOME_INTERVIEW)
        db.compute_funnel_insights(store=True)

        summary = db.get_targeting_summary(profile_id=1, days=90)
        self.assertEqual(3, summary["total_applications"])
        self.assertEqual(90, summary["window_days"])
        bands = {row["value"]: row for row in summary["by_band"]}
        self.assertEqual(1, bands["bridging"]["applications"])
        self.assertEqual(2, bands["manager-lead"]["applications"])
        channels = {row["value"]: row for row in summary["by_channel"]}
        self.assertEqual(3, channels[db.CHANNEL_BOARD]["applications"])
        self.assertEqual(0, summary["warm_channel_applications"])

    def test_window_excludes_older_applications(self):
        old_job = self._make_applied_job("Technical Lead", "Acme", "https://x.test/t4")
        with db.get_db_connection() as conn:
            conn.execute("UPDATE application_outcomes SET applied_at = '2020-01-01' WHERE job_id = ?", (old_job,))
            conn.commit()
        self.assertEqual(0, db.get_targeting_summary(profile_id=1, days=90)["total_applications"])

    def test_band_yields_flag_low_yield_without_rejecting(self):
        priors = {
            "baseline_rate": 0.058,
            "dimensions": {"seniority_band": {
                "bridging": {"support": 28, "rate": 0.25, "delta": 19, "clamp": 25},
                "manager-lead": {"support": 82, "rate": 0.0122, "delta": -5, "clamp": 25},
                "ic": {"support": 2, "rate": 0.0, "delta": 0, "clamp": 25},
            }},
        }
        yields = db.seniority_band_yields(priors)
        self.assertTrue(yields["bands"]["bridging"]["high_yield"])
        self.assertTrue(yields["bands"]["manager-lead"]["low_yield"])
        # Below MIN_BAND_YIELD_SUPPORT: no verdict either way.
        self.assertFalse(yields["bands"]["ic"]["credible"])
        self.assertFalse(yields["bands"]["ic"]["low_yield"])

    def test_band_note_states_the_observed_rate_and_that_it_never_rejects(self):
        priors = {
            "baseline_rate": 0.058,
            "dimensions": {"seniority_band": {
                "manager-lead": {"support": 82, "rate": 0.0122, "delta": -5, "clamp": 25}}},
        }
        note = db.band_triage_note("IT Manager", priors)
        self.assertIn("1.2%", note)
        self.assertIn("82 applications", note)
        self.assertIn("never rejects", note)
        # Nothing credible to say about an unobserved band -> no note at all.
        self.assertIsNone(db.band_triage_note("Cloud Engineer", priors))


if __name__ == "__main__":
    unittest.main()
