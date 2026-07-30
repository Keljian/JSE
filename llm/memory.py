"""Candidate-memory fragment extraction, alignment, and consolidation.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
from .providers import (
    _call_document_ai,
    _settings_for_ai_task,
)
from .parsing import (
    _coerce_list,
    _extract_json,
    _extract_json_list,
    _repair_json_via_llm,
)
from .analysis import (
    _format_fragment_context,
)

def _normalise_memory_fragments(value):
    """Coerce plausible LLM fragment objects into the persisted fragment shape."""
    fragments = []
    allowed_types = {"capability", "domain", "seniority", "outcome", "tool", "preference"}
    allowed_seniority = {"individual", "lead", "manager", "executive", "unknown"}
    allowed_confidence = {"high", "medium", "low"}
    allowed_status = {"established", "emerging"}

    if not isinstance(value, list):
        return fragments

    for item in value:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        theme = str(item.get("theme") or "").strip()
        if not claim or not theme:
            continue

        fragment_type = str(item.get("fragment_type") or "capability").strip().lower()
        if fragment_type not in allowed_types:
            fragment_type = "capability"

        seniority = str(item.get("seniority") or "unknown").strip().lower()
        if seniority not in allowed_seniority:
            seniority = "unknown"

        confidence = str(item.get("confidence") or "medium").strip().lower()
        if confidence not in allowed_confidence:
            confidence = "medium"

        status = str(item.get("status") or "established").strip().lower()
        if status not in allowed_status:
            status = "established"

        supporting_detail = (
            item.get("supporting_detail")
            or item.get("evidence")
            or item.get("supporting_evidence")
            or ""
        )
        fragments.append({
            **item,
            "fragment_type": fragment_type,
            "theme": theme,
            "claim": claim,
            "supporting_detail": str(supporting_detail).strip(),
            "job_families": _coerce_list(item.get("job_families")),
            "keywords": _coerce_list(item.get("keywords")),
            "anti_keywords": _coerce_list(item.get("anti_keywords")),
            "seniority": seniority,
            "skills": _coerce_list(item.get("skills")),
            "domains": _coerce_list(item.get("domains")),
            "reuse_guidance": str(item.get("reuse_guidance") or "").strip(),
            "confidence": confidence,
            "confidence_reasoning": str(item.get("confidence_reasoning") or "").strip(),
            "status": status,
            "reinforces_fragment_themes": _coerce_list(item.get("reinforces_fragment_themes")),
        })
    return fragments


def extract_application_memory_fragments(
    application_payload,
    settings=None,
    log_callback=None,
    prior_lane_fragments=None,
    kit_outcome=None,
):
    """Mine reusable typed fragments from a saved (human-validated) application kit.

    Submitted applications are higher-signal than raw scraped jobs: a human chose
    to spend a real application slot on this role and approved the kit. The
    extracted fragments form a candidate-memory bank that future jobs are scored
    against, that future search terms are derived from, and that future
    tailoring leans on.

    Parameters
    ----------
    application_payload
        Dict containing the saved kit (job ad, tailored resume, cover letter,
        analysis, source paths). See _saved_application_document_sources in
        python_bridge.py for the shape.
    prior_lane_fragments
        OPTIONAL list of fragments already in this lane's bank (from earlier
        applied jobs). When supplied, the prompt reconciles the new extraction
        against the existing bank: reinforcing themes get lifted confidence,
        genuinely new themes are flagged as `emerging`, and near-duplicates are
        merged via `reinforces_fragment_themes`. Pass this every time so the
        extraction is lane-aware, not isolated.
    kit_outcome
        OPTIONAL string: 'applied', 'interviewed', 'rejected', 'liked',
        'archived', or 'unknown'. Biases confidence and status assignment.

    See the top-of-file architecture note for the wider memory loop.
    """
    log = log_callback or print
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")

    prior_context = _format_fragment_context(prior_lane_fragments)
    prior_block = (
        f"\n\nPRIOR LANE FRAGMENT BANK (from earlier applied jobs in THIS lane — reconcile against these):\n"
        f"---\n{prior_context}\n---\n"
        f"When this kit reinforces a prior theme, list its theme in `reinforces_fragment_themes` and lift confidence accordingly. "
        f"When the new claim is genuinely new for this lane, mark status='emerging'. Avoid producing near-duplicates of prior themes — "
        f"merge by reusing the prior theme name."
        if prior_context else
        "\n\nPRIOR LANE FRAGMENT BANK: (none — this is the first applied job mined for this lane, or the caller did not supply prior context)"
    )

    outcome_block = ""
    if kit_outcome:
        outcome_block = (
            f"\n\nKIT OUTCOME: {kit_outcome}\n"
            "Bias confidence by outcome: 'interviewed' or 'liked' lifts confidence one band on fragments well-anchored in the kit; "
            "'rejected' caps confidence at 'medium' and prefers status='emerging' for new themes; 'archived' caps at 'low'; "
            "'applied' or 'unknown' uses the normal evidence-based heuristic."
        )

    messages = [
        {
            "role": "system",
            "content": (
                "You extract reusable, typed candidate-memory fragments from a saved Australian job application kit "
                "(job ad + tailored resume + cover letter + analysis). Fragments form a long-term memory of what the "
                "candidate can credibly claim, where evidence sits, what job language activates each claim, and how "
                "strong each signal is. Return ONLY one valid JSON object with key 'fragments'. No <think> tags, no markdown. "
                "Australian English. Every fragment must be grounded in the supplied documents — do not invent. "
                "When a prior lane fragment bank is supplied, reconcile against it: reinforce known themes, flag truly new ones."
            ),
        },
        {
            "role": "user",
            "content": f"""Extract 8-18 typed fragments from the saved application kit below.{prior_block}{outcome_block}

