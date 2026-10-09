"""Strata as the local endpoint.

Strata (github.com/Niko1221/Strata) serves an OpenAI-compatible API but
describes itself differently from Unsloth Studio: /v1/status says
{"service": "strata"} with no reasoning_style, /v1/models reports the served
window under meta.n_ctx, and there is no /v1/load to reload at another window.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm import providers  # noqa: E402


LOCAL = {
    "base_url": "http://127.0.0.1:8080/v1",
    "api_key": "",
    "model": "",
    "context_target": 32768,
    "auto_reload": True,
}

STRATA_STATUS = {"service": "strata", "model": "Qwen3.8-Flash-Next-IQ3_XXS", "context": {"native": 65536}}


class StrataEndpointTests(unittest.TestCase):
    def setUp(self):
        for cache in (providers._local_context_cache, providers._local_reasoning_cache):
            cache.clear()
            self.addCleanup(cache.clear)

    def test_context_window_is_read_from_meta_n_ctx(self):
        catalogue = {"object": "list", "data": [
            {"id": "Qwen3.8-Flash-Next-IQ3_XXS", "object": "model",
             "status": {"value": "loaded"}, "meta": {"n_ctx": 65536}},
        ]}
        with mock.patch.object(providers, "_get_json", return_value=catalogue):
            self.assertEqual(providers._local_context_length(LOCAL), 65536)

    def test_reasoning_is_switched_off_through_the_template_not_a_token(self):
        with mock.patch.object(providers, "local_status", return_value=STRATA_STATUS):
            payload = {}
            messages = [{"role": "user", "content": "Score this role."}]
            out = providers._suppress_reasoning(payload, messages, LOCAL)
        self.assertEqual(payload.get("chat_template_kwargs"), {"enable_thinking": False})
        self.assertNotIn("/no_think", out[-1]["content"])

    def test_context_reload_is_refused_with_a_pointer_to_setup(self):
        with mock.patch.object(providers, "local_status", return_value=STRATA_STATUS), \
             mock.patch.object(providers, "_post_json") as post:
            with self.assertRaises(Exception) as caught:
                providers.set_local_context_window(65536, local=LOCAL)
        post.assert_not_called()
        self.assertIn("Strata", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
