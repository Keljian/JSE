"""Golden request/response checks for bridge command dispatch.

The bridge is the only contract between the renderer and every Python module,
and it was previously untested: a renamed command key or a changed response
shape would surface as a broken button rather than a failing build. These tests
pin the dispatch table and the response shape of the commands added alongside
job flags and channel warmth.
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
import python_bridge as bridge  # noqa: E402


class _BridgeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_data = tempfile.mkdtemp(prefix="jse_bridge_test_")
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

    def _add(self, title, company, url, **columns):
        db.add_job(
            {"title": title, "company": company, "location": "Melbourne VIC",
             "url": url, "description": f"{title} at {company}."},
            "Seek", 1,
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


class DispatchTableTests(unittest.TestCase):
    def test_new_commands_are_registered_and_callable(self):
        for name in ("jobs:addFlag", "jobs:dismissFlag", "jobs:clearFlags",
                     "jobs:setChannel", "jobs:exportShortlist"):
            self.assertIn(name, bridge.COMMANDS, f"{name} is missing from the dispatch table")
            self.assertTrue(callable(bridge.COMMANDS[name]))

    def test_every_registered_command_is_callable(self):
        # A stale entry pointing at a renamed function is a runtime-only
        # failure; catch it at import time instead.
        for name, handler in bridge.COMMANDS.items():
            self.assertTrue(callable(handler), f"{name} is registered but not callable")


class FlagCommandTests(_BridgeTestCase):
    def test_add_and_dismiss_round_trip(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/bv1")

        added = bridge.COMMANDS["jobs:addFlag"]({
            "job_id": job_id, "type": "credential_gate",
            "requirement": "NV1 clearance required.", "detail": "Not held.",
        })
        self.assertEqual(len(added["flags"]["flags"]), 1)
        self.assertEqual(added["flags"]["flags"][0]["source"], "manual")

        dismissed = bridge.COMMANDS["jobs:dismissFlag"]({
            "job_id": job_id, "requirement": "NV1 clearance required.",
        })
        self.assertEqual(dismissed["flags"]["flags"], [])

    def test_clear_removes_everything(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/bv2")
        bridge.COMMANDS["jobs:addFlag"]({
            "job_id": job_id, "type": "evidence_gap", "requirement": "No ERP.",
        })
        cleared = bridge.COMMANDS["jobs:clearFlags"]({"job_id": job_id})
        self.assertEqual(cleared["flags"]["flags"], [])

    def test_detail_carries_the_parsed_flags(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/bv3")
        db.update_job_flags(job_id, {
            "flags": [{"type": "evidence_gap", "requirement": "No ERP experience."}],
            "summary": "One gap.",
        })
        detail = bridge.COMMANDS["jobs:detail"]({"job_id": job_id})
        self.assertEqual(detail["job"]["job_flags"]["summary"], "One gap.")
        self.assertEqual(len(detail["job"]["job_flags"]["flags"]), 1)


class ChannelCommandTests(_BridgeTestCase):
    def test_set_and_clear_round_trip(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/ch1")

        result = bridge.COMMANDS["jobs:setChannel"]({"job_id": job_id, "channel": "warm_referral"})
        self.assertEqual(result["channel"], "warm_referral")
        self.assertEqual(result["job"]["channel"], "warm_referral")

        cleared = bridge.COMMANDS["jobs:setChannel"]({"job_id": job_id, "channel": ""})
        self.assertIsNone(cleared["channel"])
        self.assertIsNone(cleared["job"]["channel"])

    def test_unknown_channel_is_rejected(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/ch2")
        with self.assertRaises(ValueError):
            bridge.COMMANDS["jobs:setChannel"]({"job_id": job_id, "channel": "smoke_signal"})

    def test_detail_reports_stored_versus_derived(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/ch3")
        detail = bridge.COMMANDS["jobs:detail"]({"job_id": job_id})
        self.assertEqual(detail["job"]["channel_source"], "derived")
        self.assertEqual(detail["job"]["channel"], "board")

        bridge.COMMANDS["jobs:setChannel"]({"job_id": job_id, "channel": "warm_referral"})
        detail = bridge.COMMANDS["jobs:detail"]({"job_id": job_id})
        self.assertEqual(detail["job"]["channel_source"], "stored")
        self.assertEqual(detail["job"]["warmth"], db.WARMTH_WARM)


class JobsListTests(_BridgeTestCase):
    def test_list_annotates_warmth_and_ranks_warm_first(self):
        cold = self._add("Cold role", "Coldco", "https://example.com/l1", match_score=90)
        warm = self._add("Warm role", "Warmco", "https://example.com/l2", match_score=60)
        db.set_job_channel(warm, db.CHANNEL_WARM_REFERRAL)

        jobs = bridge.COMMANDS["jobs:list"]({"profile_id": 1})["jobs"]
        ids = [job["id"] for job in jobs]
        self.assertLess(ids.index(warm), ids.index(cold),
                        "a warm 60 must rank above a cold 90")
        by_id = {job["id"]: job for job in jobs}
        self.assertEqual(by_id[warm]["warmth_label"], "Warm")
        self.assertEqual(by_id[cold]["warmth_label"], "Cold")

    def test_priority_and_due_dates_still_outrank_warmth(self):
        # Warmth must not push an overdue action down the board.
        warm = self._add("Warm role", "Warmco", "https://example.com/l3")
        db.set_job_channel(warm, db.CHANNEL_WARM_REFERRAL)
        urgent = self._add("Urgent role", "Coldco", "https://example.com/l4", priority="high")

        ids = [job["id"] for job in bridge.COMMANDS["jobs:list"]({"profile_id": 1})["jobs"]]
        self.assertLess(ids.index(urgent), ids.index(warm))

    def test_compact_payload_keeps_the_flag_and_channel_fields(self):
        job_id = self._add("IT Manager", "Coldco", "https://example.com/l5")
        db.update_job_flags(job_id, {
            "flags": [{"type": "credential_gate", "requirement": "NV1 required."}],
        })
        jobs = bridge.COMMANDS["jobs:list"]({"profile_id": 1, "compact": True})["jobs"]
        job = next(item for item in jobs if item["id"] == job_id)
        for key in ("job_flags_types", "job_flags_json", "channel", "warmth", "warm_path"):
            self.assertIn(key, job, f"compact payload dropped {key}")
        self.assertEqual(job["job_flags_types"], "credential_gate")

    def test_warm_path_is_surfaced_on_listed_jobs(self):
        db.upsert_warm_contact("Dana Lee", organisation="Flavorite", role_title="COO", profile_id=1)
        job_id = self._add("IT Manager", "Flavorite", "https://example.com/l6")
        jobs = bridge.COMMANDS["jobs:list"]({"profile_id": 1})["jobs"]
        job = next(item for item in jobs if item["id"] == job_id)
        self.assertEqual(job["warmth"], db.WARMTH_NAMED)
        self.assertEqual(job["warm_path"][0]["name"], "Dana Lee")


if __name__ == "__main__":
    unittest.main()