FRAGMENT TYPES (use one per fragment; pick the best fit)
- capability: a skill or pattern the candidate repeatedly sells (e.g. systems thinking, technical leadership, vendor management).
- domain:     a sector / context with named tenure (utilities, councils, healthcare, higher education, manufacturing, infrastructure).
- seniority:  a leadership-level signal (team lead, manager, strategic advisor, executive-facing, governance, budget responsibility).
- outcome:    a concrete value-claim (reduced risk, improved reliability, delivered transformation, automated manual work).
- tool:       platform / stack evidence (ERP, M365, Azure, Power BI, ITSM, integrations, governance frameworks).
- preference: a pattern across roles the candidate actually applied for (hybrid Melbourne, IT/business bridge, delivery-heavy not pure coding, systems ownership).

PER-FRAGMENT FIELDS (all required)
- fragment_type:   one of the six types above.
- theme:           short title (3-6 words).
- claim:           one reusable sentence the candidate can credibly assert.
- evidence:        one or two sentences citing the source — name the employer/project/outcome from THIS application kit. Use exact dates only when present in the source.
- job_families:    1-4 role families this fragment is genuinely useful for (e.g. "Delivery Lead", "Business Systems Manager").
- keywords:        4-10 ad-side phrases that should ACTIVATE this fragment (the words you would scan a job ad for).
- anti_keywords:   2-6 ad-side signals that mean this fragment is NOT a good fit (e.g. "L1 support", "pure coding role", "C-suite").
- seniority:       "individual" | "lead" | "manager" | "executive" | "unknown".
- skills:          0-6 supporting skills.
- domains:         0-4 supporting domains.
- reuse_guidance:  one sentence on WHEN to use it AND when to avoid it.
- confidence:      "high" | "medium" | "low".
- confidence_reasoning: one short sentence explaining the confidence (e.g. "Appears across two roles with named outcomes" vs "Single-role evidence, not yet repeated").
- status:          "established" if the evidence is concrete and repeatable; "emerging" if the fragment is plausible but rests on a single stretch application — emerging fragments are kept for cautious reuse, not narrow echo-chamber filtering.

QUALITY BAR
- Fragments are small reusable units, NOT whole paragraphs to copy. Aim for portable claims.
- Avoid generic platitudes ("strong communicator"). Every fragment must have something specific to point at in the kit.
- Prefer fragments that appear across multiple roles in the kit — they are higher-confidence.
- A single one-off claim can still produce a fragment, but mark status="emerging" and confidence<=medium.

JSON SHAPE
{{"fragments":[
  {{
    "fragment_type":"capability|domain|seniority|outcome|tool|preference",
    "theme":"...",
    "claim":"...",
    "evidence":"...",
    "job_families":["..."],
    "keywords":["..."],
    "anti_keywords":["..."],
    "seniority":"individual|lead|manager|executive|unknown",
    "skills":["..."],
    "domains":["..."],
    "reuse_guidance":"...",
    "confidence":"high|medium|low",
    "confidence_reasoning":"...",
    "status":"established|emerging",
    "reinforces_fragment_themes":["EXACT theme name(s) from the PRIOR LANE FRAGMENT BANK this kit reinforces — empty array when this is a genuinely new theme"]
  }}
]}}

