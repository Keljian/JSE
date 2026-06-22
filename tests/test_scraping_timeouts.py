"""Regression tests for bounded scraper detail extraction."""
import unittest
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException

import scraping_helpers


class _SwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        self.driver.current_handle = handle


class _Driver:
    def __init__(self):
        self.window_handles = ["main"]
        self.switch_to = _SwitchTo(self)
        self.current_url = "https://example.test/job"
        self.page_timeouts = []

    def execute_script(self, _script):
        self.window_handles.append("detail")

    def set_page_load_timeout(self, value):
        self.page_timeouts.append(value)

    def get(self, _url):
        return None

    def close(self):
        self.window_handles.pop()


class ScrapingTimeoutTests(unittest.TestCase):
    def test_detail_selector_probes_stop_at_per_job_deadline(self):
        clock = [0.0]
        logs = []

        class AdvancingWait:
            def __init__(self, _driver, timeout):
                self.timeout = timeout

            def until(self, _condition):
                clock[0] += self.timeout
                raise TimeoutException("not found")

        driver = _Driver()
        job = {"title": "Slow advert", "company": "Example", "url": "https://example.test/job"}
        with patch.object(scraping_helpers.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(scraping_helpers, "WebDriverWait", AdvancingWait), \
             patch.object(scraping_helpers.db, "add_job") as add_job:
            saved = scraping_helpers.scrape_job_details(
                driver, None, [job], logs.append, job_timeout_seconds=15
            )

        self.assertEqual(0, saved)
        add_job.assert_not_called()
        self.assertTrue(any("Timeout: skipped 'Slow advert' after 15 seconds" in line for line in logs))
        self.assertLessEqual(max(driver.page_timeouts), scraping_helpers.PAGE_LOAD_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
