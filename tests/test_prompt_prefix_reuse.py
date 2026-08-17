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

    Scoped to _analyze_single_job, which is where this prompt is actually
    built. Falling back to a whole-module scan instead would compare the first
    "CANDIDATE RESUME:" against the first "JOB ADVERTISEMENT:" anywhere in the
    file — headings belonging to two different prompts — and the result would
    say nothing about either one's ordering.
    """
    source = inspect.getsource(analysis._analyze_single_job)
    assert "CANDIDATE RESUME:" in source, "full-analysis prompt moved out of _analyze_single_job"
    assert source.index("CANDIDATE RESUME:") < source.index("JOB ADVERTISEMENT:")
    assert source.index("PROFILE PREFERENCE WEIGHTING:") < source.index("JOB ADVERTISEMENT:")


def test_relevance_gate_keeps_the_resume_first():
    source = inspect.getsource(analysis.check_job_relevance)
    assert source.index("CANDIDATE RESUME:") < source.index("JOB ADVERTISEMENT:")
