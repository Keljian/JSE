"""Search outcomes, empty-search retries, and keeping the LLM out of the scrape.

A search that found listings it had already saved used to look exactly like a
search that found nothing. Both were retried with an LLM-broadened term, and
both counted toward a source's empty streak. With the LLM endpoint down, every
duplicate-only search paid three backoff retries, and a SEEK location that
matched nothing went a week before health flagged it.
"""
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_logic  # noqa: E402
import database_manager as db  # noqa: E402
import db_setup  # noqa: E402
import scraper_dispatcher  # noqa: E402
from db import jobs as db_jobs  # noqa: E402


class _DbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_search_outcome_test_")
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
            conn.execute("DELETE FROM search_term_retries")
            conn.commit()
        if not db.get_profile_by_name("Search outcome lane"):
            db.add_profile("Search outcome lane", "")
        self.lane_id = db.get_profile_by_name("Search outcome lane")["id"]


def _fake_plugin_run(scraper_func):
    """Patch the plugin registry so run_scraper_search calls scraper_func."""
    return [
        mock.patch.object(scraper_dispatcher.scraper_plugins, "get_plugin", return_value={"id": "fake"}),
        mock.patch.object(scraper_dispatcher.scraper_plugins, "load_callable", return_value=scraper_func),
        mock.patch.object(scraper_dispatcher.scraper_plugins, "build_config", return_value={}),
    ]


class OutcomeClassificationTests(unittest.TestCase):
    def test_classifies_plugin_results(self):
        classify = scraper_dispatcher._classify
        self.assertEqual("saved", classify(True, 0))
        self.assertEqual("no_new", classify(False, 3))
        self.assertEqual("empty", classify(False, 0))
        self.assertEqual("skipped", classify({"ok": False, "skipped": "rate_limited"}, 0))
        self.assertEqual("no_new", classify({"ok": False, "found": 2}, 0))
        self.assertEqual("saved", classify({"ok": True}, 0))
        # Rate-limited part way through: the listings and saves still count.
        self.assertEqual("no_new", classify({"ok": False, "skipped": "rate_limited"}, 4))
        self.assertEqual("saved", classify({"ok": False, "skipped": "rate_limited"}, 4, 2))
        self.assertEqual("saved", classify(False, 3, 1))

    def test_listing_counts_are_per_thread(self):
        db.reset_listing_count()
        seen = {}

        def other_thread():
            db.reset_listing_count()
            db_jobs._listing_counter.count += 5
            seen["other"] = db.listing_count()

        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
        self.assertEqual(5, seen["other"])
        self.assertEqual(0, db.listing_count())


class DispatcherOutcomeTests(unittest.TestCase):
    def _run(self, scraper_func):
        patches = _fake_plugin_run(scraper_func)
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])
        with mock.patch.object(scraper_dispatcher.db, "record_scraper_health") as health:
            search = scraper_dispatcher.run_scraper_search("Fake", "IT Manager")
        return search, health

    def test_duplicate_only_search_is_no_new_and_healthy(self):
        def scraper(**kwargs):
            # The listing is offered to add_job and rejected, as a duplicate is.
            with mock.patch.object(db_jobs, "_should_store_scraped_job", return_value=False):
                db.add_job({"title": "IT Manager", "url": "https://x.test/1"}, "Fake")
            return False

        search, health = self._run(scraper)
        self.assertEqual("no_new", search["outcome"])
        self.assertEqual(1, search["listings"])
        self.assertFalse(search["saved"])
        health.assert_called_once_with("fake", "success")

    def test_new_listing_counts_as_saved_even_if_the_plugin_says_false(self):
        def scraper(**kwargs):
            with mock.patch.object(db_jobs, "_add_job", return_value=True):
                db.add_job({"title": "IT Manager", "url": "https://x.test/2"}, "Fake")
            return {"ok": False, "skipped": "rate_limited"}

        search, health = self._run(scraper)
        self.assertEqual("saved", search["outcome"])
        health.assert_called_once_with("fake", "success")

    def test_search_with_no_listings_is_empty(self):
        search, health = self._run(lambda **kwargs: False)
        self.assertEqual("empty", search["outcome"])
        health.assert_called_once_with("fake", "empty")

    def test_skipped_search_records_no_health(self):
        search, health = self._run(lambda **kwargs: {"ok": False, "skipped": "rate_limited"})
        self.assertEqual("skipped", search["outcome"])
        health.assert_not_called()

    def test_bool_wrapper_still_reports_saved(self):
        patches = _fake_plugin_run(lambda **kwargs: True)
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])
        with mock.patch.object(scraper_dispatcher.db, "record_scraper_health"):
            self.assertIs(True, scraper_dispatcher.run_scraper_for_keyword("Fake", "IT Manager"))


