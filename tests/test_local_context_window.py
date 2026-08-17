"""Regression tests for how JSE sizes requests to the local context window.

Unsloth Studio serves whatever window the model was *loaded* with, which can be
far below the model's native context (a 262K-native Qwen3 loaded at 4096 was the
case that prompted this). Nothing in the OpenAI protocol announces that: the
server accepts an oversized max_tokens, truncates the answer at the window, and
returns finish_reason="length". JSE used to accept the fragment as if it were a
complete response, so structured calls came back as unparseable half-objects.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm import providers  # noqa: E402


LOCAL = {
    "base_url": "http://localhost:8888/v1",
    "api_key": "",
    "model": "test-model",
    "context_target": 32768,
    "auto_reload": True,
}


def _catalogue(*rows):
    return {"object": "list", "data": list(rows)}


class ContextDiscoveryTests(unittest.TestCase):
    def setUp(self):
        providers._local_context_cache.clear()
        self.addCleanup(providers._local_context_cache.clear)

    def _context(self, payload):
        with mock.patch.object(providers, "_get_json", return_value=payload):
            return providers._local_context_length(LOCAL)

    def test_the_loaded_window_is_read_not_the_native_one(self):
        # The distinction is the whole point: native says what the model could
        # do, context_length says what this server will actually serve.
        context = self._context(_catalogue({
            "id": "test-model", "loaded": True,
            "context_length": 4096, "max_context_length": 4096,
            "native_context_length": 262144,
        }))
        self.assertEqual(context, 4096)

    def test_an_endpoint_that_reports_nothing_is_not_a_failure(self):
        self.assertIsNone(self._context(_catalogue({"id": "test-model", "loaded": True})))

    def test_an_unreachable_catalogue_is_not_a_failure(self):
        with mock.patch.object(providers, "_get_json", side_effect=OSError("refused")):
            self.assertIsNone(providers._local_context_length(LOCAL))

    def test_the_window_is_cached_between_calls(self):
        payload = _catalogue({"id": "test-model", "loaded": True, "context_length": 8192})
        with mock.patch.object(providers, "_get_json", return_value=payload) as get:
            providers._local_context_length(LOCAL)
            providers._local_context_length(LOCAL)
        self.assertEqual(get.call_count, 1)


class LoadedModelSelectionTests(unittest.TestCase):
    def test_the_loaded_model_wins_over_catalogue_order(self):
        # Unsloth Studio lists every model it *could* load alongside the live
        # one, so taking data[0] can describe a model that isn't running.
        row = providers._loaded_model_row([
            {"id": "some/other-model", "loaded": False},
            {"id": "the-live-one", "loaded": True},
        ])
        self.assertEqual(row["id"], "the-live-one")

    def test_a_configured_folder_path_matches_its_api_id(self):
        # The Model setting may hold the path the model was loaded from while
        # /models reports only the final segment as the id.
        row = providers._loaded_model_row(
            [{"id": "Qwen3.6-35B-A3B-MTP-GGUF", "loaded": True}],
            r"F:\AI Models\unsloth\Qwen3.6-35B-A3B-MTP-GGUF",
        )
        self.assertEqual(row["id"], "Qwen3.6-35B-A3B-MTP-GGUF")

    def test_an_empty_catalogue_selects_nothing(self):
        self.assertIsNone(providers._loaded_model_row([]))


class OutputBudgetTests(unittest.TestCase):
    def test_the_budget_is_trimmed_to_what_the_window_leaves(self):
        fitted = providers._fit_output_budget(8000, context=4096, prompt_tokens=1000)
        self.assertLessEqual(fitted + 1000 + providers.LOCAL_CONTEXT_RESERVE, 4096)

    def test_a_budget_that_already_fits_is_untouched(self):
        self.assertEqual(providers._fit_output_budget(2000, context=32768, prompt_tokens=4000), 2000)

    def test_an_unknown_window_leaves_the_budget_alone(self):
        self.assertEqual(providers._fit_output_budget(8000, context=None, prompt_tokens=4000), 8000)

    def test_a_prompt_that_cannot_fit_fails_with_an_explanation(self):
        with self.assertRaises(Exception) as caught:
            providers._fit_output_budget(4000, context=4096, prompt_tokens=3900)
        message = str(caught.exception)
        self.assertIn("4096", message)
        self.assertIn("context", message.lower())

    def test_the_prompt_estimate_grows_with_the_prompt(self):
        small = providers._estimate_prompt_tokens([{"role": "user", "content": "hi"}])
        large = providers._estimate_prompt_tokens([{"role": "user", "content": "word " * 4000}])
        self.assertGreater(large, small)
        self.assertGreater(large, 4000, "the estimate must not undercount a long prompt")


class SetContextWindowTests(unittest.TestCase):
    """JSE sets the window itself, over Unsloth Studio's load API."""

    STATUS = {
        "model_identifier": r"F:\AI Models\unsloth\Qwen3.6-35B-A3B-MTP-GGUF",
        "active_model": "Qwen3.6-35B-A3B-MTP-GGUF",
        "context_length": 4096,
        "native_context_length": 262144,
        "gguf_variant": "UD-Q6_K",
        "cache_type_kv": "q8_0",
        "speculative_type": "auto",
        "gpu_memory_mode": "auto",
        "gpu_layers": -1,
    }

    def setUp(self):
        providers._local_context_cache.clear()
        providers._local_reload_attempted.clear()
        self.addCleanup(providers._local_context_cache.clear)
        self.addCleanup(providers._local_reload_attempted.clear)

    def _reload(self, target=32768, after=None, post=None):
        posts = []

        def fake_post(url, _headers, payload, timeout=None):
            posts.append((url, payload, timeout))
            return (post or (lambda: {}))()

        statuses = [self.STATUS, {**self.STATUS, **(after or {"context_length": target})}]

        def fake_status(_local=None):
            return statuses.pop(0) if len(statuses) > 1 else statuses[0]

        with mock.patch.object(providers, "_post_json", fake_post), \
             mock.patch.object(providers, "local_status", fake_status):
            result = providers.set_local_context_window(target, local=dict(LOCAL))
        return result, posts

    def test_the_window_is_sent_as_max_seq_length(self):
        _, posts = self._reload(32768)
        url, payload, timeout = posts[0]
        self.assertTrue(url.endswith("/v1/load"))
        self.assertEqual(payload["max_seq_length"], 32768)
        self.assertGreaterEqual(timeout, 600, "paging in tens of GB is not a two-minute operation")

    def test_the_reload_targets_the_model_already_loaded(self):
        _, posts = self._reload()
        self.assertEqual(posts[0][1]["model_path"], self.STATUS["model_identifier"])

    def test_the_current_load_shape_is_preserved(self):
        # Setting the window must not silently re-quantise the model or undo the
        # user's offload and speculative-decoding choices.
        _, posts = self._reload()
        payload = posts[0][1]
        self.assertEqual(payload["gguf_variant"], "UD-Q6_K")
        self.assertEqual(payload["cache_type_kv"], "q8_0")
        self.assertEqual(payload["speculative_type"], "auto")
        self.assertEqual(payload["gpu_memory_mode"], "auto")

    def test_the_reload_asks_for_a_single_decode_slot(self):
        # llama-server divides its KV budget across --parallel slots, and JSE
        # holds the endpoint to one in-flight request anyway.
        _, posts = self._reload()
        self.assertEqual(posts[0][1]["n_parallel"], 1)

    def test_an_active_generation_is_never_cancelled(self):
        _, posts = self._reload()
        self.assertNotEqual(posts[0][1].get("force_cancel_active"), True)

    def test_a_busy_server_is_reported_as_busy(self):
        def busy():
            raise providers.LLMHTTPError(409, "409 Conflict", "generation in flight")

        with self.assertRaises(Exception) as caught:
            self._reload(post=busy)
        self.assertIn("mid-generation", str(caught.exception))

    def test_the_served_window_is_reported_not_the_requested_one(self):
        # The fitter caps the ask to what the hardware holds.
        result, _ = self._reload(32768, after={"context_length": 16384})
        self.assertEqual(result["context_length"], 16384)

    def test_a_stale_window_is_not_served_from_cache_after_a_reload(self):
        providers._local_context_cache[(LOCAL["base_url"], LOCAL["model"])] = (float("inf"), 4096)
        self._reload(32768)
        self.assertEqual(providers._local_context_cache, {})

    def test_nothing_loaded_is_an_explained_refusal(self):
        with mock.patch.object(providers, "local_status", return_value={}):
            with self.assertRaises(ValueError):
                providers.set_local_context_window(32768, local={**LOCAL, "model": ""})


