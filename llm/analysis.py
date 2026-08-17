"""The scoring chain: triage (which also raises flags), full analysis, deep gatekeeping.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
import concurrent.futures
import re
import screening
import hashlib
import concurrency
import database_manager as db
from .providers import (
    _analysis_worker_count,
    _call_scoring_ai,
    _local_ai_settings,
    _local_is_configured,
)
from .parsing import (
    _bullet_section,
    _coerce_list,
    _extract_json,
    _strip_image_references,
)
from .prompts import (
    ANALYSIS_SYSTEM_PROMPT_BASE,
    DEEP_GATEKEEPER_SYSTEM_PROMPT_BASE,
    FULL_ANALYSIS_TRIAGE_THRESHOLD,
    TRIAGE_KEEP_THRESHOLD,
    TRIAGE_SYSTEM_PROMPT_BASE,
    lane_brief,
    with_doctrine,
)

# A triage score below this is a genuine no, not a borderline call, and the
# lane-title rescue leaves it alone. Set under TRIAGE_KEEP_THRESHOLD on purpose:
# the retired-track cap the rescue exists to second-guess lands at 40, i.e.
# already below the keep floor, so a rescue gated on the keep floor could never
# fire for the only case that needed it.
TRIAGE_RESCUE_FLOOR = 30

def _format_gatekeeper_section(data, original_score, enforced_score=None):
    score = (max(0, min(100, int(enforced_score))) if enforced_score is not None
             else max(0, min(100, int(data.get("gate_score", original_score) or original_score))))
    decision = data.get("decision", "research_first")
    cap = data.get("score_cap")
    sections = [
        "Deep Gatekeeper Review:",
        f"- Decision: {decision}",
        f"- Gate Score: {score}%",
        f"- Original Full-Analysis Score: {original_score}%",
        f"- Score Cap Applied: {cap if cap is not None else 'None'}",
        f"- Confidence: {data.get('confidence', 'N/A')}",
        f"- Role Family: {data.get('role_family', 'N/A')}",
        f"- Seniority Fit: {data.get('seniority_fit', 'N/A')}",
        f"- Application ROI: {data.get('application_roi', 'N/A')}",
        f"- Application Angle: {data.get('application_angle', 'N/A')}",
        f"- Reason: {data.get('one_line_reason', 'N/A')}",
        "",
        _bullet_section("Gatekeeper Knockouts", data.get("knockout_reasons")),
        _bullet_section("False Positive Risks", data.get("false_positive_risks")),
        _bullet_section("Evidence Matches", data.get("evidence_matches")),
        _bullet_section("Missing / Weak Evidence", data.get("missing_or_weak_evidence")),
    ]
    return "\n".join(sections), score


def _run_deep_gatekeeper(resume_summary, resume_text, full_description, analysis_data, original_score,
                         profile_id, log, lane_settings=None):
    preference_context = _analysis_preferences(profile_id)
    if lane_settings is None:
        lane_settings = db.get_lane_settings(profile_id)
    # Same prefix-reuse rule as _triage_job: candidate-side blocks first and
    # contiguous, role-side blocks after the marker. The resume extract is the
    # largest stable block at ~9k characters and used to sit below the per-job
    # analysis JSON, which stranded it outside the reusable prefix. This path
    # only fires above the gatekeeper score threshold, so the volume is far
    # lower than triage, but the ordering costs nothing to get right.
    user_prompt = f"""Run a strict third-pass gatekeeper review.

Do not simply validate the prior score. Look for false positives and apply score caps aggressively.

COMPACT RESUME SUMMARY:
---
{resume_summary[:2200]}
---

PROFILE PREFERENCE WEIGHTING:
---
{preference_context}
---
{_lane_brief_block(lane_brief(lane_settings))}
RESUME EXTRACT:
---
{resume_text[:9000]}
---

--- BEGIN ROLE UNDER ASSESSMENT ---

FULL ANALYSIS JSON:
---
{json.dumps(analysis_data, ensure_ascii=False)[:4500]}
---

