"""The scoring chain: triage, the hard-blocker gate, full analysis, deep gatekeeping.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
import concurrent.futures
import re
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
    ANALYSIS_SYSTEM_PROMPT,
    BLOCKER_GATE_SYSTEM_PROMPT,
    DEEP_GATEKEEPER_SYSTEM_PROMPT,
    FULL_ANALYSIS_TRIAGE_THRESHOLD,
    TRIAGE_KEEP_THRESHOLD,
    TRIAGE_SYSTEM_PROMPT,
)

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


def _run_deep_gatekeeper(resume_summary, resume_text, full_description, analysis_data, original_score, profile_id, log):
    preference_context = _analysis_preferences(profile_id)
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

FULL ANALYSIS JSON:
---
{json.dumps(analysis_data, ensure_ascii=False)[:4500]}
---

RESUME EXTRACT:
---
{resume_text[:9000]}
---

JOB DESCRIPTION:
---
{full_description[:10000]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": DEEP_GATEKEEPER_SYSTEM_PROMPT},
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


def _triage_job(resume_summary, full_description, log):
    user_prompt = f"""Estimate job fit for first-pass triage.

COMPACT RESUME SUMMARY:
---
{resume_summary[:1800]}
---

JOB EXTRACT:
---
{full_description[:3500]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.05,
        max_tokens=768,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        log(f"Triage response was not valid JSON; sending to full analysis. Response: {response[:180]}...")
        return 100, "Triage failed open.", True
    score = max(0, min(100, int(data.get("match_score", 0) or 0)))
    return score, data.get("reason", "No triage reason supplied."), bool(data.get("keep", score >= TRIAGE_KEEP_THRESHOLD))


BLOCKER_GATE_VERDICTS = ("skip", "stretch", "clear", "unknown")


# A skip verdict caps the stored score at the keep floor: the role stays
# visible for manual review but can never sit high in the campaign plan.
# Deliberately equal to (not below) database_manager.AUTO_REJECT_THRESHOLD, so
# the gate never silently auto-rejects — blocking documents is its job, purging
# the pipeline is not.
BLOCKER_SKIP_SCORE_CAP = TRIAGE_KEEP_THRESHOLD


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

    Deterministic on purpose. The gate LLM is far more decisive when handed a
    short list of the ad's actual "must have" lines than when asked to re-read
    the whole ad, where the surrounding narrative pulls it toward a reframing.

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


def _normalise_blocker_gate(data):
    """Coerce raw gate JSON into the stored shape, applying the safety rules.

    Two rules matter here. A skip must carry at least one evidenced hard
    blocker, and a low-confidence skip is downgraded to a stretch: this stage
    exists to remove false positives, and it must not become a new source of
    false negatives.
    """
    verdict = str(data.get("verdict") or "").strip().lower().replace("-", "_")
    if verdict.startswith("stretch"):
        verdict = "stretch"
    elif verdict.startswith("clear"):
        verdict = "clear"
    elif verdict != "skip":
        verdict = "unknown"

    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    # Direction matters even when the verdict is not a skip: a role below the
    # resume's ceiling still gets applied for sometimes, and when it does the
    # documents have to be written differently.
    direction = str(data.get("seniority_direction") or "").strip().lower()
    if direction not in {"below", "above", "aligned"}:
        direction = "unknown"

    blockers = []
    for item in data.get("hard_blockers") or []:
        if isinstance(item, dict):
            requirement = str(item.get("requirement") or "").strip()
            why = str(item.get("why_unmet") or "").strip()
        else:
            requirement, why = str(item or "").strip(), ""
        if requirement:
            blockers.append({"requirement": requirement, "why_unmet": why})

    gaps = [gap for gap in _coerce_list(data.get("named_gaps")) if gap]
    downgraded_from = None

    if verdict == "skip" and not blockers:
        # The evidence rule was not met, so the skip is unsupported.
        downgraded_from, verdict = "skip", "stretch"
    elif verdict == "skip" and confidence == "low":
        downgraded_from, verdict = "skip", "stretch"
        gaps = gaps + [
            f"{item['requirement']} ({item['why_unmet']})".strip().rstrip("()").strip()
            for item in blockers
        ]

    if verdict == "clear" and gaps:
        verdict = "stretch"

    return {
        "verdict": verdict,
        "confidence": confidence,
        "hard_blockers": blockers,
        "named_gaps": gaps,
        "domain_match": str(data.get("domain_match") or "").strip(),
        "seniority_match": str(data.get("seniority_match") or "").strip(),
        "seniority_direction": direction,
        "reason": str(data.get("reason") or "").strip() or "No gate reason supplied.",
        "downgraded_from": downgraded_from,
    }


def _run_blocker_gate(resume_summary, resume_text, full_description, job_title, profile_id, log):
    """Decide skip / stretch / clear for one job before full analysis.

    Returns the normalised verdict dict, or None when the gate could not run —
    callers must treat None as "no opinion" and continue, never as a block.
    """
    mandatory, credential_gates = _extract_mandatory_requirements(full_description)
    stated_requirements = (
        "\n".join(f"- {line}" for line in mandatory)
        if mandatory else "The ad states no explicitly mandatory requirements. Judge domain and seniority only; do not invent a credential gate."
    )
    credential_block = (
        "\n".join(f"- {line}" for line in credential_gates)
        if credential_gates else "None detected by the deterministic pre-pass."
    )
    user_prompt = f"""Decide whether the candidate should apply for this role at all.

JOB TITLE: {job_title or 'Not supplied'}

MANDATORY REQUIREMENT LINES EXTRACTED FROM THE AD (deterministic pre-pass — these are the ad's own words):
---
{stated_requirements}
---

OF THOSE, THE ONES NAMING A CREDENTIAL, REGISTRATION, OR ELIGIBILITY GATE:
---
{credential_block}
---

PROFILE PREFERENCE WEIGHTING:
---
{_analysis_preferences(profile_id)}
---

COMPACT RESUME SUMMARY:
---
{resume_summary[:2200]}
---

RESUME EXTRACT:
---
{resume_text[:8000]}
---

FULL JOB ADVERTISEMENT:
---
{full_description[:9000]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": BLOCKER_GATE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.05,
        max_tokens=2000,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        log(f"Blocker gate response was not valid JSON; continuing to full analysis. Response: {response[:180]}...")
        return None
    result = _normalise_blocker_gate(data)
    result["stated_requirement_count"] = len(mandatory)
    result["credential_gate_count"] = len(credential_gates)
    return result


def _format_blocker_section(result):
    """Render the gate verdict for the stored analysis text."""
    lines = [
        "Hard-Blocker Gate:",
        f"- Verdict: {result['verdict']}",
        f"- Confidence: {result['confidence']}",
        f"- Domain Match: {result.get('domain_match') or 'N/A'}",
        f"- Seniority Match: {result.get('seniority_match') or 'N/A'}",
        f"- Seniority Direction: {result.get('seniority_direction') or 'unknown'}",
        f"- Reason: {result['reason']}",
    ]
    if result.get("downgraded_from"):
        lines.append(
            f"- Note: gate returned '{result['downgraded_from']}' but it was not supported by "
            "evidenced, confident blockers, so it was downgraded."
        )
    lines.append("")
    lines.append(_bullet_section(
        "Hard Blockers",
        [f"{item['requirement']} -> {item['why_unmet']}" if item.get("why_unmet") else item["requirement"]
         for item in result.get("hard_blockers") or []],
    ))
    lines.append(_bullet_section("Named Gaps", result.get("named_gaps")))
    return "\n".join(lines)


def _lane_title_overlap(title, lane_target_text):
    """Token overlap between a job title and the lane's stated targets.

    Used by the borderline-rescue path: a single noisy triage number should not
    kill a role whose title plainly matches what the lane is hunting for."""
    stop = {"and", "the", "for", "of", "a", "an", "to", "in"}
    tokenize = lambda value: {
        token for token in re.findall(r"[a-z0-9]{2,}", str(value or "").lower())
        if token not in stop
    }
    return len(tokenize(title) & tokenize(lane_target_text))


def _analysis_preferences(profile_id):
    settings = db.get_lane_settings(profile_id)
    boost_terms = settings.get("boost_terms") or ""
    penalty_terms = settings.get("penalty_terms") or ""
    if not boost_terms and not penalty_terms:
        return "No extra lane weighting terms have been set."
    return (
        "Extra lane weighting terms:\n"
        f"- Add weight when present: {boost_terms or 'None'}\n"
        f"- Subtract weight when present: {penalty_terms or 'None'}\n"
        "Treat these as preference signals, not absolute rules. Mention any strong effect in the rationale."
    )


def _apply_preference_weight(score, text, profile_id):
    settings = db.get_lane_settings(profile_id)
    haystack = str(text or "").lower()
    boost_hits = [term for term in _coerce_list((settings.get("boost_terms") or "").replace(",", "\n")) if term.lower() in haystack]
    penalty_hits = [term for term in _coerce_list((settings.get("penalty_terms") or "").replace(",", "\n")) if term.lower() in haystack]
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

    try:
        triage_score, triage_reason, keep = _triage_job(
            f"{resume_summary}\n\n{preference_context}",
            full_description_for_analysis,
            log,
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
            rescued = (
                keep
                and triage_score >= TRIAGE_KEEP_THRESHOLD
                and _lane_title_overlap(job_title, lane_target_text) >= 2
            )
            if rescued:
                log(
                    f"Borderline rescue for job ID {job_id}: triage {triage_score}% but title "
                    f"'{job_title}' matches lane targets. Escalating to full analysis."
                )
        if triage_score < FULL_ANALYSIS_TRIAGE_THRESHOLD and not rescued:
            analysis_text = (
                f"Triage Match Score: {triage_score}%\n\n"
                f"Triage Result:\n{triage_reason}\n\n"
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

    # --- Hard-blocker gate --------------------------------------------------
    # Only runs when triage produced a score, because a skip verdict persists
    # the triage score alongside it. When triage failed we keep the existing
    # behaviour and fall straight through to full analysis.
    blocker = None
    if triage_score is not None:
        try:
            blocker = _run_blocker_gate(
                resume_summary,
                resume_text,
                full_description_for_analysis,
                job_title,
                profile_id,
                log,
            )
        except concurrency.OperationCancelledError:
            raise
        except Exception as e:
            if concurrency.cancel_event.is_set():
                raise
            # A gate failure must never block a role. No opinion, carry on.
            log(f"Blocker gate failed for job ID {job_id}; continuing to full analysis: {e}")
            blocker = None

    if blocker:
        try:
            db.update_job_blocker_gate(job_id, blocker["verdict"], blocker["reason"], blocker)
        except Exception as exc:
            log(f"Blocker verdict persist skipped for job {job_id}: {exc}")
        log(f"Blocker gate for job ID {job_id}: {blocker['verdict']} ({blocker['confidence']} confidence) - {blocker['reason']}")

    if blocker and blocker["verdict"] == "skip":
        capped_score = min(triage_score, BLOCKER_SKIP_SCORE_CAP)
        analysis_text = (
            f"Match Score: {capped_score}%\n\n"
            f"Triage Match Score: {triage_score}%\n\n"
            f"{_format_blocker_section(blocker)}\n\n"
            f"{_band_block(job)}"
            "Full analysis and document generation skipped: the hard-blocker gate returned a "
            "decisive skip. Clear the verdict in the workspace if you disagree; that also queues "
            "the job for re-analysis."
        )
        db.update_job_analysis(job_id, analysis_text, capped_score, analysis_signature)
        try:
            db.update_job_fragment_alignment(job_id, None, _compose_score(capped_score, None), None)
        except Exception as exc:
            log(f"Composite score persist skipped for job {job_id}: {exc}")
        return

    blocker_block = ""
    if blocker and blocker.get("named_gaps"):
        blocker_block = (
            "\n\nHARD-BLOCKER GATE — NAMED GAPS (a prior stage has already checked eligibility, domain and level; "
            "these gaps are real and must be answered directly, not reframed away. If a gap cannot be answered with "
            "resume evidence, say so and let it lower the score):\n"
            "---\n" + "\n".join(f"- {gap}" for gap in blocker["named_gaps"]) + "\n---"
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
---{fragment_block}{blocker_block}

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
        if blocker:
            analysis_text = f"{analysis_text}\n\n{_format_blocker_section(blocker)}"
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


def _perform_analysis_loop(jobs_to_analyze, resume_text, system_prompt, log_callback, profile_id=1, fragments=None):
    """Run the core analysis pipeline over a batch of jobs.

    Shared context (resume triage summary, lane preferences, fragment bank)
    is built once, then jobs run through _analyze_single_job on a bounded
    thread pool sized by the analysis_workers setting. When `fragments` is
    supplied, the analysis prompt includes them as additional evidence so the
    model can lean on validated reusable claims, not just the raw resume;
    if not supplied, composite scoring falls back to match_score.
    """
    log = log_callback or print
    if not jobs_to_analyze:
        return
    resume_summary = _get_resume_triage_summary(resume_text, profile_id, log)
    preference_context = _analysis_preferences(profile_id)
    lane_settings = db.get_lane_settings(profile_id)
    lane_target_text = " ".join([
        lane_settings.get("target_titles") or "",
        lane_settings.get("lane_intent") or "",
    ])
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
        "fragment_context": fragment_context,
        "system_prompt": system_prompt,
        "profile_id": profile_id,
    }

    workers = _analysis_worker_count()
    total = len(jobs_to_analyze)
    if workers <= 1 or total <= 1:
        for job in jobs_to_analyze:
            if concurrency.cancel_event.is_set():
                log("Analysis cancelled by user.")
                raise concurrency.OperationCancelledError("Analysis cancelled by user.")
            _analyze_single_job(job, ctx)
        return

    log(f"Analyzing {total} job(s) with {workers} parallel workers...")
    done = 0
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
                    log(f"Analysis worker raised unexpectedly: {exc}")
                done += 1
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


def analyze_jobs(log_callback=None, resume_text: str = "", re_analyze: bool = False, status_filter: str = 'new', profile_id=1):
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

    _perform_analysis_loop(jobs_to_analyze, resume_text, ANALYSIS_SYSTEM_PROMPT, log_callback, profile_id)
    log("Analysis complete.")


def analyze_specific_jobs(job_ids, log_callback=None, resume_text: str = "", profile_id=1):
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

    _perform_analysis_loop(jobs_to_analyze, resume_text, ANALYSIS_SYSTEM_PROMPT, log_callback, profile_id)
    log("Specific analysis complete.")