class AutoReloadTests(unittest.TestCase):
    def setUp(self):
        providers._local_reload_attempted.clear()
        self.addCleanup(providers._local_reload_attempted.clear)

    def test_a_request_that_does_not_fit_triggers_one_reload(self):
        with mock.patch.object(providers, "_local_context_length", return_value=4096), \
             mock.patch.object(providers, "set_local_context_window",
                               return_value={"context_length": 32768}) as reload:
            first = providers._ensure_local_context(dict(LOCAL), needed=20000)
            second = providers._ensure_local_context(dict(LOCAL), needed=20000)
        self.assertEqual(first, 32768)
        self.assertEqual(reload.call_count, 1, "a hardware-capped window must not be retried per request")
        self.assertEqual(second, 4096, "the second call reads the cached window rather than reloading again")

    def test_a_request_that_fits_reloads_nothing(self):
        with mock.patch.object(providers, "_local_context_length", return_value=32768), \
             mock.patch.object(providers, "set_local_context_window") as reload:
            providers._ensure_local_context(dict(LOCAL), needed=8000)
        reload.assert_not_called()

    def test_auto_reload_can_be_turned_off(self):
        with mock.patch.object(providers, "_local_context_length", return_value=4096), \
             mock.patch.object(providers, "set_local_context_window") as reload:
            context = providers._ensure_local_context({**LOCAL, "auto_reload": False}, needed=20000)
        reload.assert_not_called()
        self.assertEqual(context, 4096)

    def test_a_failed_reload_does_not_take_the_call_down_with_it(self):
        # The fit check below still reports the too-small window; a reload that
        # could not happen is not itself the error worth raising.
        with mock.patch.object(providers, "_local_context_length", return_value=4096), \
             mock.patch.object(providers, "set_local_context_window", side_effect=Exception("busy")):
            self.assertEqual(providers._ensure_local_context(dict(LOCAL), needed=20000), 4096)