JOB DESCRIPTION:
---
{full_description[:10000]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": with_doctrine(DEEP_GATEKEEPER_SYSTEM_PROMPT_BASE, lane_settings)},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.05,
        max_tokens=4000,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        log(f"Deep gatekeeper response was not valid JSON. Keeping original score. Response: {response[:180]}...")
        return "", original_score

    gate_score = max(0, min(100, int(data.get("gate_score", original_score) or original_score)))
    decision = str(data.get("decision") or "").lower()
    cap = data.get("score_cap")
    if cap is not None:
        try:
            gate_score = min(gate_score, max(0, min(100, int(cap))))
        except (TypeError, ValueError):
            pass
    if decision == "reject":
        gate_score = min(gate_score, 49)
    elif decision == "research_first":
        gate_score = min(gate_score, 74)
    elif decision == "apply_now":
        gate_score = max(gate_score, 80)
        if str(data.get("application_roi") or "").lower() != "high":
            gate_score = min(gate_score, 78)
        angle = str(data.get("application_angle") or "").strip()
        generic_angle = not angle or len(angle) < 45 or any(
            phrase in angle.lower()
            for phrase in (
                "strong fit",
                "relevant experience",
                "transferable skills",
                "apply his experience",
                "technology leader",
            )
        )
        if generic_angle:
            gate_score = min(gate_score, 76)
    final_score = min(original_score, gate_score)
    # Format only after every decision invariant and cap has been applied, so
    # the visible Gate Score is the same number persisted as match_score.
    gate_section, _ = _format_gatekeeper_section(data, original_score, final_score)
    log(f"Deep gatekeeper: {decision or 'unknown'} at {final_score}% for originally {original_score}%.")
    return gate_section, final_score


def _format_analysis_text(data):
    score = int(data.get("match_score", 0) or 0)
    score = max(0, min(100, score))
    fit_level = data.get("fit_level", "N/A")
    summary = data.get("suitability_summary", "N/A")
    high_fit = data.get("high_fit_rationale", "")
    cover_letter_angle = data.get("cover_letter_angle", "N/A")
    recommended_action = data.get("recommended_action", "N/A")

    sections = [
        f"Match Score: {score}%",
        f"Fit Level: {fit_level}",
        f"Recommended Action: {recommended_action}",
        f"Suitability Summary:\n{summary}",
    ]
    if high_fit:
        sections.append(f"High-Fit Rationale:\n{high_fit}")
    sections.extend([
        _bullet_section("Strengths", data.get("strengths")),
        _bullet_section("Weaknesses / Risks", data.get("weaknesses")),
        _bullet_section("Key Skills Required", data.get("key_skills")),
        _bullet_section("Application Focus Points", data.get("application_focus_points")),
        _bullet_section("Resume Focus", data.get("resume_focus")),
        f"Cover Letter Angle:\n{cover_letter_angle}",
        _bullet_section("Interview Focus", data.get("interview_focus")),
    ])
    fragment_score = _coerce_fragment_score(data.get("fragment_score"))
    if fragment_score is not None:
        fragment_confidence = data.get("fragment_confidence", "N/A")
        fragment_angle = data.get("fragment_angle", "")
        sections.extend([
            "Fragment Alignment:",
            f"- Fragment Score: {fragment_score}%",
            f"- Confidence: {fragment_confidence}",
            _bullet_section("Activated Fragments", data.get("activated_fragments")),
            _bullet_section("Fragment Capability Gaps", data.get("fragment_capability_gaps")),
        ])
        if fragment_angle:
            sections.append(f"Fragment Angle:\n{fragment_angle}")
    return "\n\n".join(sections), score


def _coerce_fragment_score(value):
    if value in (None, ""):
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _analysis_fragment_alignment(data, has_fragment_context):
    """Extract fragment alignment from the full-analysis JSON."""
    if not has_fragment_context:
        return None, None
    fragment_score = _coerce_fragment_score(data.get("fragment_score"))
    if fragment_score is None:
        return None, None
    alignment = {
        "source": "full_analysis",
        "fragment_score": fragment_score,
        "activated_fragments": _coerce_list(data.get("activated_fragments")),
        "capability_gaps": _coerce_list(data.get("fragment_capability_gaps")),
        "angle_recommendation": str(data.get("fragment_angle") or "").strip(),
        "confidence": str(data.get("fragment_confidence") or "").strip() or "unknown",
    }
    return fragment_score, json.dumps(alignment, ensure_ascii=False, separators=(",", ":"))


def _resume_hash(resume_text):
    return hashlib.sha256(str(resume_text or "").encode("utf-8", errors="replace")).hexdigest()


def _get_resume_triage_summary(resume_text, profile_id, log):
    resume_hash = _resume_hash(resume_text)
    cached = db.get_resume_triage_cache(profile_id, resume_hash)
    if cached:
        return cached

    log("Creating compact resume triage cache...")
    prompt = f"""Summarise this Australian candidate's resume for fast first-pass job-fit triage. Plain text only, no markdown, no <think> tags. Australian English spelling. Maximum 300 words.

Structure the summary as labelled lines so the downstream triage prompt can scan it cheaply:

TARGET ROLE FAMILIES: comma-separated families the resume credibly supports (e.g. "Senior IT leadership, Business systems, Delivery / project").
SENIORITY CEILING: highest level credibly evidenced (e.g. "senior manager / head-of, but not C-suite").
STRONGEST SKILLS: 5-8 capability phrases (not single words) ordered by evidence weight.
DOMAIN STRENGTHS: sectors with named tenure (utilities, councils, higher education, manufacturing, etc.).
TRANSFERABLE ADJACENT ROLES: 3-5 adjacent role families where the resume credibly stretches.
CLEAR NON-FIT FAMILIES: 2-4 role families this resume does NOT credibly serve (e.g. "Pure software engineering, Clinical, Sales/BD, Junior support").
RECENT ANCHORS: 2-3 specific recent role/employer/outcome anchors a triage pass can name as evidence.

Use only facts present in the resume. Do not invent.

RESUME:
---
{resume_text[:12000]}
---"""
    summary = _call_scoring_ai(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.15,
        max_tokens=1000,
    ).strip()
    db.save_resume_triage_cache(profile_id, resume_hash, summary)
    return summary


def _triage_job(resume_summary, full_description, job_title, profile_id, log, lane_settings=None):
    """Score the role and raise flags on it, in one call.

    Flagging used to be a second LLM pass. It was folded in here because flags
    do not gate anything: nothing downstream branches on them, so there was
    nothing to justify a second round trip per job. Merging also widened
    coverage — triage runs on every job, while the old stage only ran on the
    ones that had already cleared the triage threshold.

    Triage gets the FULL advertisement rather than an extract, plus the ad's
    mandatory-requirement lines pulled out deterministically, so a credential
    gate stated in the small print at the bottom is still visible. It also gets
    the lane's own brief: without it the model judges level against the global
    doctrine's primary track and retires roles a secondary lane exists to find.

    Returns (score, reason, keep, flags) where flags is the normalised dict, or
    None when the model gave nothing usable.
    """
    if lane_settings is None:
        lane_settings = db.get_lane_settings(profile_id)
    mandatory, credential_gates = _extract_mandatory_requirements(full_description)
    stated_requirements = (
        "\n".join(f"- {line}" for line in mandatory)
        if mandatory else "The ad states no explicitly mandatory requirements. Do not invent a credential gate."
    )
    credential_block = (
        "\n".join(f"- {line}" for line in credential_gates)
        if credential_gates else "None detected by the deterministic pre-pass."
    )
    # Block order here is load-bearing, not cosmetic. Everything above the
    # BEGIN ROLE UNDER ASSESSMENT marker is byte-identical for every job in a
    # sweep: the instruction, the lane weighting terms, the lane brief and the
    # compact resume summary. Everything below it changes per job.
    #
    # A local server reuses the KV cache for the longest token prefix shared
    # with the request before it, so a stable block sitting after a varying one
    # is recomputed every call for nothing. This prompt used to open with the
    # job title, which truncated the reusable prefix at about ten tokens and
    # forced ~600-900 tokens of redundant prefill per job. Triage runs on every
    # job, so on a thousand-role sweep that was most of an hour of pure
    # recomputation.
    #
    # Keep the marker. New stable blocks go above it; new per-job blocks below.
    user_prompt = f"""Score this role for first-pass triage and raise any flags on it.

PROFILE PREFERENCE WEIGHTING:
---
{_analysis_preferences(profile_id)}
---
{_lane_brief_block(lane_brief(lane_settings))}
COMPACT RESUME SUMMARY:
---
{resume_summary[:2200]}
---

--- BEGIN ROLE UNDER ASSESSMENT ---

JOB TITLE: {job_title or 'Not supplied'}

MANDATORY REQUIREMENT LINES EXTRACTED FROM THE AD (deterministic pre-pass — the ad's own words):
---
{stated_requirements}
---

OF THOSE, THE ONES NAMING A CREDENTIAL, REGISTRATION, OR ELIGIBILITY GATE:
---
{credential_block}
---

FULL JOB ADVERTISEMENT:
---
{full_description[:12000]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": with_doctrine(TRIAGE_SYSTEM_PROMPT_BASE, lane_settings)},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.05,
        max_tokens=2000,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        log(f"Triage response was not valid JSON; sending to full analysis. Response: {response[:180]}...")
        return 100, "Triage failed open.", True, None
    score = max(0, min(100, int(data.get("match_score", 0) or 0)))
    flags = _normalise_job_flags(data)
    flags["stated_requirement_count"] = len(mandatory)
    flags["credential_gate_count"] = len(credential_gates)
    return (
        score,
        data.get("reason", "No triage reason supplied."),
        bool(data.get("keep", score >= TRIAGE_KEEP_THRESHOLD)),
        flags,
    )


# --- Job flags --------------------------------------------------------------
# Flags are observations, not decisions. Nothing downstream branches on them:
# they never block document generation, never cap a score, and never remove a
# role from the pipeline. They exist so the person deciding can see the specific,
# checkable concerns about a role next to its score.

JOB_FLAG_TYPES = (
    "credential_gate", "domain_mismatch", "seniority_below", "seniority_above", "evidence_gap",
)

JOB_FLAG_LABELS = {
    "credential_gate": "Credential gate",
    "domain_mismatch": "Domain mismatch",
    "seniority_below": "Below your level",
    "seniority_above": "Above your level",
    "evidence_gap": "Evidence gap",
}


# Phrases that mark a requirement as mandatory rather than aspirational. Ads
# that use none of these have no stated gate to check.
_MANDATORY_CUES = (
    "must have", "must hold", "must possess", "must be", "must currently",
    "must also", "must demonstrate", "essential criteria", "essential requirement",
    "is essential", "are essential", "mandatory", "required to hold",
    "is required", "are required", "you will need", "you must",
    "applicants must", "candidates must", "only applicants", "only candidates",
    "non-negotiable", "prerequisite", "eligibility", "unable to consider",
)


# Requirement nouns that make a mandatory line a credential/eligibility gate
# rather than a generic duty statement.
_CREDENTIAL_CUES = (
    "degree", "bachelor", "masters", "master's", "phd", "doctorate", "honours",
    "diploma", "certificate", "certification", "certified", "accredit",
    "registration", "registered", "licence", "license", "ticket", "white card",
    "clearance", "police check", "working with children", "wwcc", "vevo",
    "citizen", "citizenship", "permanent resident", "visa", "right to work",
    "chartered", "cpa", "ahpra", "aphra", "rpeq", "nv1", "nv2", "baseline",
    "qualification", "qualified", "years of experience", "years' experience",
    "years experience",
)


_REQUIREMENT_SPLIT_RE = re.compile(r"(?<=[.;:!?])\s+|\n+")


def _extract_mandatory_requirements(text, limit=14):
    """Pull the ad's own mandatory-requirement statements out of the prose.

    Deterministic on purpose. A model handed a short list of the ad's actual
    "must have" lines checks credentials far more reliably than one asked to
    re-read the whole ad, where the surrounding narrative crowds them out.

    Returns (mandatory_lines, credential_gate_lines); the second list is the
    subset that names a credential, registration, or eligibility gate.
    """
    mandatory = []
    credential_gates = []
    seen = set()
    for raw in _REQUIREMENT_SPLIT_RE.split(str(text or "")):
        line = " ".join(str(raw).split()).strip(" -*•\t")
        if not (12 <= len(line) <= 320):
            continue
        lowered = line.lower()
        if not any(cue in lowered for cue in _MANDATORY_CUES):
            continue
        key = lowered[:120]
        if key in seen:
            continue
        seen.add(key)
        mandatory.append(line)
        if any(cue in lowered for cue in _CREDENTIAL_CUES):
            credential_gates.append(line)
        if len(mandatory) >= limit:
            break
    return mandatory, credential_gates


def _normalise_job_flags(data):
    """Coerce raw triage JSON into the stored flag shape.

    One rule is enforced: a flag must name the ad's requirement, or it is
    dropped. An unevidenced flag is noise, and noise is what makes people stop
    reading the evidenced ones. Nothing else is filtered — low confidence is
    kept and labelled, because the reader decides, not this function.
    """
    flags = []
    for item in data.get("flags") or []:
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requirement") or "").strip()
        if not requirement:
            continue
        flag_type = str(item.get("type") or "").strip().lower().replace("-", "_")
        if flag_type not in JOB_FLAG_TYPES:
            flag_type = "evidence_gap"
        confidence = str(item.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        flags.append({
            "type": flag_type,
            "label": JOB_FLAG_LABELS[flag_type],
            "requirement": requirement,
            "detail": str(item.get("detail") or "").strip(),
            "confidence": confidence,
            "source": "auto",
        })

    direction = str(data.get("seniority_direction") or "").strip().lower()
    if direction not in {"below", "above", "aligned"}:
        direction = "unknown"

    summary = str(data.get("flag_summary") or "").strip()
    if not summary:
        summary = (
            f"{len(flags)} flag{'' if len(flags) == 1 else 's'} raised."
            if flags else "Nothing stood out."
        )

    return {
        "flags": flags,
        "domain_match": str(data.get("domain_match") or "").strip(),
        "seniority_match": str(data.get("seniority_match") or "").strip(),
        "seniority_direction": direction,
        "summary": summary,
    }


def _persist_flags(job_id, flags, log):
    """Store triage flags. Never raises: a flag is an observation, and failing
    to record one must not cost the job its analysis."""
    if not flags:
        return
    try:
        db.update_job_flags(job_id, flags)
    except Exception as exc:
        log(f"Flag persist skipped for job {job_id}: {exc}")
        return
    if flags.get("flags"):
        log(
            f"Flags for job ID {job_id}: "
            + ", ".join(f"{item['label']} ({item['confidence']})" for item in flags["flags"])
        )


def _format_flags_section(result):
    """Render flags for the stored analysis text."""
    flags = result.get("flags") or []
    lines = [
        "Flags:",
        f"- Summary: {result.get('summary') or 'None'}",
        f"- Domain: {result.get('domain_match') or 'N/A'}",
        f"- Seniority: {result.get('seniority_match') or 'N/A'} ({result.get('seniority_direction') or 'unknown'})",
        "",
        _bullet_section(
            "Raised",
            [
                f"[{flag['label']}, {flag['confidence']} confidence] {flag['requirement']}"
                + (f" -> {flag['detail']}" if flag.get("detail") else "")
                for flag in flags
            ],
        ),
    ]
    return "\n".join(lines)


_TITLE_STOPWORDS = {"and", "the", "for", "of", "a", "an", "to", "in", "with"}

# Shortest shared prefix that counts two tokens as the same word. Exact matching
# missed the obvious cases the rescue exists for — "Technician" against a lane
# hunting "Technical Officer", "Teacher" against "teaching" — because the lane
# states a family and ads state a job title. Five characters is long enough that
# unrelated words rarely collide, and the rescue only ever escalates to full
# analysis, which can demote again.
_TITLE_STEM_CHARS = 5


def _title_tokens(value):
    return {
        token for token in re.findall(r"[a-z0-9]{2,}", str(value or "").lower())
        if token not in _TITLE_STOPWORDS
    }


def _tokens_match(left, right):
    if left == right:
        return True
    if min(len(left), len(right)) < _TITLE_STEM_CHARS:
        return False
    return left[:_TITLE_STEM_CHARS] == right[:_TITLE_STEM_CHARS]


def _lane_title_overlap(title, lane_target_text):
    """How many words of a job title the lane's stated targets also use.

    Used by the borderline-rescue path: a single noisy triage number should not
    kill a role whose title plainly matches what the lane is hunting for.
    Matching is prefix-based rather than exact so ordinary word forms of the same
    term (technician/technical, teacher/teaching, officer/officers) count."""
    target_tokens = _title_tokens(lane_target_text)
    return sum(
        1 for token in _title_tokens(title)
        if any(_tokens_match(token, target) for target in target_tokens)
    )


def _title_matches_lane(title, lane_target_text):
    """Does this title read as one of the lane's own targets?

    Short titles must match in full: a one-word title like "Technician" carries
    its whole meaning in that word, and demanding two overlaps made the rescue
    unreachable for exactly those roles. Longer titles need two, so a stray
    shared word ("Senior", "Engineer") is not enough on its own.
    """
    tokens = _title_tokens(title)
    if not tokens:
        return False
    needed = 2 if len(tokens) >= 3 else len(tokens)
    return _lane_title_overlap(title, lane_target_text) >= needed


def _has_hard_knockout(flags):
    """True when triage found a stated credential the resume cannot evidence.

    The rescue second-guesses level judgements, which are a matter of strategy.
    A mandatory registration or clearance is not: no lane brief makes the
    candidate eligible, so those roles stay rejected."""
    return any(
        item.get("type") == "credential_gate" and item.get("confidence") == "high"
        for item in (flags or {}).get("flags") or []
    )


# Lane weighting terms are free text. Users separate them with semicolons at
# least as often as commas, and the settings fields sit next to other
# semicolon-delimited lists, so accept every separator rather than silently
# treating the whole field as one term that can never match.
_PREFERENCE_SPLIT_RE = re.compile(r"[;,\n]")


def _preference_terms(value):
    """Split a lane weighting field into individual terms."""
    return [term.strip(" -\t") for term in _PREFERENCE_SPLIT_RE.split(str(value or "")) if term.strip(" -\t")]


def _lane_brief_block(brief_text):
    """Wrap the lane brief for a user prompt, or return nothing when there is none."""
    if not brief_text:
        return ""
    return f"\nACTIVE LANE BRIEF:\n---\n{brief_text}\n---\n"


def _analysis_preferences(profile_id):
    settings = db.get_lane_settings(profile_id)
    boost_terms = _preference_terms(settings.get("boost_terms"))
    penalty_terms = _preference_terms(settings.get("penalty_terms"))
    if not boost_terms and not penalty_terms:
        return "No extra lane weighting terms have been set."
    return (
        "Extra lane weighting terms:\n"
        f"- Add weight when present: {'; '.join(boost_terms) or 'None'}\n"
        f"- Subtract weight when present: {'; '.join(penalty_terms) or 'None'}\n"
        "Treat these as preference signals, not absolute rules. Mention any strong effect in the rationale."
    )


def _apply_preference_weight(score, text, profile_id):
    settings = db.get_lane_settings(profile_id)
    haystack = str(text or "").lower()
    boost_hits = [term for term in _preference_terms(settings.get("boost_terms")) if term.lower() in haystack]
    penalty_hits = [term for term in _preference_terms(settings.get("penalty_terms")) if term.lower() in haystack]
    adjusted = score + min(10, 3 * len(boost_hits)) - min(15, 5 * len(penalty_hits))
    return max(0, min(100, adjusted)), boost_hits, penalty_hits


def check_job_relevance(job_description: str, resume_text: str, log_callback=None):
    """Check if a job is relevant to the candidate's resume using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()

    log = log_callback or print

    if not job_description or not resume_text:
        log("Error: Missing job description or resume text for relevance check.")
        return False

    if not _local_is_configured():
        log("ERROR: Local LLM endpoint is not configured for job relevance check.")
        return False

    system_prompt = """You are a fast Australian career-fit relevance gate. One resume, one job ad. Decide if this role is worth analysing.

OUTPUT CONTRACT
- Return exactly ONE minified JSON object. Nothing before or after. No <think> tags, no markdown.
- Use ONLY evidence from the supplied resume and job ad. Do not invent.
- Australian English spelling.

DECISION RULES
- "relevant" = plausibly worth full analysis. Includes credible step-ups and adjacent senior roles.
- "not relevant" = wrong level (junior/graduate or executive C-suite the resume cannot evidence), wrong function (sales, clinical, trades, etc.), missing mandatory eligibility (clearance, registration, citizenship), or no credible skill overlap.
- A vague recruiter ad with weak signal but plausible family fit -> relevant with low confidence.

REQUIRED JSON SHAPE
{
  "is_relevant": boolean,
  "confidence": int 0-100,
  "fit_level": "exceptional" | "strong" | "possible" | "weak" | "poor",
  "reason": "one sentence naming the dominant signal",
  "strengths": ["1-3 concise role-specific strengths grounded in resume evidence"],
  "weaknesses": ["1-2 concise gaps or risks specific to this role"],
  "application_focus": ["1-3 tailoring actions for this role"]
}

EXAMPLE
{"is_relevant":true,"confidence":78,"fit_level":"strong","reason":"Senior IT leadership role with credible cloud/platform overlap.","strengths":["Platform leadership at <employer>","Vendor governance"],"weaknesses":["No explicit Victorian government tenure"],"application_focus":["Lead with platform consolidation outcomes","Add a public-sector framing line"]}"""

    user_prompt = f"""Decide whether this role is worth full analysis given the candidate's resume.

CANDIDATE RESUME:
---
{resume_text[:11000]}
---

JOB ADVERTISEMENT:
---
{job_description[:6000]}
---"""

    try:
        llm_response_text = _call_scoring_ai(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1200,
            json_mode=True,
        )

        data = _extract_json(llm_response_text)
        if data:
            is_relevant = data.get("is_relevant", False)
            confidence = data.get("confidence", 0)
            fit_level = data.get("fit_level", "unknown")
            reason = data.get("reason", "No reason provided")
            strengths = "; ".join(_coerce_list(data.get("strengths"))[:3])
            weaknesses = "; ".join(_coerce_list(data.get("weaknesses"))[:2])
            focus = "; ".join(_coerce_list(data.get("application_focus"))[:3])
            detail = f"Relevance check - Relevant: {is_relevant}, Confidence: {confidence}%, Fit: {fit_level}, Reason: {reason}"
            if strengths:
                detail += f", Strengths: {strengths}"
            if weaknesses:
                detail += f", Risks: {weaknesses}"
            if focus:
                detail += f", Focus: {focus}"
            log(detail)
            return is_relevant
        else:
            log(f"Could not find JSON in LLM response for relevance check. Response: {llm_response_text[:200]}...")
            return False

    except Exception as e:
        log(f"Error in job relevance check: {e}")
        return False


def generalize_search_term(failed_term: str, resume_text: str):
    """Generate a more general search term using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()

    if not _local_is_configured():
        print("ERROR: Local LLM endpoint is not configured for generalize_search_term.")
        return failed_term

    system_prompt = """You broaden a failed Australian job-board search term to a more general, higher-recall alternative.

Return ONLY one minified JSON object with a single key "new_term". No <think> tags, no prose.

RULES
- Output one canonical job title that Seek and LinkedIn actually use (e.g. "IT Manager", "Business Systems Analyst", "Project Manager").
- Stay in the same seniority band as the original term.
- Drop the most-specialised qualifier first (sector, tool, sub-discipline) before dropping seniority.
- Do NOT return the original term unchanged.
- Do NOT include locations, salary, or qualifiers like "experienced", "senior" (unless the original had it).

EXAMPLE
{"new_term":"Senior Technology Manager"}"""
    user_prompt = (
        f"The job search for '{failed_term}' returned zero results. Based on the resume excerpt, "
        f"return ONE broader job title to retry.\n\nRESUME EXCERPT:\n{resume_text[:2500]}"
    )

    try:
        llm_response_text = _call_scoring_ai(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=300,
            json_mode=True,
        )

        data = _extract_json(llm_response_text)
        if data:
            new_term = data.get("new_term", failed_term).strip()
            if not new_term or new_term.lower() == failed_term.lower():
                return failed_term
            return new_term
        else:
            print("Could not find JSON object in LLM response for generalization.")
            return failed_term

    except Exception as e:
        print(f"Error getting generalized search term from local endpoint: {e}")
        return failed_term


def derive_search_terms_from_resume(optimism_level: int, resume_text: str):
    """Generate search terms using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()
    if not resume_text:
        raise ValueError("Resume text cannot be empty.")

    if not _local_is_configured():
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")

    if optimism_level <= 1:
        level_description = "3-4 direct, conservative title matches"
        spread = "direct matches only"
    elif optimism_level == 2:
        level_description = "4-5 titles: direct matches + realistic step-up"
        spread = "direct + realistic step-up"
    elif optimism_level == 3:
        level_description = "5-6 titles: direct + step-up + adjacent senior"
        spread = "direct + step-up + adjacent"
    elif optimism_level == 4:
        level_description = "6-8 titles: direct + step-up + adjacent + selective reach"
        spread = "direct + step-up + adjacent + selective reach"
    else:
        level_description = "8-10 titles: direct + step-up + adjacent + ambitious-but-credible reach"
        spread = "full spread including ambitious reach"

    system_prompt = """You generate Australian job-board search titles. Return ONLY a JSON array of strings — nothing else, no <think> tags, no commentary.

RULES
- Each string is a canonical job title that Seek and LinkedIn keyword search will match (e.g. "IT Manager", "Senior Business Analyst", "Technology Operations Manager", "Digital Delivery Lead").
- Titles only — no locations, salaries, qualifiers like "experienced", boolean operators, or markdown.
- No near-duplicates ("IT Manager" and "Manager IT" are the same query).
- Order from most to least likely to surface a fit.
- Use Australian title conventions (e.g. "Programme Manager" or "Program Manager" — match the spelling the user's market actually uses).

TRACK ANCHORS
- When the resume/lane context signals senior technology leadership, anchor on: "Head of IT", "Head of Digital and Technology", "Head of Technology", "IT Manager", "ICT Manager", "Technology Manager", "IT Operations Manager".
- When it signals embedded/electronics engineering, anchor on: "Embedded Systems Engineer", "Electronics Engineer", "Power Electronics Engineer", "Firmware Engineer", "Mechatronics Engineer", "Product Development Engineer".
- NEVER generate coordinator, project officer, helpdesk, service desk, support analyst, or graduate titles — these are retired tracks."""
    user_prompt = (
        f"Generate {level_description}. Spread: {spread}.\n"
        "Return a JSON array of strings only.\n\n"
        f"RESUME / LANE CONTEXT:\n---\n{resume_text}\n---"
    )

    llm_response_text = _call_scoring_ai(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=1500,
    )
    return llm_response_text


def _format_fragment_context(fragments):
    """Render up to ~25 fragments as compact bullets for the analysis prompt.

    The analysis prompt then has the option of leaning on prior validated
    fragments as additional evidence, or noting capability gaps the fragment
    bank does not cover.
    """
    if not fragments:
        return ""
    lines = []
    for frag in fragments[:25]:
        theme = str(frag.get("theme") or "").strip()
        claim = str(frag.get("claim") or "").strip()
        conf = str(frag.get("confidence") or "").strip()
        status = str(frag.get("status") or "").strip()
        ftype = str(frag.get("fragment_type") or "").strip()
        keywords = frag.get("keywords") or []
        if isinstance(keywords, list):
            kw = ", ".join(str(k) for k in keywords[:6])
        else:
            kw = str(keywords)
        suffix_bits = [bit for bit in (ftype, conf, status) if bit]
        suffix = f" [{' / '.join(suffix_bits)}]" if suffix_bits else ""
        line = f"- {theme}: {claim}{suffix}"
        if kw:
            line += f" — activates on: {kw}"
        lines.append(line)
    return "\n".join(lines)


def _compose_score(match_score, fragment_score, match_weight=None, fragment_weight=None):
    """Blend the resume-vs-ad match score with the fragment-bank alignment score.

    Weights default to the canonical constants in database_manager so there is
    exactly one place to tune the balance. Rebalanced to 60/40 on 2026-07-30:
    outcome evidence showed match_score does not separate interviews from
    non-interviews at all, so it no longer carries 80% of the ranking weight.
    """
    match_weight = db.COMPOSITE_MATCH_WEIGHT if match_weight is None else match_weight
    fragment_weight = db.COMPOSITE_FRAGMENT_WEIGHT if fragment_weight is None else fragment_weight
    if fragment_score is None:
        return int(round(match_score))
    return int(round(match_weight * float(match_score) + fragment_weight * float(fragment_score)))


def _band_block(job):
    """Seniority-band verdict appended to the stored analysis text.

    The band prior moves a job's composite score; this makes the reason legible
    in the job view instead of leaving an unexplained number. Band is advisory
    only — it never rejects a job on its own, so the note says so where the band
    is low-yield.
    """
    try:
        title = job["title"] if "title" in job.keys() else job.get("title")
    except AttributeError:
        title = None
    try:
        note = db.band_triage_note(title)
    except Exception:
        return ""
    return f"Targeting:\n{note}\n\n" if note else ""


def _maybe_align_fragments(job_id, score, full_description_for_analysis, profile_id, log):
    """Compute fragment_score + alignment_json for a job that's worth the spend.

    Skips below-threshold jobs (no point spending an extra LLM call to refine
    a rejection) and skips when the lane has no fragment bank yet (graceful
    degradation — composite_score falls back to match_score).
    """
    from .memory import align_memory_fragments_to_role
    # Imported here rather than at module scope: _maybe_align_fragments needs a
    # module that imports this one back.
    if score < 65:
        return None, None, None
    try:
        fragments = [dict(row) for row in db.get_lane_fragments(profile_id, limit=120)]
    except Exception as exc:
        log(f"Could not load lane fragments for composite scoring: {exc}")
        return None, None, None
    if not fragments:
        return None, None, None
    role_payload = {
        "job_id": job_id,
        "description": str(full_description_for_analysis or "")[:9000],
    }
    try:
        alignment, _provider = align_memory_fragments_to_role(role_payload, fragments, log_callback=log)
    except Exception as exc:
        log(f"Fragment alignment skipped for job {job_id}: {exc}")
        return None, None, None
    try:
        fragment_score = int(round(float(alignment.get("fragment_score") or 0)))
    except (TypeError, ValueError):
        fragment_score = 0
    fragment_score = max(0, min(100, fragment_score))
    alignment_json = json.dumps(alignment, ensure_ascii=False, separators=(",", ":"))
    return fragment_score, alignment_json, alignment


def _analyze_single_job(job, ctx):
    """Triage + full analysis for one job. Runs on analysis worker threads.

    Thread safety: every database_manager call opens its own SQLite
    connection (WAL + busy_timeout) and the bridge log emitter is
    lock-protected, so concurrent workers are safe. Raises
    OperationCancelledError when the user cancels.
    """
    log = ctx["log"]
    resume_text = ctx["resume_text"]
    resume_summary = ctx["resume_summary"]
    preference_context = ctx["preference_context"]
    lane_target_text = ctx["lane_target_text"]
    lane_settings = ctx["lane_settings"]
    lane_brief_text = ctx["lane_brief"]
    fragment_context = ctx["fragment_context"]
    system_prompt = ctx["system_prompt"]
    profile_id = ctx["profile_id"]

    job_id, description, pdf_text = job['id'], job['description'], job['pdf_text']
    position_description_text = job["position_description_text"] if "position_description_text" in job.keys() else ""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Analysis cancelled by user.")
    concurrency.paused.wait()

    full_description_for_analysis = _strip_image_references(description or "")
    if position_description_text:
        full_description_for_analysis = (
            f"--- UPLOADED POSITION DESCRIPTION ---\n{_strip_image_references(position_description_text)}\n\n"
            f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_description_for_analysis}"
        )
    if pdf_text:
        full_description_for_analysis += f"\n\n--- ADDITIONAL TEXT FROM PDF ---\n{_strip_image_references(pdf_text)}"
    analysis_signature = db.make_analysis_signature(resume_text, description, pdf_text, position_description_text)
    job_title = job["title"] if "title" in job.keys() else ""
    triage_score = None
    flags = None

    try:
        triage_score, triage_reason, keep, flags = _triage_job(
            f"{resume_summary}\n\n{preference_context}",
            full_description_for_analysis,
            job_title,
            profile_id,
            log,
            lane_settings,
        )
        triage_score, boost_hits, penalty_hits = _apply_preference_weight(triage_score, full_description_for_analysis, profile_id)
        if boost_hits or penalty_hits:
            triage_reason += f" Preference flags: +{', '.join(boost_hits) or 'none'}; -{', '.join(penalty_hits) or 'none'}."
        if not keep:
            if triage_score >= TRIAGE_KEEP_THRESHOLD:
                # Model returned keep=false for a score at or above the keep floor —
                # inconsistent with the KEEP RULE (keep=true when score >= 45 and no
                # knockout). Don't force the score below the auto-reject threshold;
                # store it as-is so the job stays for manual review.
                log(f"Triage keep=false inconsistency for job ID {job_id}: score {triage_score}% is above keep floor; storing uncapped.")
                triage_reason += " Triage keep=false (model inconsistency; score stored uncapped)."
            else:
                triage_score = min(triage_score, TRIAGE_KEEP_THRESHOLD - 1)
                triage_reason += " Triage keep=false; treating as below keep threshold."
        log(f"Triage for job ID {job_id}: {triage_score}% - {triage_reason}")
        rescued = False
        if triage_score < FULL_ANALYSIS_TRIAGE_THRESHOLD:
            # Borderline rescue: one noisy triage number must not kill a
            # role whose title plainly matches the lane's stated targets.
            # Those get the evidence-anchored full analysis instead — the
            # only stage equipped to promote as well as demote.
            #
            # Deliberately not gated on keep or the keep threshold. The caps
            # this exists to second-guess are level judgements, and those land
            # at 40 with keep=false, i.e. below both gates: a rescue that
            # required either could never reach the roles that needed it. A
            # stated credential the resume cannot meet is a different kind of
            # no, so that one still stands.
            rescued = (
                triage_score >= TRIAGE_RESCUE_FLOOR
                and not _has_hard_knockout(flags)
                and _title_matches_lane(job_title, lane_target_text)
            )
            if rescued:
                log(
                    f"Borderline rescue for job ID {job_id}: triage {triage_score}% but title "
                    f"'{job_title}' matches lane targets. Escalating to full analysis."
                )
        if triage_score < FULL_ANALYSIS_TRIAGE_THRESHOLD and not rescued:
            # Flags are recorded even here. A role that never earns the
            # full-analysis spend still benefits from carrying the reason it
            # looked marginal, and this is now the only stage that raises them.
            _persist_flags(job_id, flags, log)
            analysis_text = (
                f"Triage Match Score: {triage_score}%\n\n"
                f"Triage Result:\n{triage_reason}\n\n"
                f"{_format_flags_section(flags) if flags else ''}\n\n"
                f"{_band_block(job)}"
                f"Full analysis skipped because the first-pass score was below {FULL_ANALYSIS_TRIAGE_THRESHOLD}%."
            )
            db.update_job_analysis(job_id, analysis_text, triage_score, analysis_signature)
            try:
                db.update_job_fragment_alignment(job_id, None, _compose_score(triage_score, None), None)
            except Exception as exc:
                log(f"Composite score persist skipped for job {job_id}: {exc}")
            return
    except concurrency.OperationCancelledError:
        raise
    except Exception as e:
        if concurrency.cancel_event.is_set():
            raise
        log(f"Triage failed for job ID {job_id}; falling back to full analysis: {e}")

    # Flags come from triage. They are recorded and shown; nothing here branches
    # on them, so a flagged role continues to full analysis exactly like any
    # other. The person deciding sees the flags, not this function.
    _persist_flags(job_id, flags, log)

    flag_block = ""
    if flags and flags["flags"]:
        flag_block = (
            "\n\nFLAGS RAISED AT TRIAGE (the ad's own requirements, checked against the resume). "
            "Address each one directly rather than reframing it away. If one cannot be answered with "
            "resume evidence, say so and let it lower the score:\n"
            "---\n"
            + "\n".join(
                f"- [{item['label']}] {item['requirement']}"
                + (f" -> {item['detail']}" if item.get("detail") else "")
                for item in flags["flags"]
            )
            + "\n---"
        )

    fragment_block = (
        f"\n\nVALIDATED MEMORY FRAGMENTS (reusable claims with prior evidence — lean on these where the job activates them):\n"
        f"---\n{fragment_context}\n---"
        if fragment_context else ""
    )
    user_prompt = f"""Analyse this Australian job advertisement against the candidate's resume. Return the required JSON only.

CANDIDATE RESUME:
---
{resume_text[:12000]}
---

PROFILE PREFERENCE WEIGHTING:
---
{preference_context}
---
{_lane_brief_block(lane_brief_text)}{fragment_block}{flag_block}

JOB ADVERTISEMENT:
---
{full_description_for_analysis[:9000]}
---"""
    json_string = ""
    llm_response_text = ""

    try:
        log(f"Analyzing job ID {job_id}...")
        llm_response_text = _call_scoring_ai(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.15,
            max_tokens=6000,
            json_mode=True,
        )

        data = _extract_json(llm_response_text)
        if data:
            json_string = llm_response_text
            analysis_text, score = _format_analysis_text(data)
            if score >= 78:
                log(f"Running deep gatekeeper for job ID {job_id} ({score}%).")
                gatekeeper_text, gated_score = _run_deep_gatekeeper(
                    resume_summary,
                    resume_text,
                    full_description_for_analysis,
                    data,
                    score,
                    profile_id,
                    log,
                    lane_settings,
                )
                if gatekeeper_text:
                    analysis_text = f"{analysis_text}\n\n{gatekeeper_text}"
                    score = gated_score
                    analysis_text = re.sub(
                        r"^Match Score:\s*\d+%",
                        f"Match Score: {score}%",
                        analysis_text,
                        count=1,
                    )
        else:
            # LLM returned an unparseable response. Skip this job rather than
            # auto-rejecting it — a transient failure (empty response, server
            # overload, model issue) must not permanently kill a good fit.
            # analysis_signature is left as-is so the job is re-analysed next run.
            log(f"Could not find JSON for job ID {job_id}; skipping (will retry). Response: {llm_response_text[:200]!r}")
            return

        fragment_score, alignment_json = (
            _analysis_fragment_alignment(data, bool(fragment_context))
            if data and score >= 65 else (None, None)
        )
        if flags:
            analysis_text = f"{analysis_text}\n\n{_format_flags_section(flags)}"
        band_block = _band_block(job)
        if band_block:
            analysis_text = f"{analysis_text}\n\n{band_block.rstrip()}"
        db.update_job_analysis(job_id, analysis_text, score, analysis_signature)
        # Fragment-aware composite scoring now uses the full-analysis JSON
        # instead of a separate alignment LLM call. composite_score falls
        # back to match_score when no fragment score is available.
        composite_score = _compose_score(score, fragment_score)
        try:
            db.update_job_fragment_alignment(job_id, fragment_score, composite_score, alignment_json)
        except Exception as exc:
            log(f"Composite score persist skipped for job {job_id}: {exc}")
        if fragment_score is not None:
            log(f"Analyzed job ID {job_id}. Match score: {score}%; Fragment score: {fragment_score}%; Composite: {composite_score}%")
        else:
            reason = "no fragment bank" if not fragment_context else "no fragment score returned"
            log(f"Analyzed job ID {job_id}. Match score: {score}% ({reason}; composite = match)")

    except json.JSONDecodeError as e:
        # Malformed JSON in an unexpected code path. Same safe-skip policy.
        log(f"JSON decode error for job ID {job_id}: {e} — skipping (will retry).")
        log(f"Failing string: {json_string[:300]}")
    except concurrency.OperationCancelledError:
        raise
    except Exception as e:
        if concurrency.cancel_event.is_set():
            raise concurrency.OperationCancelledError("Analysis cancelled by user.")
        # Transient errors (timeout, server overload, model crash) must not
        # permanently auto-reject jobs. Log and skip; next run will retry.
        log(f"Error analysing job ID {job_id}: {e} — skipping (will retry).")


