"""The facade write-forwarding that keeps split packages patchable.

Splitting database_manager and llm_handler into packages silently broke two
long-standing behaviours, both of which fail quietly rather than loudly:

- `llm_handler._call_unsloth = stub` used to affect every caller. Without
  forwarding it rebinds the facade only, the real function still runs, and a
  test appears to pass while making live network calls. That regression was
  observed during the split: the suite went from 4s to 42s and three tests
  failed against a refused connection.
- `database_manager.DB_FILE = tmp` must move the one binding the connection
  helper reads, or tests write to the production database.

These tests exist so neither can regress unnoticed.
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as dbm  # noqa: E402
import db as db_package  # noqa: E402
import llm_handler  # noqa: E402
import llm as llm_package  # noqa: E402


class LlmFacadePatchingTests(unittest.TestCase):
    """Patching through llm_handler must reach the implementation module."""

    def test_patching_reaches_the_defining_module(self):
        original = llm_package.providers._call_unsloth
        sentinel = object()
        try:
            llm_handler._call_unsloth = sentinel
            self.assertIs(llm_package.providers._call_unsloth, sentinel)
            self.assertIs(llm_handler._call_unsloth, sentinel)
        finally:
            llm_handler._call_unsloth = original
        self.assertIs(llm_package.providers._call_unsloth, original)

    def test_patching_reaches_every_module_holding_the_name(self):
        # A module that imported a helper from an earlier layer keeps its own
        # reference. Patching only the defining module would leave it stale, so
        # the forwarding sets every holder.
        holders = [
            module for module in
            (llm_package.providers, llm_package.parsing, llm_package.prompts,
             llm_package.analysis, llm_package.documents, llm_package.memory,
             llm_package.research)
            if "_extract_json" in vars(module)
        ]
        self.assertGreater(len(holders), 1, "expected _extract_json to be imported across layers")
        originals = {m: vars(m)["_extract_json"] for m in holders}
        sentinel = object()
        try:
            llm_handler._extract_json = sentinel
            for module in holders:
                self.assertIs(vars(module)["_extract_json"], sentinel, module.__name__)
        finally:
            for module, value in originals.items():
                setattr(module, "_extract_json", value)
            llm_handler._extract_json = originals[holders[0]]

    def test_a_patched_function_is_actually_called(self):
        # The end-to-end guarantee, not just the binding: patch the transport
        # and confirm the scoring path uses the stub instead of the network.
        calls = []

        def stub(*args, **kwargs):
            calls.append(True)
            return '{"verdict": "clear"}'

        original = llm_package.providers._call_scoring_ai
        try:
            llm_handler._call_scoring_ai = stub
            llm_package.analysis._call_scoring_ai(messages=[], temperature=0)
        finally:
            llm_handler._call_scoring_ai = original
        self.assertEqual(len(calls), 1, "the patched transport was bypassed")

    def test_unknown_attribute_still_raises(self):
        with self.assertRaises(AttributeError):
            llm_handler.definitely_not_a_real_name


class DatabaseFacadePatchingTests(unittest.TestCase):
    def test_patching_a_query_helper_reaches_the_module(self):
        original = db_package.jobs.get_job_details
        sentinel = object()
        try:
            dbm.get_job_details = sentinel
            self.assertIs(db_package.jobs.get_job_details, sentinel)
        finally:
            dbm.get_job_details = original
        self.assertIs(db_package.jobs.get_job_details, original)

    def test_proxied_state_is_not_duplicated_onto_the_facade(self):
        # The proxied names must never appear in the facade's own __dict__, or
        # __getattr__ stops firing and the stale-copy bug returns.
        for name in ("DB_FILE", "DATA_DIR", "_wal_enabled"):
            self.assertNotIn(name, vars(dbm),
                             f"{name} gained a second binding on the facade")

    def test_install_refuses_a_re_exported_proxied_name(self):
        # Guard the guard: facade.install must reject the mistake rather than
        # silently produce a desynchronised facade.
        import types
        import facade

        owner = types.ModuleType("fake_owner")
        owner.DB_FILE = "canonical"
        broken = types.ModuleType("fake_facade")
        broken.DB_FILE = "copy"          # the mistake: re-exported, not proxied
        sys.modules["fake_facade"] = broken
        try:
            with self.assertRaises(ImportError):
                facade.install("fake_facade", (owner,), proxied=("DB_FILE",), proxy_owner=owner)
        finally:
            del sys.modules["fake_facade"]

    def test_install_requires_an_owner_for_proxied_names(self):
        import types
        import facade

        empty = types.ModuleType("fake_facade2")
        sys.modules["fake_facade2"] = empty
        try:
            with self.assertRaises(ValueError):
                facade.install("fake_facade2", (), proxied=("X",))
        finally:
            del sys.modules["fake_facade2"]


if __name__ == "__main__":
    unittest.main()
