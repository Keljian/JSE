"""Progress reporting for the long-running search and analysis tasks.

The UI's status indicator is only as good as the frames underneath it, and the
failure mode is silent: a callback that stops being threaded through leaves the
bar stuck at zero while the task runs fine, which nothing else would catch.

These cover the three things that must hold:

1. ProgressReporter coalesces chatter but never drops a frame that changes what
   the UI shows (first, phase change, final).
2. The analysis loop reports every completed job in *both* its branches. The
   serial branch had no counter at all before this.
3. The callback survives the trip through app_logic and the bridge commands.
"""
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_logic  # noqa: E402
import llm.analysis as analysis  # noqa: E402
from bridge import runtime  # noqa: E402


class ProgressReporterTests(unittest.TestCase):
    def _capture(self):
        frames = []
        # First parameter is deliberately not named `kind`: the reporter passes
        # its own kind= keyword and would collide with it.
        return frames, mock.patch.object(runtime, "emit", lambda event_type, **payload: frames.append(payload))

    def test_intermediate_frames_are_coalesced(self):
        frames, patched = self._capture()
        with patched:
            reporter = runtime.ProgressReporter("analysis", min_interval=5)
            for index in range(0, 10):
                reporter(index, 20, phase="analysing")
        # First frame always goes out; the rest fall inside the throttle window.
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["current"], 0)

    def test_final_frame_is_never_throttled_away(self):
        frames, patched = self._capture()
        with patched:
            reporter = runtime.ProgressReporter("analysis", min_interval=5)
            reporter(0, 3, phase="analysing")
            reporter(1, 3, phase="analysing")
            reporter(3, 3, phase="analysing")
        self.assertEqual([frame["current"] for frame in frames], [0, 3])

    def test_phase_change_is_never_throttled_away(self):
        frames, patched = self._capture()
        with patched:
            reporter = runtime.ProgressReporter("search", min_interval=5)
            reporter(0, None, phase="preparing")
            reporter(0, 8, phase="scraping")
            reporter(1, 8, phase="scraping")
        self.assertEqual([frame["phase"] for frame in frames], ["preparing", "scraping"])

    def test_frames_carry_kind_and_extra_context(self):
        frames, patched = self._capture()
        with patched:
            reporter = runtime.ProgressReporter("search", extra={"lane": "Ops", "lane_index": 2, "lane_count": 3})
            reporter(0, 4, phase="scraping", detail="seek")
        frame = frames[0]
        self.assertEqual(frame["kind"], "search")
        self.assertEqual(frame["lane"], "Ops")
        self.assertEqual((frame["lane_index"], frame["lane_count"]), (2, 3))
        self.assertEqual(frame["detail"], "seek")

    def test_indeterminate_total_is_passed_through_not_faked(self):
        frames, patched = self._capture()
        with patched:
            runtime.ProgressReporter("search")(0, None, phase="preparing")
        self.assertIsNone(frames[0]["total"])


