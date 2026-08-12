"""Regression tests for how JSE paces requests at the local LLM endpoint.

Three separate faults produced the same symptom — a run collapsing into
"local endpoint timeout" — and each is pinned here:

1. A flat 120s client timeout abandoned generations the server was still
   working on, so the retry landed on a busy endpoint.
2. Retries came back on a short, near-linear backoff, stacking requests.
3. The concurrency gate was sized from the *scoring* provider but gated every
   provider, so a hosted scoring provider with several workers let local calls
   run many-wide against a server that answers one at a time.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import concurrency  # noqa: E402
from llm import providers  # noqa: E402


class LocalTimeoutBudgetTests(unittest.TestCase):
    def _timeout_for(self, max_tokens):
        seen = {}

        def fake_post(_url, _headers, payload, timeout=None):
            seen["timeout"] = timeout
            seen["max_tokens"] = payload["max_tokens"]
            return {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(providers, "_post_json", fake_post), \
             mock.patch.object(providers, "_local_ai_settings",
                               return_value={"base_url": "http://localhost:1234/v1",
                                             "api_key": "", "model": "test-model"}), \
             mock.patch.object(providers, "_analysis_worker_count", return_value=1), \
             mock.patch.object(providers, "_local_context_length", return_value=None), \
             mock.patch.object(providers, "_count_local_prompt_tokens", return_value=None), \
             mock.patch.object(providers, "_local_lock_path", return_value=None):
            providers._call_unsloth([{"role": "user", "content": "hi"}], max_tokens=max_tokens)
        return seen

    def test_the_wait_scales_with_the_output_budget(self):
        # The old flat 120s was under the honest generation time for anything
        # but the smallest budget.
        small = self._timeout_for(512)["timeout"]
        large = self._timeout_for(8192)["timeout"]
        self.assertGreater(small, 120, "even a small budget needs more than the old flat timeout")
        self.assertGreater(large, small)

    def test_the_wait_is_capped(self):
        # A wedged endpoint must not hold a worker indefinitely.
        self.assertLessEqual(self._timeout_for(16384)["timeout"], providers.LOCAL_TIMEOUT_CEILING)


class BackoffTests(unittest.TestCase):
    def test_backoff_grows_exponentially_and_is_capped(self):
        delays = [providers._backoff_delay(n, base=5, cap=60) for n in range(1, 6)]
        self.assertEqual(delays, [5, 10, 20, 40, 60])

    def test_a_timeout_cools_down_longer_than_an_ordinary_retry(self):
        # The abandoned generation is still running on the server; coming back
        # on the ordinary backoff is what stacked requests on it.
        ordinary = providers._backoff_delay(1)
        after_timeout = providers._backoff_delay(1, base=providers.LOCAL_TIMEOUT_COOLDOWN)
        self.assertGreater(after_timeout, ordinary)

    def test_a_server_supplied_retry_after_is_honoured(self):
        calls = []
        sleeps = []

        def fake_post(_url, _headers, _payload, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise providers.LLMHTTPError(429, "429 Too Many Requests", "busy", retry_after=17)
            return {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(providers, "_post_json", fake_post), \
             mock.patch.object(providers, "_interruptible_sleep", sleeps.append), \
             mock.patch.object(providers, "_local_ai_settings",
                               return_value={"base_url": "http://localhost:1234/v1",
                                             "api_key": "", "model": "test-model"}), \
             mock.patch.object(providers, "_analysis_worker_count", return_value=1), \
             mock.patch.object(providers, "_local_context_length", return_value=None), \
             mock.patch.object(providers, "_count_local_prompt_tokens", return_value=None), \
             mock.patch.object(providers, "_local_lock_path", return_value=None):
            providers._call_unsloth([{"role": "user", "content": "hi"}])

        self.assertEqual(sleeps, [17])

    def test_a_backoff_is_interruptible(self):
        concurrency.cancel_event.set()
        self.addCleanup(concurrency.cancel_event.clear)
        started = time.monotonic()
        with self.assertRaises(concurrency.OperationCancelledError):
            providers._interruptible_sleep(30)
        self.assertLess(time.monotonic() - started, 5, "cancel should not wait out the backoff")


class LocalSerializationTests(unittest.TestCase):
    def test_only_one_local_request_is_in_flight_at_a_time(self):
        # The gate used to be sized from the scoring provider. With scoring on a
        # hosted provider and several workers, local calls went many-wide.
        concurrent = []
        peak = []
        guard = threading.Lock()

        def fake_post(_url, _headers, _payload, timeout=None):
            with guard:
                concurrent.append(1)
                peak.append(len(concurrent))
            time.sleep(0.05)
            with guard:
                concurrent.pop()
            return {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(providers, "_post_json", fake_post), \
             mock.patch.object(providers, "_local_ai_settings",
                               return_value={"base_url": "http://localhost:1234/v1",
                                             "api_key": "", "model": "test-model"}), \
             mock.patch.object(providers, "_analysis_worker_count", return_value=8), \
             mock.patch.object(providers, "_local_context_length", return_value=None), \
             mock.patch.object(providers, "_count_local_prompt_tokens", return_value=None), \
             mock.patch.object(providers, "_local_lock_path", return_value=None):
            threads = [
                threading.Thread(target=providers._call_unsloth,
                                 args=([{"role": "user", "content": "hi"}],))
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(max(peak), 1, "local endpoint must stay single-slot")

    def test_the_cross_process_lock_excludes_a_second_holder(self):
        with mock.patch.object(providers, "_local_lock_path",
                               return_value=str(Path(self.tmp) / "llm.lock")):
            with providers._local_endpoint_lock():
                self.assertTrue(Path(self.tmp, "llm.lock").exists())
                # A second process would find the file and wait. Simulate its
                # non-blocking probe rather than deadlocking this test.
                self.assertFalse(self._can_take_lock_now())
            self.assertFalse(Path(self.tmp, "llm.lock").exists(),
                             "the lock must be released for the next process")

    def test_a_stale_lock_does_not_wedge_the_app(self):
        # A task process killed mid-request leaves the file behind. Nothing
        # refreshes it, so the next caller must be able to break it.
        lock = Path(self.tmp) / "llm.lock"
        lock.write_text("999999", encoding="utf-8")
        import os
        stale = time.time() - (providers.LOCAL_LOCK_STALE_AFTER + 10)
        os.utime(lock, (stale, stale))
        with mock.patch.object(providers, "_local_lock_path", return_value=str(lock)):
            with providers._local_endpoint_lock():
                pass  # acquired rather than blocking forever

    def test_the_lock_fails_open_when_it_cannot_be_created(self):
        # Losing serialization is survivable; a lock that can wedge is not.
        with mock.patch.object(providers, "_local_lock_path",
                               return_value=str(Path(self.tmp) / "no" / "such" / "dir" / "llm.lock")):
            with providers._local_endpoint_lock():
                pass

    def setUp(self):
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="jse_llm_lock_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _can_take_lock_now(self):
        import os
        try:
            handle = os.open(str(Path(self.tmp) / "llm.lock"), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            return False
        os.close(handle)
        return True


if __name__ == "__main__":
    unittest.main()