class PromptCountingTests(unittest.TestCase):
    def test_the_servers_own_tokenizer_is_preferred(self):
        with mock.patch.object(providers, "_post_json", return_value={"input_tokens": 2452}):
            self.assertEqual(
                providers._count_local_prompt_tokens(dict(LOCAL), [{"role": "user", "content": "x" * 15000}]),
                2452,
            )

    def test_an_endpoint_without_a_token_counter_falls_back(self):
        with mock.patch.object(providers, "_post_json", side_effect=OSError("404")):
            self.assertIsNone(providers._count_local_prompt_tokens(dict(LOCAL), [{"role": "user", "content": "hi"}]))


class TruncationTests(unittest.TestCase):
    def _call(self, response, json_mode=False, max_tokens=2000, context=4096):
        with mock.patch.object(providers, "_post_json", return_value=response), \
             mock.patch.object(providers, "_local_ai_settings", return_value=dict(LOCAL)), \
             mock.patch.object(providers, "_count_local_prompt_tokens", return_value=30), \
             mock.patch.object(providers, "_ensure_local_context", return_value=context), \
             mock.patch.object(providers, "_analysis_worker_count", return_value=1), \
             mock.patch.object(providers, "_local_lock_path", return_value=None):
            return providers._call_unsloth(
                [{"role": "user", "content": "hi"}], max_tokens=max_tokens, json_mode=json_mode
            )

    @staticmethod
    def _truncated(content, total=4096):
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": total - 30, "total_tokens": total},
        }

    def test_a_truncated_json_response_raises_instead_of_returning_half_an_object(self):
        with self.assertRaises(providers.LLMTruncatedError):
            self._call(self._truncated('{"score": 7, "rea'), json_mode=True)

    def test_the_truncation_error_names_the_window_when_the_window_is_the_limit(self):
        with self.assertRaises(providers.LLMTruncatedError) as caught:
            self._call(self._truncated('{"score": 7, "rea'), json_mode=True)
        self.assertIn("4096", str(caught.exception))

    def test_a_truncation_error_is_not_rewrapped_as_an_unexpected_error(self):
        with self.assertRaises(providers.LLMTruncatedError) as caught:
            self._call(self._truncated('{"a": 1'), json_mode=True)
        self.assertNotIn("Unexpected error", str(caught.exception))

    def test_truncated_free_text_is_kept(self):
        # A shortened paragraph is still usable; only structured output isn't.
        self.assertEqual(self._call(self._truncated("a partial sentence")), "a partial sentence")

    def test_a_complete_response_is_returned_normally(self):
        response = {"choices": [{"message": {"content": "all done"}, "finish_reason": "stop"}]}
        self.assertEqual(self._call(response), "all done")

    def test_the_request_sent_fits_the_window(self):
        seen = {}

        def fake_post(_url, _headers, payload, timeout=None):
            seen.update(payload)
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        with mock.patch.object(providers, "_post_json", fake_post), \
             mock.patch.object(providers, "_local_ai_settings", return_value=dict(LOCAL)), \
             mock.patch.object(providers, "_count_local_prompt_tokens", return_value=30), \
             mock.patch.object(providers, "_ensure_local_context", return_value=4096), \
             mock.patch.object(providers, "_analysis_worker_count", return_value=1), \
             mock.patch.object(providers, "_local_lock_path", return_value=None):
            providers._call_unsloth([{"role": "user", "content": "hi"}], max_tokens=16384)

        self.assertLess(seen["max_tokens"], 4096)