class _AnalysisLoopHarness:
    """Patches away everything _perform_analysis_loop needs but does not test."""

    def __enter__(self):
        self._patches = [
            mock.patch.object(analysis, "_get_resume_triage_summary", return_value="summary"),
            mock.patch.object(analysis, "_analysis_preferences", return_value="prefs"),
            mock.patch.object(analysis, "with_doctrine", side_effect=lambda prompt, _settings: prompt),
            mock.patch.object(analysis, "lane_brief", return_value="brief"),
            mock.patch.object(analysis, "_format_fragment_context", return_value=""),
            mock.patch.object(analysis.db, "get_lane_settings", return_value={}),
            mock.patch.object(analysis.db, "get_lane_fragments", return_value=[]),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()
        return False


class AnalysisLoopProgressTests(unittest.TestCase):
    def _run(self, job_count, workers, analyze=None):
        jobs = [{"id": index} for index in range(job_count)]
        seen = []
        with _AnalysisLoopHarness():
            with mock.patch.object(analysis, "_analysis_worker_count", return_value=workers), \
                 mock.patch.object(analysis, "_analyze_single_job", side_effect=analyze or (lambda job, ctx: None)):
                analysis._perform_analysis_loop(
                    jobs, "resume", "prompt", lambda message: None, profile_id=1, fragments=[],
                    progress_callback=lambda current, total, **fields: seen.append((current, total, fields)),
                )
        return seen

    def test_serial_branch_reports_every_job(self):
        seen = self._run(job_count=3, workers=1)
        self.assertEqual([(current, total) for current, total, _ in seen],
                         [(0, 3), (1, 3), (2, 3), (3, 3)])

    def test_parallel_branch_reports_every_job(self):
        seen = self._run(job_count=6, workers=4)
        currents = [current for current, _, _ in seen]
        self.assertEqual(currents[0], 0)
        self.assertEqual(currents[-1], 6)
        self.assertEqual(sorted(currents), currents, "progress must not go backwards")
        self.assertTrue(all(total == 6 for _, total, _ in seen))

    def test_serial_branch_counts_failures_and_keeps_going(self):
        def analyze(job, ctx):
            if job["id"] == 1:
                raise RuntimeError("boom")

        seen = self._run(job_count=3, workers=1, analyze=analyze)
        self.assertEqual(seen[-1][0], 3, "a failed job must still advance the bar")
        self.assertEqual(seen[-1][2]["failed"], 1)

    def test_parallel_branch_counts_failures_and_keeps_going(self):
        def analyze(job, ctx):
            if job["id"] == 2:
                raise RuntimeError("boom")

        seen = self._run(job_count=4, workers=3, analyze=analyze)
        self.assertEqual(seen[-1][0], 4)
        self.assertEqual(seen[-1][2]["failed"], 1)

    def test_empty_batch_reports_nothing(self):
        self.assertEqual(self._run(job_count=0, workers=4), [])

    def test_loop_runs_without_a_progress_callback(self):
        jobs = [{"id": 0}, {"id": 1}]
        with _AnalysisLoopHarness():
            with mock.patch.object(analysis, "_analysis_worker_count", return_value=1), \
                 mock.patch.object(analysis, "_analyze_single_job", return_value=None):
                analysis._perform_analysis_loop(jobs, "resume", "prompt", None, profile_id=1, fragments=[])


class CallbackPlumbingTests(unittest.TestCase):
    """A progress_callback dropped anywhere in the chain silently flatlines."""

    def test_every_layer_accepts_a_progress_callback(self):
        for label, func in [
            ("app_logic.execute_scraping_and_analysis", app_logic.execute_scraping_and_analysis),
            ("app_logic.run_analysis_on_existing", app_logic.run_analysis_on_existing),
            ("app_logic.run_analysis_on_specific_jobs", app_logic.run_analysis_on_specific_jobs),
            ("analysis.analyze_jobs", analysis.analyze_jobs),
            ("analysis.analyze_specific_jobs", analysis.analyze_specific_jobs),
            ("analysis._perform_analysis_loop", analysis._perform_analysis_loop),
        ]:
            self.assertIn("progress_callback", inspect.signature(func).parameters, label)

    def test_app_logic_forwards_the_callback_to_the_analysis_layer(self):
        marker = object()
        with mock.patch.object(app_logic.llm_handler, "analyze_jobs") as analyze:
            app_logic.run_analysis_on_existing("resume", False, "new", lambda m: None, 1, progress_callback=marker)
        self.assertIs(analyze.call_args.kwargs["progress_callback"], marker)

        with mock.patch.object(app_logic.llm_handler, "analyze_specific_jobs") as analyze_specific:
            app_logic.run_analysis_on_specific_jobs([1], "resume", lambda m: None, 1, progress_callback=marker)
        self.assertIs(analyze_specific.call_args.kwargs["progress_callback"], marker)

    def test_bridge_commands_build_reporters_for_search_and_analysis(self):
        from bridge import jobs as jobs_commands
        from bridge import scrapers as scraper_commands
        self.assertIn("ProgressReporter", jobs_commands.command_analysis_run.__globals__)
        self.assertIn("ProgressReporter", scraper_commands.command_scrape_run.__globals__)
        for source, command in [
            (Path(jobs_commands.__file__).read_text(encoding="utf-8"), "analysis"),
            (Path(scraper_commands.__file__).read_text(encoding="utf-8"), "search"),
        ]:
            self.assertIn(f'ProgressReporter("{command}"', source)


class SearchProgressTests(unittest.TestCase):
    def test_scrape_reports_indeterminate_before_the_fan_out_is_known(self):
        """A scrape has no total until keywords x sources is queued.

        Reporting a total there would mean inventing one and then correcting it,
        so the first frame carries total=None and the UI shows an indeterminate
        bar instead.
        """
        seen = []
        with mock.patch.object(app_logic.db, "dedupe_database"), \
             mock.patch.object(app_logic.scraper_plugins, "plugin_mode", return_value="keyword"), \
             mock.patch.object(app_logic, "_run_scraper_task",
                               side_effect=lambda source, keyword, *args, **kwargs: {"source": source, "keyword": keyword, "success": True}), \
             mock.patch.object(app_logic.db, "mark_missing_new_jobs_after_sweep", return_value={}):
            app_logic.execute_scraping_and_analysis(
                ["ops manager", "operations lead"],
                ["seek", "indeed"],
                "",  # no resume text: skips the LLM retry pass
                lambda message, progress=False: None,
                lambda message: None,
                # Truthy so the lane-settings lookup is skipped: this test is
                # about the progress frames, not the settings load.
                search_settings={"preferred_location": "Melbourne", "work_modes": ["remote"]},
                progress_callback=lambda current, total, **fields: seen.append((current, total, fields.get("phase"))),
            )

        self.assertEqual(seen[0], (0, None, "preparing"))
        # 2 keywords x 2 sources, and the total is final once they are queued.
        scraping = [frame for frame in seen if frame[2] == "scraping"]
        self.assertTrue(scraping)
        self.assertTrue(all(total == 4 for _, total, _ in scraping))
        self.assertEqual(scraping[-1][0], 4)
        self.assertEqual(seen[-1][2], "finishing")


if __name__ == "__main__":
    unittest.main()