class BroadenSearchTermTests(unittest.TestCase):
    def test_drops_seniority_and_qualifiers(self):
        self.assertEqual("IT Manager", app_logic.broaden_search_term("Senior IT Manager"))
        self.assertEqual("Engineer", app_logic.broaden_search_term("Lead Engineer (Contract)"))
        self.assertEqual("Business Analyst", app_logic.broaden_search_term("Business Analyst - Level 6"))

    def test_returns_none_when_nothing_to_drop(self):
        self.assertIsNone(app_logic.broaden_search_term("IT Manager"))
        self.assertIsNone(app_logic.broaden_search_term("Operational Technology"))
        self.assertIsNone(app_logic.broaden_search_term("Senior"))


class EmptySearchStoreTests(_DbTestCase):
    def test_suggestion_lifecycle(self):
        db.record_empty_search(self.lane_id, "Technical Services Manager", "Seek")
        self.assertEqual(["Technical Services Manager"], db.get_empty_searches_needing_suggestion(self.lane_id))

        # Analysis found nothing better: no longer pending...
        db.save_search_term_suggestion(self.lane_id, "Technical Services Manager", None)
        self.assertEqual([], db.get_empty_searches_needing_suggestion(self.lane_id))
        # ...until the term comes back empty again.
        db.record_empty_search(self.lane_id, "technical services manager", "LinkedIn")
        self.assertEqual(["Technical Services Manager"], db.get_empty_searches_needing_suggestion(self.lane_id))

        db.save_search_term_suggestion(self.lane_id, "Technical Services Manager", "Facilities Manager")
        self.assertEqual("Facilities Manager", db.get_search_term_suggestion(self.lane_id, "Technical Services Manager"))
        # A real suggestion survives later empty searches.
        db.record_empty_search(self.lane_id, "Technical Services Manager", "Seek")
        self.assertEqual("Facilities Manager", db.get_search_term_suggestion(self.lane_id, "Technical Services Manager"))
        self.assertEqual([], db.get_empty_searches_needing_suggestion(self.lane_id))

    def test_a_suggestion_equal_to_the_term_is_not_stored(self):
        db.record_empty_search(self.lane_id, "IT Manager", "Seek")
        db.save_search_term_suggestion(self.lane_id, "IT Manager", "it manager")
        self.assertIsNone(db.get_search_term_suggestion(self.lane_id, "IT Manager"))


class SweepRetryTests(_DbTestCase):
    def _sweep(self, outcomes, keywords):
        calls = []

        def fake_task(source, keyword, *args, **kwargs):
            calls.append((source, keyword))
            outcome = outcomes(source, keyword)
            return {"source": source, "keyword": keyword, "success": outcome == "saved",
                    "outcome": outcome, "listings": 0 if outcome == "empty" else 3}

        with mock.patch.object(app_logic.db, "dedupe_database"), \
             mock.patch.object(app_logic.scraper_plugins, "plugin_mode", return_value="keyword"), \
             mock.patch.object(app_logic, "_run_scraper_task", side_effect=fake_task), \
             mock.patch.object(app_logic.llm_handler, "generalize_search_term",
                               side_effect=AssertionError("a scrape must not call the LLM")), \
             mock.patch.object(app_logic.db, "mark_missing_new_jobs_after_sweep", return_value={}) as missing:
            app_logic.execute_scraping_and_analysis(
                keywords, ["Seek", "LinkedIn"], "resume text",
                lambda message, progress=False: None, lambda message: None,
                profile_id=self.lane_id,
                search_settings={"preferred_location": "Melbourne", "work_modes": []},
            )
        return calls, missing

    def test_only_empty_searches_are_retried_and_without_the_llm(self):
        outcomes = lambda source, keyword: "empty" if source == "Seek" else "no_new"
        calls, missing = self._sweep(outcomes, ["Senior IT Manager", "Automation Manager"])
        retries = calls[4:]
        # Seek's "Senior IT Manager" broadens; "Automation Manager" has no
        # deterministic broadening and waits for analysis. LinkedIn found
        # listings (all duplicates) and is not retried.
        self.assertEqual([("Seek", "IT Manager")], retries)
        self.assertEqual(
            {"Senior IT Manager", "Automation Manager"},
            set(db.get_empty_searches_needing_suggestion(self.lane_id)),
        )
        # A source that found nothing is not a completed sweep, so its jobs
        # do not tick toward being archived as unlisted.
        self.assertEqual({"LinkedIn"}, set(missing.call_args.args[1]))

    def test_retry_uses_the_suggestion_analysis_stored(self):
        db.record_empty_search(self.lane_id, "Automation Manager", "Seek")
        db.save_search_term_suggestion(self.lane_id, "Automation Manager", "Controls Engineer")
        outcomes = lambda source, keyword: "empty" if (source, keyword) == ("Seek", "Automation Manager") else "saved"
        calls, _ = self._sweep(outcomes, ["Automation Manager"])
        self.assertIn(("Seek", "Controls Engineer"), calls)


