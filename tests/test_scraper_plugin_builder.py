"""Regression tests for resilient local-LLM scraper generation."""
import json
import unittest
from unittest.mock import patch

import scraper_plugin_builder as builder


VALID_CODE = """\
def scrape(keyword, status_callback=None, log_callback=None, profile_id=1,
           base_url="", company_name="", location="", max_pages=1,
           dry_run=False, **config):
    if dry_run:
        return {"ok": True, "found": 1, "sample_jobs": [], "warnings": []}
    return False
"""


class ScraperPluginBuilderTests(unittest.TestCase):
    def test_extracts_complete_object_without_greedy_prose_braces(self):
        response = 'Reason {not JSON}. Result: {"manifest": {}, "scraper_code": "pass"} trailing {noise}'
        data = builder._extract_json_object(response)
        self.assertEqual("pass", data["scraper_code"])

    def test_extracts_double_encoded_json(self):
        payload = {"manifest": {}, "scraper_code": VALID_CODE}
        self.assertEqual(payload, builder._extract_json_object(json.dumps(json.dumps(payload))))

    def test_accepts_python_dict_from_small_model(self):
        response = "{'manifest': {}, 'scraper_code': 'pass', 'notes': []}"
        self.assertEqual("pass", builder._extract_json_object(response)["scraper_code"])

    def test_empty_response_has_actionable_lm_studio_error(self):
        with self.assertRaisesRegex(ValueError, "empty response.*LM Studio chat template"):
            builder._extract_json_object("")

    def test_python_fallback_accepts_fenced_source(self):
        self.assertEqual(VALID_CODE.strip(), builder._extract_python_code(f"```python\n{VALID_CODE}```"))

    def test_validator_accepts_decorated_function_alias(self):
        code = VALID_CODE.replace("def scrape(", "def _scrape_inner(") + "\nscrape = _scrape_inner\n"
        self.assertTrue(builder._validate_code(code))

    def test_python_generation_fallback_disables_json_mode(self):
        answers = {"source_name": "Example", "careers_url": "https://example.test/jobs"}
        with patch.object(builder.llm_handler, "_call_unsloth", return_value=VALID_CODE) as call, \
             patch.object(builder.scraper_plugins, "validate_manifest"):
            generated = builder._generate_once(answers, {}, output_mode="python")
        self.assertEqual(VALID_CODE.strip(), generated["scraper_code"])
        self.assertFalse(call.call_args.kwargs["json_mode"])
        self.assertIn("Return ONLY complete Python source", call.call_args.args[0][1]["content"])

    def test_build_stages_formats_before_installing_fallback(self):
        answers = {"source_name": "Example", "careers_url": "https://example.test/jobs"}
        generated = {
            "manifest": {"id": "example", "name": "Example"},
            "scraper_code": VALID_CODE,
            "notes": [],
            "test_plan": [],
        }
        with patch.object(builder, "_existing_plugin_path", return_value=None), \
             patch.object(builder, "_reconnoitre", return_value={}), \
             patch.object(
                 builder,
                 "_generate_once",
                 side_effect=[ValueError("empty"), ValueError("bad JSON"), generated],
             ) as generate, \
             patch.object(builder, "_write_candidate", return_value="example"), \
             patch.object(builder, "test_plugin", return_value={"ok": True, "result": {"found": 1}}), \
             patch.object(builder, "save_generated_plugin", return_value=({"id": "example"}, "plugin-dir")):
            result = builder.build_and_install(answers, max_attempts=3)
        self.assertTrue(result["verified"])
        self.assertEqual(
            ["structured_json", "json_text", "python"],
            [call.kwargs["output_mode"] for call in generate.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