APPLICATION KIT:
---
{json.dumps(application_payload, ensure_ascii=False)[:18000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.15, max_tokens=5000, json_mode=True
    )
    data = _extract_json(response) or _repair_json_via_llm(response, settings=local_settings)
    fragments = _normalise_memory_fragments(data.get("fragments") if isinstance(data, dict) else None)
    if not fragments:
        raise ValueError(f"Memory extraction did not return valid fragments. Response started: {response[:250]}")
    log(f"Extracted memory fragments with {provider_label}.")
    return fragments, provider_label


def align_memory_fragments_to_role(role_payload, fragments, settings=None, log_callback=None):
    """Score a target role against the candidate-memory fragment bank.

    Instead of only asking "does this resume match the ad?", this asks:
      1. Which stored fragments does this role ACTIVATE (via keyword match)?
      2. Which required capabilities have NO fragment support (true gaps)?
      3. Which activated fragments form the strongest application angle?
      4. Should we suggest an EMERGING fragment for a stretch role with no
         prior pattern, so the memory bank doesn't become an echo chamber?

    The output is intended to drive both tailoring and a fragment-aware score.
    """
    log = log_callback or print
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")
    messages = [
        {
            "role": "system",
            "content": (
                "You match a candidate-memory fragment bank to ONE Australian job advertisement. "
                "Return ONLY one valid JSON object. No <think> tags, no markdown. Australian English. "
                "Do not invent facts. Only cite fragments that genuinely match the role's named requirements. "
                "Do not narrow the candidate to known patterns: a stretch role should produce an EMERGING fragment "
                "suggestion rather than a rejection."
            ),
        },
        {
            "role": "user",
            "content": f"""Score this role against the supplied fragment bank.

PROCESS
1. Identify 5-10 role features from the job ad — the duties, capabilities, and ownership the ad actually asks for.
2. For each fragment in the bank, decide whether it is activated by the role features. A fragment is activated when at least one of its keywords appears in (or is clearly evidenced by) the role features, AND none of its anti_keywords describe the role.
3. List capability_gaps: role features for which NO fragment in the bank provides credible evidence.
4. Pick the angle_recommendation: the 2-3 strongest activated fragments combined into one sentence that should drive the application angle.
5. Suggest emerging fragments (status="emerging") ONLY when the role activates fewer than 3 fragments but the resume context suggests there is honest adjacent evidence worth capturing for future use.
6. Weight activated fragments by their stored confidence; flag any activations that rely solely on emerging/low-confidence fragments.

REQUIRED JSON SHAPE
{{
  "role_features": ["5-10 short feature strings from the ad"],
  "fragment_matches": [
    {{
      "fragment_id": 123,
      "theme": "...",
      "match_strength": "strong" | "medium" | "weak",
      "role_feature": "which role feature this fragment activates",
      "activating_keywords": ["keyword from the fragment that fired"],
      "fragment_confidence": "high|medium|low",
      "fragment_status": "established|emerging",
      "how_to_use": "one sentence on how to deploy this fragment in resume/cover letter",
      "caution": "risk or empty string"
    }}
  ],
  "capability_gaps": ["role features with no credible fragment support"],
  "angle_recommendation": "one sentence application angle drawing on the strongest activated fragments",
  "fragment_score": int 0-100 (composite: 4+ strong activations covering core features => 80+; 2-3 medium activations => 60-79; <2 activations => <60),
  "emerging_suggestions": [
    {{"theme":"...","claim":"...","why":"why this stretch role justifies capturing a low-confidence fragment for future cautious reuse"}}
  ],
  "writing_strategy": "concise strategy for how to lean on activated fragments and how to address each capability gap"
}}

ROLE:
---
{json.dumps(role_payload, ensure_ascii=False)[:9000]}
---

FRAGMENT BANK:
---
{json.dumps(fragments, ensure_ascii=False)[:16000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.1, max_tokens=5000, json_mode=True
    )
    data = _extract_json(response)
    if not data:
        raise ValueError(f"Memory alignment did not return valid JSON. Response started: {response[:250]}")
    log(f"Aligned memory fragments with {provider_label}.")
    return data, provider_label


def consolidate_memory_fragments(fragments_from_kits, settings=None, log_callback=None):
    """Dedupe + merge fragments across many application kits.

    Same theme appearing across multiple kits should produce ONE consolidated
    fragment with a lifted confidence. Truly one-off claims stay as separate
    emerging fragments. The output is intended to overwrite or supplement the
    persisted fragment bank.

    Input shape: a list of {kit_id, role_title, outcome, fragments:[...]}
    where outcome is one of: 'applied', 'interviewed', 'rejected', 'liked',
    'archived', or 'unknown'. Outcome weighting is performed by the model.
    """
    log = log_callback or print
    if not fragments_from_kits:
        return [], "no fragments to consolidate"
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")
    messages = [
        {
            "role": "system",
            "content": (
                "You consolidate a candidate's typed memory fragments across many submitted Australian "
                "job applications. Same theme across many kits => ONE merged fragment with lifted "
                "confidence. One-off claims stay separate, marked status='emerging' and confidence<=medium. "
                "Return ONLY one valid JSON object. No <think> tags, no markdown. Australian English. "
                "Never invent facts not present in the supplied fragments."
            ),
        },
        {
            "role": "user",
            "content": f"""Consolidate the supplied per-kit fragments into a deduped fragment bank.

RULES
- Merge fragments with the same theme/claim across kits. Keep the strongest evidence wording.
- Track which kits supported the merged fragment in `source_kit_ids` and how many times the theme appeared in `support_count`.
- Outcome weighting: bias confidence UP when supporting kits include outcome='interviewed' or 'liked'; bias DOWN when only 'rejected' or 'archived' kits support it; ignore 'unknown'.
- Confidence ladder: support_count >= 4 with at least one interviewed kit => "high"; support_count 2-3 => "medium"; support_count 1 => "low" and status="emerging".
- Promote status from 'emerging' to 'established' ONLY when support_count >= 2 AND at least one non-rejected outcome.
- Preserve the typed shape — keep fragment_type, keywords, anti_keywords, job_families, etc.

REQUIRED JSON SHAPE
{{
  "consolidated_fragments": [
    {{
      "fragment_type": "capability|domain|seniority|outcome|tool|preference",
      "theme": "...",
      "claim": "merged sentence using strongest source wording",
      "evidence": "merged evidence citing the strongest source kit",
      "job_families": ["..."],
      "keywords": ["..."],
      "anti_keywords": ["..."],
      "seniority": "individual|lead|manager|executive|unknown",
      "skills": ["..."],
      "domains": ["..."],
      "reuse_guidance": "...",
      "confidence": "high|medium|low",
      "confidence_reasoning": "explain support_count + outcomes that drove the level",
      "status": "established|emerging",
      "source_kit_ids": [int, ...],
      "support_count": int,
      "outcomes_seen": ["interviewed","applied", ...]
    }}
  ],
  "dropped_fragments": [
    {{"theme":"...","reason":"why this fragment was dropped (duplicate of X, contradicted by Y, too vague)"}}
  ],
  "consolidation_notes": ["any patterns the user should know: dominant themes, gaps, contradictions"]
}}

PER-KIT FRAGMENTS:
---
{json.dumps(fragments_from_kits, ensure_ascii=False)[:30000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.05, max_tokens=8000, json_mode=True
    )
    data = _extract_json(response) or _repair_json_via_llm(response, settings=local_settings)
    if not data or not isinstance(data.get("consolidated_fragments"), list):
        raise ValueError(f"Fragment consolidation did not return valid JSON. Response started: {response[:250]}")
    log(f"Consolidated {len(data['consolidated_fragments'])} fragments with {provider_label}.")
    return data, provider_label


def promote_emerging_fragments(fragments, outcome_history, settings=None, log_callback=None):
    """Decide which 'emerging' fragments have earned promotion to 'established'.

    `outcome_history` is a list of {kit_id, outcome, role_title} so the model
    can check whether the fragment was reused successfully. The output lists
    only fragments whose status should change; the caller patches the bank.
    """
    log = log_callback or print
    emerging = [f for f in (fragments or []) if str(f.get("status", "")).lower() == "emerging"]
    if not emerging:
        return {"promotions": [], "demotions": [], "notes": ["No emerging fragments to evaluate."]}, "no emerging fragments"
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")
    messages = [
        {
            "role": "system",
            "content": (
                "You audit emerging candidate-memory fragments and decide which have earned promotion to "
                "'established' status. Return ONLY one valid JSON object. No <think> tags. Australian English. "
                "Be cautious: established fragments shape future applications, so the bar is real."
            ),
        },
        {
            "role": "user",
            "content": f"""Audit these emerging fragments against the outcome history.

PROMOTION RULE
- Promote to 'established' if the fragment now has source_kit_ids count >= 2 AND at least one supporting kit had outcome in ('interviewed', 'liked').
- Keep as 'emerging' otherwise — but lift confidence one band if outcomes are net positive (interviewed/liked outweigh rejected).
- DEMOTE (suggest deletion) if the only supporting kits had outcome='rejected' AND the fragment has not been reused in 3+ subsequent applications.

REQUIRED JSON SHAPE
{{
  "promotions": [
    {{"fragment_id_or_theme":"...","reason":"why this earned promotion (cite outcomes)","new_confidence":"high|medium|low"}}
  ],
  "demotions": [
    {{"fragment_id_or_theme":"...","reason":"why this should be dropped or downgraded"}}
  ],
  "confidence_adjustments": [
    {{"fragment_id_or_theme":"...","old":"medium","new":"high","reason":"..."}}
  ],
  "notes": ["patterns worth surfacing to the user"]
}}

EMERGING FRAGMENTS:
---
{json.dumps(emerging, ensure_ascii=False)[:18000]}
---

OUTCOME HISTORY:
---
{json.dumps(outcome_history or [], ensure_ascii=False)[:8000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.05, max_tokens=4000, json_mode=True
    )
    data = _extract_json(response) or _repair_json_via_llm(response, settings=local_settings)
    if not data:
        raise ValueError(f"Promotion audit did not return valid JSON. Response started: {response[:250]}")
    log(f"Promotion audit complete with {provider_label}.")
    return data, provider_label


def derive_search_terms_from_fragments(fragments, optimism_level=3, settings=None, log_callback=None):
    """Generate job-board search terms from the fragment bank, not the raw resume.

    Fragments are higher-signal than the resume because they encode WHICH
    capabilities have actually carried previous applications and WHERE the
    candidate has chosen to spend slots. The terms generated here should bias
    toward fragments that are 'established' and that activated in successful
    (interviewed/liked) applications.

    `fragments` may carry optional outcome metadata (avg_outcome_strength,
    times_activated, last_interview_at) populated by the caller from the DB.
    The prompt uses it when present and ignores it gracefully when absent.
    """
    log = log_callback or print
    if not fragments:
        return [], "no fragments — caller should fall back to derive_search_terms_from_resume"
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")

    if optimism_level <= 1:
        spread = "3-4 conservative titles drawn from the strongest established fragments only"
    elif optimism_level == 2:
        spread = "4-5 titles: established fragments + one realistic step-up"
    elif optimism_level == 3:
        spread = "5-6 titles: established + step-up + one cautious emerging fragment"
    elif optimism_level == 4:
        spread = "6-8 titles: established + step-up + adjacent + selective reach using emerging fragments"
    else:
        spread = "8-10 titles: full spread including ambitious reach from emerging fragments — but flag the reach titles"

    messages = [
        {
            "role": "system",
            "content": (
                "You generate Australian job-board search titles from a candidate's memory-fragment bank. "
                "Return ONLY a JSON array of strings — nothing else, no <think> tags, no markdown. "
                "Each title is canonical (Seek/LinkedIn keyword-search friendly), no boolean operators, "
                "no locations, no salary. Bias toward fragments with status='established', high confidence, "
                "and positive outcome history when that metadata is present. Use Australian title conventions."
            ),
        },
        {
            "role": "user",
            "content": f"""Generate {spread} from this fragment bank.

PROCESS
1. Group fragments by job_families.
2. For each high-confidence established cluster, generate the best matching canonical title.
3. Add step-up titles where a 'seniority' fragment indicates the candidate has demonstrated lead/manager/executive evidence.
4. Add adjacent titles by mixing capability + domain fragments (e.g. capability='enterprise systems ownership' + domain='councils' => 'Business Systems Manager').
5. Order by activation strength (established + high confidence + recent positive outcomes first).
6. NEVER include a title that conflicts with the anti_keywords on relevant fragments.

FRAGMENT BANK:
---
{json.dumps(fragments, ensure_ascii=False)[:18000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.35, max_tokens=1500
    )
    terms = _extract_json_list(response) or []
    if not terms:
        # Lenient fallback: line-split the response if the model returned a list-like blob.
        terms = [line.strip(' -"\t,') for line in str(response).splitlines() if line.strip(' -"\t,')]
        terms = [t for t in terms if len(t) <= 120 and not t.startswith('{')]
    log(f"Derived {len(terms)} fragment-driven search terms with {provider_label}.")
    return terms, provider_label