def _perform_analysis_loop(
    jobs_to_analyze,
    resume_text,
    system_prompt,
    log_callback,
    profile_id=1,
    fragments=None,
    progress_callback=None,
):
    """Run the core analysis pipeline over a batch of jobs.

    Shared context (resume triage summary, lane preferences, fragment bank)
    is built once, then jobs run through _analyze_single_job on a bounded
    thread pool sized by the analysis_workers setting. When `fragments` is
    supplied, the analysis prompt includes them as additional evidence so the
    model can lean on validated reusable claims, not just the raw resume;
    if not supplied, composite scoring falls back to match_score.

    `progress_callback(current, total, failed=…)` reports countable progress
    for the UI. It is called once per completed job in both the serial and the
    parallel branch; the bridge throttles the resulting protocol frames, so
    this layer does not need to.
    """
    log = log_callback or print
    report = progress_callback or (lambda *args, **kwargs: None)
    if not jobs_to_analyze:
        return
    resume_summary = _get_resume_triage_summary(resume_text, profile_id, log)
    preference_context = _analysis_preferences(profile_id)
    lane_settings = db.get_lane_settings(profile_id)
    lane_target_text = " ".join([
        lane_settings.get("target_titles") or "",
        lane_settings.get("target_domains") or "",
        lane_settings.get("lane_intent") or "",
    ])
    # The caller hands in a doctrine-free base prompt; the lane's own doctrine
    # is resolved here because this is the only layer that knows the lane.
    system_prompt = with_doctrine(system_prompt, lane_settings)
    if fragments is None:
        try:
            fragments = [dict(row) for row in db.get_lane_fragments(profile_id, limit=40)]
        except Exception:
            fragments = []
    fragment_context = _format_fragment_context(fragments)

    ctx = {
        "log": log,
        "resume_text": resume_text,
        "resume_summary": resume_summary,
        "preference_context": preference_context,
        "lane_target_text": lane_target_text,
        "lane_settings": lane_settings,
        "lane_brief": lane_brief(lane_settings),
        "fragment_context": fragment_context,
        "system_prompt": system_prompt,
        "profile_id": profile_id,
    }
    # Deterministic screen before any LLM call. Commute and pay are facts about
    # the posting, not judgements, so resolving them in Python is both cheaper
    # and more reliable than asking a model. Blocked jobs keep their row and
    # their reason; they are skipped here and omitted from the shortlist.
    try:
        screener = screening.build(lane_settings, log=log)
        kept, set_aside = [], 0
        for job in jobs_to_analyze:
            verdict = screener.screen(job)
            db.save_job_screening(job["id"], verdict)
            if verdict["verdict"] == "blocked":
                set_aside += 1
            else:
                kept.append(job)
        if set_aside:
            log(f"Screened out {set_aside} job(s) on commute or pay before analysis; "
                f"they remain visible with a reason.")
        jobs_to_analyze = kept
    except Exception as exc:
        # Screening is an optimisation. If it breaks, analyse everything rather
        # than silently dropping roles the user would have wanted to see.
        log(f"Screening skipped ({exc}); analysing all jobs.")

    if not jobs_to_analyze:
        report(0, 0, failed=0)
        return

    workers = _analysis_worker_count()
    total = len(jobs_to_analyze)
    report(0, total, failed=0)
    if workers <= 1 or total <= 1:
        done = 0
        failed = 0
        for job in jobs_to_analyze:
            if concurrency.cancel_event.is_set():
                log("Analysis cancelled by user.")
                raise concurrency.OperationCancelledError("Analysis cancelled by user.")
            try:
                _analyze_single_job(job, ctx)
            except concurrency.OperationCancelledError:
                raise
            except Exception as exc:
                # Matches the parallel branch: per-job failures are handled
                # inside the worker, so anything landing here is unexpected.
                # Log it, count it, and keep the batch moving.
                failed += 1
                log(f"Analysis raised unexpectedly: {exc}")
            done += 1
            report(done, total, failed=failed)
        return

    log(f"Analyzing {total} job(s) with {workers} parallel workers...")
    done = 0
    failed = 0
    cancelled = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job-analysis") as executor:
        futures = [executor.submit(_analyze_single_job, job, ctx) for job in jobs_to_analyze]
        try:
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except concurrency.OperationCancelledError:
                    cancelled = True
                    break
                except Exception as exc:
                    # Per-job failures are already handled inside the worker;
                    # anything surfacing here is unexpected. Log and continue.
                    failed += 1
                    log(f"Analysis worker raised unexpectedly: {exc}")
                done += 1
                report(done, total, failed=failed)
                if done % 5 == 0 or done == total:
                    log(f"Analysis progress: {done}/{total} job(s) processed.")
        finally:
            if cancelled or concurrency.cancel_event.is_set():
                # Drop everything still queued; running workers notice the
                # cancel event at their next checkpoint and exit quickly.
                for future in futures:
                    future.cancel()
    if cancelled:
        log("Analysis cancelled by user.")
        raise concurrency.OperationCancelledError("Analysis cancelled by user.")


