"""Prompts and searches follow the lane's search location, not Australia.

A lane set to Manchester, UK was scored by "an Australian career analyst" in
Australian English, and its LinkedIn searches went to Melbourne. These pin the
fix, and pin that an Australian lane's prompts stay byte-for-byte unchanged.
"""
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as db  # noqa: E402
import db_setup  # noqa: E402
import region  # noqa: E402
import scraper_plugin_builder as builder  # noqa: E402
import scraper_plugins  # noqa: E402
from llm import prompts  # noqa: E402

PROMPT_CONSTANTS = [
    value for name, value in vars(prompts).items()
    if name.isupper() and isinstance(value, str) and "PROMPT" in name
]


class MarketDetectionTests(unittest.TestCase):
    def test_australian_and_blank_locations_keep_the_prompts(self):
        for location in ("", None, "Melbourne VIC", "Sydney NSW", "Perth WA", "Victoria", "New South Wales",
                         "Brisbane, Queensland, Australia"):
            self.assertIsNone(region.market_for(location), location)

    def test_other_markets_are_recognised(self):
        self.assertEqual(region.market_for("Manchester, UK")["country"], "United Kingdom")
        self.assertEqual(region.market_for("Perth, Scotland")["country"], "United Kingdom")
        self.assertEqual(region.market_for("Austin, TX, USA")["country"], "United States")
        self.assertEqual(region.market_for("Auckland")["country"], "New Zealand")
        unknown = region.market_for("Berlin")
        self.assertIsNone(unknown["country"])
        self.assertEqual(unknown["label"], "Berlin")


class LocaliseTests(unittest.TestCase):
    def test_australian_lane_is_untouched(self):
        for prompt in PROMPT_CONSTANTS:
            self.assertEqual(region.localise(prompt, "Melbourne VIC", add_market_line=True), prompt)
        messages = [{"role": "system", "content": prompts.ANALYSIS_SYSTEM_PROMPT_BASE}]
        self.assertIs(region.localise_messages(messages, "Melbourne VIC"), messages)

    def test_uk_lane_loses_the_australian_framing(self):
        for prompt in PROMPT_CONSTANTS:
            text = region.localise(prompt, "Manchester, UK")
            self.assertNotIn("Australian", text)
            self.assertNotIn("AUSTRALIAN", text)
            self.assertNotIn("ASX", text)
        analysis = region.localise(prompts.ANALYSIS_SYSTEM_PROMPT_BASE, "Manchester, UK")
        self.assertIn("British English spelling", analysis)
        self.assertIn("a senior UK career analyst", analysis)
        triage = region.localise(prompts.TRIAGE_SYSTEM_PROMPT_BASE, "Manchester, UK")
        self.assertIn("for a UK job-search pipeline", triage)

    def test_unknown_market_drops_the_adjective_and_fixes_the_article(self):
        triage = region.localise(prompts.TRIAGE_SYSTEM_PROMPT_BASE, "Berlin")
        self.assertIn("for a job-search pipeline", triage)

    def test_market_line_is_added_once_to_the_system_message(self):
        messages = [
            {"role": "system", "content": "You are an Australian analyst."},
            {"role": "user", "content": "Analyse this Australian job advertisement."},
        ]
        once = region.localise_messages(messages, "Manchester, UK")
        twice = region.localise_messages(once, "Manchester, UK")
        self.assertEqual(once, twice)
        self.assertIn(region.MARKET_LINE_PREFIX, once[0]["content"])
        self.assertIn("Manchester, UK", once[0]["content"])
        self.assertNotIn(region.MARKET_LINE_PREFIX, once[1]["content"])
        self.assertEqual(once[1]["content"], "Analyse this UK job advertisement.")
        self.assertEqual(messages[0]["content"], "You are an Australian analyst.", "input must not be mutated")

    def test_active_location_is_scoped_and_does_not_leak_into_new_threads(self):
        seen = {}
        with region.use_location("Manchester, UK"):
            self.assertEqual(region.active_location(), "Manchester, UK")
            worker = threading.Thread(target=lambda: seen.setdefault("loc", region.active_location()))
            worker.start()
            worker.join()
        self.assertIsNone(seen["loc"])
        self.assertIsNone(region.active_location())

    def test_letter_conventions(self):
        from datetime import datetime
        when = datetime(2026, 10, 9)
        self.assertEqual(region.letter_date(when, "Austin, TX, USA"), "October 9, 2026")
        self.assertEqual(region.letter_closing("Austin, TX, USA"), "Sincerely,")
        self.assertEqual(region.letter_date(when, "Manchester, UK"), "09 October 2026")
        self.assertEqual(region.letter_closing("Melbourne VIC"), "Yours sincerely,")


class SearchLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_region_test_")
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

    def test_source_locations_follow_the_lane(self):
        db.add_lane("UK lane", "resume.docx", {"preferred_location": "Manchester, UK"})
        lane = db.get_profile_by_name("UK lane")
        settings = db.get_lane_settings(lane["id"])
        self.assertEqual(settings["linkedin_location"], "Manchester, UK")
        self.assertEqual(settings["seek_location"], "Manchester, UK")

        db.update_profile_settings(lane["id"], {**settings, "preferred_location": "Leeds, UK"})
        self.assertEqual(db.get_lane_settings(lane["id"])["linkedin_location"], "Leeds, UK")

        db.update_profile_settings(lane["id"], {"linkedin_location": "London, UK"})
        self.assertEqual(db.get_lane_settings(lane["id"])["linkedin_location"], "London, UK",
                         "a deliberately different source location is kept")

    def test_lane_scraper_location_beats_the_legacy_column(self):
        plugin = {
            "manifest": {"config_schema": [
                {"key": "location", "default": "Melbourne VIC", "legacy_key": "linkedin_location"},
            ]},
            "config": {},
            "lane_config": {"location": "Manchester, UK"},
        }
        config = scraper_plugins.build_config(plugin, {"linkedin_location": "Melbourne VIC"})
        self.assertEqual(config["location"], "Manchester, UK")

    def test_blank_source_location_uses_the_lane_search_location(self):
        plugin = {"manifest": {"config_schema": [{"key": "location", "default": ""}]}, "config": {}, "lane_config": {}}
        config = scraper_plugins.build_config(plugin, {"preferred_location": "Manchester, UK"})
        self.assertEqual(config["location"], "Manchester, UK")

    def test_builder_never_keeps_a_location_the_model_copied(self):
        generated = builder._normalise_generation(
            {
                "manifest": {"config_schema": [{"key": "location", "label": "Location", "type": "text",
                                                "default": "Melbourne VIC", "legacy_key": "linkedin_location"}]},
                "scraper_code": "def scrape(keyword, **config):\n    return False\n",
            },
            {"source_name": "LinkedIn UK", "careers_url": "https://example.com", "location": ""},
        )
        item = next(i for i in generated["manifest"]["config_schema"] if i["key"] == "location")
        self.assertEqual(item["default"], "")
        self.assertNotIn("legacy_key", item)


if __name__ == "__main__":
    unittest.main()
