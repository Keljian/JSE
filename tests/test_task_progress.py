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
import contextlib
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


def _phase_patches(workers, triage_fn=None, analysis_fn=None, gate_fn=None, order=None):
    """Patch the three phases, optionally recording the order they are called in."""
    def _record(name, fn, default):
        def wrapper(state, ctx):
            if order is not None:
                order.append((name, state["job_id"]))
            return (fn or default)(state, ctx)
        return wrapper

    reject = lambda state, ctx: False  # noqa: E731 - terse default for a test double
    return [
        mock.patch.object(analysis, "_analysis_worker_count", return_value=workers),
        mock.patch.object(analysis, "_prepare_job",
                          side_effect=lambda job, ctx: {"job": job, "job_id": job["id"]}),
        mock.patch.object(analysis, "_triage_phase", side_effect=_record("triage", triage_fn, reject)),
        mock.patch.object(analysis, "_analysis_phase", side_effect=_record("analysis", analysis_fn, reject)),
        mock.patch.object(analysis, "_gatekeeper_phase", side_effect=_record("gatekeeper", gate_fn, reject)),
    ]


class AnalysisLoopProgressTests(unittest.TestCase):
    def _run(self, job_count, workers, triage_fn=None, analysis_fn=None, gate_fn=None, order=None):
        jobs = [{"id": index} for index in range(job_count)]
        seen = []
        with _AnalysisLoopHarness():
            with contextlib.ExitStack() as stack:
                for patch in _phase_patches(workers, triage_fn, analysis_fn, gate_fn, order):
                    stack.enter_context(patch)
                analysis._perform_analysis_loop(
                    jobs, "resume", "prompt", lambda message: None, profile_id=1, fragments=[],
                    progress_callback=lambda current, total, **fields: seen.append((current, total, fields)),
                )
        return seen

    def test_jobs_rejected_at_triage_are_all_counted(self):
        seen = self._run(job_count=3, workers=1)
        self.assertEqual(seen[0][:2], (0, 3))
        self.assertEqual(seen[-1][:2], (3, 3))

    def test_survivors_are_counted_after_the_analysis_phase(self):
        seen = self._run(job_count=4, workers=1, triage_fn=lambda state, ctx: True)
        currents = [current for current, _, _ in seen]
        self.assertEqual(currents[0], 0)
        self.assertEqual(currents[-1], 4)
        self.assertEqual(sorted(currents), currents, "progress must not go backwards")

    def test_gated_jobs_are_counted_after_the_gatekeeper_phase(self):
        seen = self._run(
            job_count=4, workers=1,
            triage_fn=lambda state, ctx: True,
            analysis_fn=lambda state, ctx: state["job_id"] % 2 == 0,
        )
        self.assertEqual(seen[-1][:2], (4, 4))
        self.assertIn("gatekeeper", [fields.get("phase") for _, _, fields in seen])

    def test_progress_never_exceeds_the_total(self):
        seen = self._run(job_count=25, workers=1, triage_fn=lambda state, ctx: True)
        self.assertTrue(all(current <= total for current, total, _ in seen))
        self.assertEqual(seen[-1][:2], (25, 25))

    def test_parallel_phase_reports_every_job(self):
        seen = self._run(job_count=6, workers=4, triage_fn=lambda state, ctx: True)
        currents = [current for current, _, _ in seen]
        self.assertEqual(currents[-1], 6)
        self.assertEqual(sorted(currents), currents)

    def test_failures_are_counted_and_the_batch_keeps_going(self):
        def triage(state, ctx):
            if state["job_id"] == 1:
                raise RuntimeError("boom")
            return False

        seen = self._run(job_count=3, workers=1, triage_fn=triage)
        self.assertEqual(seen[-1][0], 3, "a failed job must still advance the bar")
        self.assertEqual(seen[-1][2]["failed"], 1)

    def test_parallel_failures_are_counted(self):
        def triage(state, ctx):
            if state["job_id"] == 2:
                raise RuntimeError("boom")
            return False

        seen = self._run(job_count=4, workers=3, triage_fn=triage)
        self.assertEqual(seen[-1][0], 4)
        self.assertEqual(seen[-1][2]["failed"], 1)

    def test_empty_batch_reports_nothing(self):
        self.assertEqual(self._run(job_count=0, workers=4), [])

    def test_loop_runs_without_a_progress_callback(self):
        jobs = [{"id": 0}, {"id": 1}]
        with _AnalysisLoopHarness():
            with contextlib.ExitStack() as stack:
                for patch in _phase_patches(1):
                    stack.enter_context(patch)
                analysis._perform_analysis_loop(jobs, "resume", "prompt", None, profile_id=1, fragments=[])


