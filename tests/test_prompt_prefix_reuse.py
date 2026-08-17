"""Locks the block ordering that makes KV-cache prefix reuse possible.

A local server reuses the KV cache for the longest token prefix it shares with
the preceding request. Every scoring prompt therefore has to put the blocks
that are identical across a sweep (instruction, lane weighting, lane brief,
resume) before the blocks that change per job (title, extracted requirements,
the ad itself). One stable block placed below a varying one strands everything
above it and forces a full prefill on every call.

Measured on the 17 Aug 2026 export, moving the job title out of the opening
line of the triage prompt took the reusable prefix from ~25 tokens to ~734,
which is 36% of the prompt instead of 1.3%. Triage runs on every job in a
sweep, so this is the highest-leverage ordering in the codebase.

These tests read the prompt templates from source rather than calling the
functions, because building a prompt for real needs a database and a resume.
They assert relative position only — wording is free to change.
"""
import inspect
import re
from unittest import mock

import pytest

from llm import analysis

MARKER = "--- BEGIN ROLE UNDER ASSESSMENT ---"

# (function, blocks that must precede the marker, blocks that must follow it)
ORDERED_PROMPTS = [
    pytest.param(
        analysis._triage_job,
        ["PROFILE PREFERENCE WEIGHTING:", "COMPACT RESUME SUMMARY:"],
        ["JOB TITLE:", "MANDATORY REQUIREMENT LINES", "FULL JOB ADVERTISEMENT:"],
        id="triage",
    ),
    pytest.param(
        analysis._run_deep_gatekeeper,
        ["COMPACT RESUME SUMMARY:", "PROFILE PREFERENCE WEIGHTING:", "RESUME EXTRACT:"],
        ["FULL ANALYSIS JSON:", "JOB DESCRIPTION:"],
        id="deep_gatekeeper",
    ),
]


@pytest.mark.parametrize("func,stable,per_job", ORDERED_PROMPTS)
def test_stable_blocks_precede_per_job_blocks(func, stable, per_job):
    source = inspect.getsource(func)
    assert source.count(MARKER) == 1, f"{func.__name__} must carry exactly one ordering marker"
    split = source.index(MARKER)
    for block in stable:
        assert block in source, f"{block} missing from {func.__name__}"
        assert source.index(block) < split, (
            f"{block} is a stable block and must sit ABOVE the marker in "
            f"{func.__name__}, otherwise it is recomputed on every call"
        )
    for block in per_job:
        assert block in source, f"{block} missing from {func.__name__}"
        assert source.index(block) > split, (
            f"{block} changes per job and must sit BELOW the marker in "
            f"{func.__name__}, otherwise it truncates the reusable prefix"
        )


@pytest.mark.parametrize("func,stable,per_job", ORDERED_PROMPTS)
def test_no_interpolation_before_the_first_stable_heading(func, stable, per_job):
    """Nothing job-specific may be interpolated ahead of the first stable block.

    A single `{job_title}` in the opening line is enough to cut the reusable
    prefix down to the handful of tokens before it.
    """
    source = inspect.getsource(func)
    head = source[: source.index(stable[0])]
    prompt_head = head[head.index('user_prompt = f"""'):] if 'user_prompt = f"""' in head else ""
    assert not re.search(r"\{[a-z_]+", prompt_head), (
        f"{func.__name__} interpolates a value before its first stable block; "
        "move it below the ordering marker"
    )


def test_full_analysis_keeps_the_resume_ahead_of_the_advertisement():
    """The full-analysis prompt was already correctly ordered — keep it that way.

    It has no marker because its per-job blocks (aligned fragments, triage
    flags) legitimately sit between the resume and the ad. What matters is only
    that the resume and preference blocks stay above the advertisement.

    Scoped to _analysis_phase, which is where this prompt is actually built.
    Falling back to a whole-module scan instead would compare the first
    "CANDIDATE RESUME:" against the first "JOB ADVERTISEMENT:" anywhere in the
    file — headings belonging to two different prompts — and the result would
    say nothing about either one's ordering.
    """
    source = inspect.getsource(analysis._analysis_phase)
    assert "CANDIDATE RESUME:" in source, "full-analysis prompt moved out of _analysis_phase"
    assert source.index("CANDIDATE RESUME:") < source.index("JOB ADVERTISEMENT:")
    assert source.index("PROFILE PREFERENCE WEIGHTING:") < source.index("JOB ADVERTISEMENT:")


def test_relevance_gate_keeps_the_resume_first():
    source = inspect.getsource(analysis.check_job_relevance)
    assert source.index("CANDIDATE RESUME:") < source.index("JOB ADVERTISEMENT:")


LANE = {"boost_terms": "manufacturing; agtech", "penalty_terms": "helpdesk"}
WEIGHTING = "Add weight when present: manufacturing; agtech"


def _render_triage_prompt(**kwargs):
    """Capture the user prompt _triage_job builds, without calling a model."""
    captured = {}

    def fake_call(messages, **_kwargs):
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return '{"match_score": 50, "reason": "r", "keep": true, "flags": []}'

    with mock.patch.object(analysis, "_call_scoring_ai", fake_call):
        analysis._triage_job("TARGET ROLE FAMILIES: ops", "An advertisement.", "Ops Manager",
                             1, lambda message: None, lane_settings=dict(LANE), **kwargs)
    return captured["user"]


def test_the_preference_block_appears_once_not_twice():
    """It used to be rendered in its own block AND concatenated onto the summary.

    The summary is capped at 2200 characters and a resume summary runs to about
    that, so the duplicate was usually truncated mid-sentence as well as wasted.
    """
    with mock.patch.object(analysis.db, "get_lane_settings", return_value=dict(LANE)):
        rendered_preferences = analysis._analysis_preferences(1)
    assert WEIGHTING in rendered_preferences  # guards the fixture, not the code
    prompt = _render_triage_prompt(preference_context=rendered_preferences)
    assert prompt.count(WEIGHTING) == 1
    summary_block = prompt.split("COMPACT RESUME SUMMARY:")[1].split("---")[1]
    assert WEIGHTING not in summary_block, "preference text leaked back into the resume block"


def test_a_supplied_preference_context_costs_no_database_read():
    """The sweep resolves this once; re-reading lane settings per job is waste."""
    with mock.patch.object(analysis.db, "get_lane_settings") as read:
        _render_triage_prompt(preference_context="PREFS GO HERE")
    assert not read.called, "_triage_job re-read lane settings despite being handed both"


def test_a_standalone_caller_still_gets_its_preferences():
    """Callers holding a single job pass nothing and must still work."""
    with mock.patch.object(analysis, "_analysis_preferences", return_value="RESOLVED PREFS") as resolve:
        prompt = _render_triage_prompt()
    assert resolve.called
    assert "RESOLVED PREFS" in prompt


def test_the_triage_contract_asks_for_one_short_sentence():
    """Triage output is read at a glance beside a number, not as a report."""
    from llm.prompts import TRIAGE_SYSTEM_PROMPT_BASE
    shape = TRIAGE_SYSTEM_PROMPT_BASE.split("REQUIRED JSON SHAPE")[1]
    reason = next(line for line in shape.splitlines() if '"reason"' in line)
    assert "25 words" in reason, "the reason field lost its length cap"
    assert "1-2 sentences" not in reason