class ReasoningToggleTests(unittest.TestCase):
    """Structured calls must not spend their token budget on reasoning.

    Qwen3.6 ignores the `/no_think` token Qwen3 honoured. Measured against
    Unsloth Studio, a triage prompt put its whole 700-token budget into
    reasoning_content and returned empty content with finish_reason "length";
    the same prompt with enable_thinking=false answered in 40 tokens. A
    truncated structured call is not a smaller answer — it raises, and the
    triage handler falls open to the full analysis it exists to avoid.
    """

    def setUp(self):
        providers._local_reasoning_cache.clear()
        providers._local_context_cache.clear()
        self.addCleanup(providers._local_reasoning_cache.clear)
        self.addCleanup(providers._local_context_cache.clear)

    def _send(self, status, json_mode=True):
        """Return the payload _call_unsloth puts on the wire."""
        seen = {}

        def fake_post(_url, _headers, payload, timeout=None):
            seen.update(payload)
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}

        with mock.patch.object(providers, "_post_json", fake_post), \
             mock.patch.object(providers, "local_status", return_value=status), \
             mock.patch.object(providers, "_local_ai_settings", return_value=dict(LOCAL)), \
             mock.patch.object(providers, "_local_prompt_tokens", return_value=30), \
             mock.patch.object(providers, "_ensure_local_context", return_value=32768), \
             mock.patch.object(providers, "_analysis_worker_count", return_value=1), \
             mock.patch.object(providers, "_local_lock_path", return_value=None):
            providers._call_unsloth([{"role": "user", "content": "score this"}],
                                    max_tokens=500, json_mode=json_mode)
        return seen

    def _last_user(self, payload):
        return [m for m in payload["messages"] if m["role"] == "user"][-1]["content"]

    def test_an_endpoint_that_advertises_the_toggle_gets_it(self):
        payload = self._send({"supports_reasoning": True, "reasoning_style": "enable_thinking"})
        self.assertEqual(payload.get("chat_template_kwargs"), {"enable_thinking": False})
        self.assertNotIn("/no_think", self._last_user(payload),
                         "the token is redundant once the toggle is understood")

    def test_an_endpoint_without_the_toggle_keeps_the_token(self):
        payload = self._send({"supports_reasoning": False})
        self.assertIsNone(payload.get("chat_template_kwargs"))
        self.assertIn("/no_think", self._last_user(payload))

    def test_an_endpoint_that_cannot_disable_reasoning_is_left_alone(self):
        payload = self._send({
            "supports_reasoning": True,
            "reasoning_style": "enable_thinking",
            "reasoning_always_on": True,
        })
        self.assertIsNone(payload.get("chat_template_kwargs"),
                          "sending a toggle the server cannot honour just risks a 400")
        self.assertIn("/no_think", self._last_user(payload))

    def test_free_text_calls_keep_their_reasoning(self):
        payload = self._send({"supports_reasoning": True, "reasoning_style": "enable_thinking"},
                             json_mode=False)
        self.assertIsNone(payload.get("chat_template_kwargs"))
        self.assertNotIn("/no_think", self._last_user(payload))

    def test_the_capability_is_cached_not_probed_per_request(self):
        calls = []

        def counting_status(local=None):
            calls.append(local)
            return {"supports_reasoning": True, "reasoning_style": "enable_thinking"}

        with mock.patch.object(providers, "local_status", counting_status):
            for _ in range(5):
                providers._local_reasoning_style(dict(LOCAL))
        self.assertEqual(len(calls), 1, "a /v1/status round trip per call would undo the saving")

    def test_a_model_reload_invalidates_the_capability(self):
        providers._local_reasoning_cache[("x", "y")] = (float("inf"), "enable_thinking")
        with mock.patch.object(providers, "_post_json", return_value={}), \
             mock.patch.object(providers, "local_status", return_value={}), \
             mock.patch.object(providers, "_local_ai_settings", return_value=dict(LOCAL)):
            providers.set_local_context_window(32768, local=dict(LOCAL))
        self.assertEqual(providers._local_reasoning_cache, {},
                         "reasoning support belongs to the model, which a reload can swap")