def analyze_jobs(log_callback=None, resume_text: str = "", re_analyze: bool = False, status_filter: str = 'new', profile_id=1, progress_callback=None):
    """Analyze jobs using the configured local endpoint."""
    log = log_callback or print
    local = _local_ai_settings()
    log(f"Analyzing '{status_filter}' jobs with local endpoint ({local['model'] or 'no model configured'})...")

    if not _local_is_configured():
        log("ERROR: Local LLM endpoint is not configured. Analysis halted.")
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")
    if not resume_text:
        log("Halting analysis because resume text was not provided.")
        return

    jobs_to_analyze = db.get_jobs_to_analyze(status_filter, re_analyze, profile_id, resume_text)
    log(f"Found {len(jobs_to_analyze)} jobs to analyze in the '{status_filter}' view.")

    _perform_analysis_loop(
        jobs_to_analyze,
        resume_text,
        ANALYSIS_SYSTEM_PROMPT_BASE,
        log_callback,
        profile_id,
        progress_callback=progress_callback,
    )
    log("Analysis complete.")


def analyze_specific_jobs(job_ids, log_callback=None, resume_text: str = "", profile_id=1, progress_callback=None):
    """Analyzes a specific list of jobs by their IDs using the local endpoint."""
    log = log_callback or print
    local = _local_ai_settings()
    log(f"Analyzing {len(job_ids)} specific job(s) with local endpoint ({local['model'] or 'no model configured'})...")

    if not _local_is_configured():
        log("ERROR: Local LLM endpoint is not configured. Analysis halted.")
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")
    if not resume_text:
        log("Halting analysis because resume text was not provided.")
        return

    jobs_to_analyze = db.get_jobs_to_analyze_by_ids(job_ids)
    log(f"Found {len(jobs_to_analyze)} jobs in DB from the provided list of IDs.")

    _perform_analysis_loop(
        jobs_to_analyze,
        resume_text,
        ANALYSIS_SYSTEM_PROMPT_BASE,
        log_callback,
        profile_id,
        progress_callback=progress_callback,
    )
    log("Specific analysis complete.")