class AnalysisTermUpkeepTests(_DbTestCase):
    def test_unreachable_endpoint_skips_the_llm(self):
        db.save_profile_terms(self.lane_id, ["IT Manager"])
        db.record_empty_search(self.lane_id, "Automation Manager", "Seek")
        with mock.patch.object(app_logic, "_scoring_endpoint_unreachable", return_value=True), \
             mock.patch.object(app_logic.llm_handler, "generalize_search_term") as generalize:
            app_logic.prepare_lane_search_terms(self.lane_id, "resume", lambda message: None)
        generalize.assert_not_called()
        self.assertEqual(["Automation Manager"], db.get_empty_searches_needing_suggestion(self.lane_id))

    def test_stores_llm_suggestions_for_empty_searches(self):
        db.save_profile_terms(self.lane_id, ["IT Manager"])
        db.record_empty_search(self.lane_id, "Automation Manager", "Seek")
        with mock.patch.object(app_logic, "_scoring_endpoint_unreachable", return_value=False), \
             mock.patch.object(app_logic.llm_handler, "generalize_search_term", return_value="Controls Engineer"):
            result = app_logic.prepare_lane_search_terms(self.lane_id, "resume", lambda message: None)
        self.assertEqual(1, result["suggested"])
        self.assertEqual("Controls Engineer", db.get_search_term_suggestion(self.lane_id, "Automation Manager"))

    def test_nothing_to_do_makes_no_calls(self):
        db.save_profile_terms(self.lane_id, ["IT Manager"])
        with mock.patch.object(app_logic, "_scoring_endpoint_unreachable") as probe:
            app_logic.prepare_lane_search_terms(self.lane_id, "resume", lambda message: None)
        probe.assert_not_called()


class UserPluginModuleReuseTests(unittest.TestCase):
    def test_module_state_survives_between_searches_until_the_file_changes(self):
        import os
        import scraper_plugins
        root = Path(tempfile.mkdtemp(prefix="jse_plugin_reuse_"))
        self.addCleanup(shutil.rmtree, root, True)
        module_path = root / "scraper.py"
        module_path.write_text("STATE = []\ndef scrape(**kwargs):\n    STATE.append(1)\n    return len(STATE)\n", encoding="utf-8")
        plugin = {"id": "reuse_test", "install_type": "user", "install_path": str(root),
                  "manifest": {"module": "scraper.py", "callable": "scrape"}}

        first = scraper_plugins.load_callable(plugin)
        self.assertEqual(1, first())
        # Same file: same module, so a lock or cooldown in it persists.
        self.assertEqual(2, scraper_plugins.load_callable(plugin)())

        module_path.write_text("STATE = []\ndef scrape(**kwargs):\n    return 'edited'\n", encoding="utf-8")
        stat = module_path.stat()
        os.utime(module_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertEqual("edited", scraper_plugins.load_callable(plugin)())


class FallbackTermsTests(unittest.TestCase):
    def test_uses_target_titles(self):
        settings = {"target_titles": "IT Manager, OT Manager\n- Systems Lead; it manager"}
        self.assertEqual(
            ["IT Manager", "OT Manager", "Systems Lead"],
            app_logic.fallback_search_terms(1, settings),
        )

    def test_falls_back_to_the_lane_name(self):
        with mock.patch.object(app_logic.db, "get_profile_by_id", return_value={"name": "IT & OT"}):
            self.assertEqual(["IT & OT"], app_logic.fallback_search_terms(1, {"target_titles": ""}))


if __name__ == "__main__":
    unittest.main()