class PromptTokenCountingTests(unittest.TestCase):
    """The exact count costs a round trip carrying the whole prompt.

    It exists to answer one question — does this request fit? — so a request
    that clearly fits should not pay for it. On a sweep that is one wasted
    transfer and tokenization per job.
    """

    def setUp(self):
        providers._local_context_cache.clear()
        self.addCleanup(providers._local_context_cache.clear)

    def _tokens(self, messages, max_tokens, context, exact=None):
        calls = []

        def fake_count(local, msgs):
            calls.append(msgs)
            return exact

        with mock.patch.object(providers, "_local_context_length", return_value=context), \
             mock.patch.object(providers, "_count_local_prompt_tokens", fake_count):
            result = providers._local_prompt_tokens(dict(LOCAL), messages, max_tokens)
        return result, calls

    def test_a_request_that_clearly_fits_skips_the_tokenizer_round_trip(self):
        messages = [{"role": "user", "content": "x" * 4000}]  # ~1k tokens
        result, calls = self._tokens(messages, max_tokens=2048, context=32768)
        self.assertEqual(calls, [], "should not have asked the server to count")
        self.assertEqual(result, providers._estimate_prompt_tokens(messages))

    def test_a_request_near_the_window_pays_for_the_exact_count(self):
        # ~15.8k estimated tokens: doubled, plus the output budget, this no
        # longer fits 32768, so the fit is genuinely in doubt.
        messages = [{"role": "user", "content": "x" * 60000}]
        result, calls = self._tokens(messages, max_tokens=6000, context=32768, exact=9000)
        self.assertEqual(len(calls), 1, "a borderline request must be counted exactly")
        self.assertEqual(result, 9000)

    def test_an_endpoint_that_hides_its_window_is_counted_exactly(self):
        messages = [{"role": "user", "content": "hi"}]
        _, calls = self._tokens(messages, max_tokens=512, context=None, exact=12)
        self.assertEqual(len(calls), 1)

    def test_a_failed_exact_count_falls_back_to_the_estimate(self):
        messages = [{"role": "user", "content": "x" * 60000}]
        result, calls = self._tokens(messages, max_tokens=6000, context=32768, exact=None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, providers._estimate_prompt_tokens(messages))

    def test_the_margin_covers_an_estimate_that_undercounts(self):
        # The skip is only safe while double the estimate still fits; at exactly
        # the boundary the exact count must still be taken.
        messages = [{"role": "user", "content": "x" * 20000}]
        estimated = providers._estimate_prompt_tokens(messages)
        max_tokens = 1000
        boundary = (estimated * providers.LOCAL_ESTIMATE_SAFETY_FACTOR) + max_tokens + providers.LOCAL_CONTEXT_RESERVE
        _, skipped = self._tokens(messages, max_tokens, context=boundary, exact=estimated)
        self.assertEqual(skipped, [], "at the boundary the estimate is still trusted")
        _, counted = self._tokens(messages, max_tokens, context=boundary - 1, exact=estimated)
        self.assertEqual(len(counted), 1, "one token past the boundary it must be counted")


if __name__ == "__main__":
    unittest.main()