class PhaseBatchingTests(unittest.TestCase):
    """Prompts of one shape must run consecutively — the whole point of the split.

    A local server reuses the KV cache only against the immediately preceding
    request, so interleaving triage and analysis per job reuses nothing. These
    assert the call order, which is the only observable proof it still holds.
    """

    def _order(self, job_count, workers=1, **kwargs):
        order = []
        AnalysisLoopProgressTests()._run(job_count=job_count, workers=workers, order=order, **kwargs)
        return order

    def test_every_triage_in_a_chunk_precedes_the_first_analysis(self):
        order = self._order(job_count=analysis.ANALYSIS_PHASE_CHUNK, triage_fn=lambda state, ctx: True)
        phases = [name for name, _ in order]
        first_analysis = phases.index("analysis")
        self.assertEqual(
            phases[:first_analysis],
            ["triage"] * analysis.ANALYSIS_PHASE_CHUNK,
            "all triage prompts in a chunk must run before any analysis prompt",
        )

    def test_every_analysis_precedes_the_first_gatekeeper(self):
        order = self._order(
            job_count=6,
            triage_fn=lambda state, ctx: True,
            analysis_fn=lambda state, ctx: True,
        )
        phases = [name for name, _ in order]
        self.assertEqual(phases.count("gatekeeper"), 6)
        self.assertLess(max(i for i, name in enumerate(phases) if name == "analysis"),
                        min(i for i, name in enumerate(phases) if name == "gatekeeper"))

    def test_work_is_chunked_so_results_are_not_held_to_the_end(self):
        chunk = analysis.ANALYSIS_PHASE_CHUNK
        order = self._order(job_count=chunk * 2 + 3, triage_fn=lambda state, ctx: True)
        phases = [name for name, _ in order]
        # Three chunks: triage x N then analysis x N, repeated.
        groups = []
        for name in phases:
            if not groups or groups[-1][0] != name:
                groups.append([name, 0])
            groups[-1][1] += 1
        self.assertEqual(
            groups,
            [["triage", chunk], ["analysis", chunk],
             ["triage", chunk], ["analysis", chunk],
             ["triage", 3], ["analysis", 3]],
        )

    def test_the_single_job_entry_point_still_runs_every_phase_in_order(self):
        order = []
        with contextlib.ExitStack() as stack:
            for patch in _phase_patches(1, triage_fn=lambda s, c: True,
                                        analysis_fn=lambda s, c: True, order=order):
                stack.enter_context(patch)
            analysis._analyze_single_job({"id": 7}, {})
        self.assertEqual([name for name, _ in order], ["triage", "analysis", "gatekeeper"])


class TriagePhaseFallbackTests(unittest.TestCase):
    """The real _triage_phase, not a double — the fallback path has no other cover.

    When _triage_job raises, triage is supposed to fail open and let the job
    through to the full analysis. Every other test here patches the phases out,
    so a name that only exists on the success path would go unnoticed until a
    real sweep hit a triage error.
    """

    def _state(self):
        return {
            "job": {"id": 1}, "job_id": 1, "full_description": "ad text",
            "analysis_signature": "sig", "job_title": "Ops Manager",
            "triage_score": None, "flags": None,
        }

    def _ctx(self):
        return {
            "log": lambda message: None, "resume_summary": "summary",
            "preference_context": "prefs", "lane_target_text": "ops",
            "lane_settings": {}, "profile_id": 1,
        }

    def test_a_raising_triage_falls_through_to_the_full_analysis(self):
        state = self._state()
        with mock.patch.object(analysis, "_triage_job", side_effect=RuntimeError("endpoint down")):
            advanced = analysis._triage_phase(state, self._ctx())
        self.assertTrue(advanced, "a failed triage must fall open, not drop the job")
        self.assertIsNone(state["triage_score"])
        self.assertIsNone(state["flags"])

    def test_a_low_score_finalises_and_stops(self):
        state = self._state()
        with mock.patch.object(analysis, "_triage_job", return_value=(10, "no", False, None)), \
             mock.patch.object(analysis, "_apply_preference_weight", return_value=(10, [], [])), \
             mock.patch.object(analysis, "_persist_flags"), \
             mock.patch.object(analysis, "_band_block", return_value=""), \
             mock.patch.object(analysis.db, "update_job_analysis") as write, \
             mock.patch.object(analysis.db, "update_job_fragment_alignment"):
            advanced = analysis._triage_phase(state, self._ctx())
        self.assertFalse(advanced)
        self.assertTrue(write.called, "a rejected job must still store its triage result")

    def test_a_high_score_advances_without_writing(self):
        state = self._state()
        with mock.patch.object(analysis, "_triage_job", return_value=(90, "yes", True, None)), \
             mock.patch.object(analysis, "_apply_preference_weight", return_value=(90, [], [])), \
             mock.patch.object(analysis.db, "update_job_analysis") as write:
            advanced = analysis._triage_phase(state, self._ctx())
        self.assertTrue(advanced)
        self.assertFalse(write.called, "the analysis phase writes, not triage")
        self.assertEqual(state["triage_score"], 90)


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
