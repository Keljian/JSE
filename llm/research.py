"""Company and job-ad intelligence, and hidden-market strategy.

Split out of llm_handler.py, which re-exports everything here.
"""
import re
import database_manager as db
from .providers import (
    _call_document_ai,
    _call_unsloth,
    _local_is_configured,
    _settings_for_ai_task,
)
from .parsing import (
    _extract_json,
)
from .prompts import (
    COMPANY_RESEARCH_SYSTEM_PROMPT,
)

def _fallback_job_intelligence(job):
    text = " ".join(str(job.get(key) or "") for key in ("title", "description", "pdf_text")).lower()
    title = str(job.get("title") or "")
    role_family = "other"
    family_terms = {
        "IT leadership": ["it manager", "technology manager", "infrastructure", "platform", "service delivery", "systems manager"],
        "engineering systems": ["engineering", "engineer", "automation", "mechatronics", "embedded", "cad", "bim"],
        "business analysis": ["business analyst", "business partner", "requirements", "process", "stakeholder"],
        "delivery": ["project manager", "program manager", "delivery lead", "transformation", "implementation"],
    }
    for family, terms in family_terms.items():
        if any(term in text or term in title.lower() for term in terms):
            role_family = family
            break
    seniority = "unknown"
    if re.search(r"\b(head|director|executive|chief)\b", text):
        seniority = "executive"
    elif re.search(r"\b(lead|manager|principal)\b", text):
        seniority = "lead"
    elif re.search(r"\b(senior|sr)\b", text):
        seniority = "senior"
    elif re.search(r"\b(junior|graduate|assistant)\b", text):
        seniority = "junior"
    skills = []
    for term in [
        "stakeholder", "vendor", "cloud", "azure", "automation", "governance", "security",
        "salesforce", "erp", "project management", "requirements", "cad", "bim", "embedded",
        "integration", "operations", "strategy",
    ]:
        if term in text:
            skills.append(term)
    work_mode = "unknown"
    if "remote" in text:
        work_mode = "remote"
    elif "hybrid" in text:
        work_mode = "hybrid"
    elif any(term in text for term in ("on site", "on-site", "onsite")):
        work_mode = "onsite"
    import ad_signals
    derived = ad_signals.derive(job)
    return {
        "role_family": role_family,
        "seniority": seniority,
        "core_skills": skills[:12],
        "domains": [],
        "responsibilities": [],
        "hard_requirements": [],
        "soft_requirements": [],
        "dealbreakers": [],
        "work_mode": work_mode,
        "employer_type_hint": "unknown",
        "hiring_trigger": derived["hiring_trigger"],
        "reporting_line": derived["reporting_line"],
        "team_size": derived["team_size"],
        "ats_keywords": derived["ats_keywords"],
        "confidence": "low",
        "fallback": True,
    }


def extract_job_intelligence(job, settings=None, log_callback=None):
    """Use the local model to extract compact structured job intelligence."""
    log = log_callback or (lambda _message: None)
    fallback = _fallback_job_intelligence(job)
    if (settings or {}).get("force_fallback") or not _local_is_configured():
        return fallback, "deterministic fallback"
    local_settings = {**(settings or {}), "doc_ai_provider": "local"}
    text = "\n\n".join([
        f"Title: {job.get('title') or ''}",
        f"Company: {job.get('company') or ''}",
        f"Location: {job.get('location') or ''}",
        str(job.get("description") or "")[:9000],
        str(job.get("pdf_text") or "")[:4000],
    ])
    messages = [
        {
            "role": "system",
            "content": (
                "You extract compact, structured job-routing intelligence from a single Australian job ad. "
                "Return ONLY one valid JSON object — no <think> tags, no markdown, no prose. "
                "Do NOT assess the candidate; this is purely about the role. "
                "Use only evidence from the supplied ad. "
                "If a field is genuinely unclear, return 'unknown' or an empty list — do not guess."
            ),
        },
        {
            "role": "user",
            "content": f"""Extract a compact JSON object with EXACTLY these keys (all present, even if empty):

{{
  "role_family": "IT leadership | engineering systems | business analysis | delivery | product | support | sales | other",
  "seniority": "junior | mid | senior | lead | executive | unknown",
  "core_skills":          [list of 4-10 capability phrases the ad emphasises — phrases, not single words],
  "domains":              [list of 0-5 sector/industry markers actually named in the ad],
  "responsibilities":     [list of 3-8 short verb-led duty lines from the ad],
  "hard_requirements":    [list of explicit must-haves: certifications, clearances, named tools, years of experience, eligibility],
  "soft_requirements":    [list of nice-to-haves explicitly framed as preferred/desirable],
  "dealbreakers":         [list of items framed as mandatory that filter candidates: clearance, on-site only, mandatory shift, citizenship, registration],
  "work_mode": "onsite | hybrid | remote | unknown",
  "employer_type_hint": "direct | recruiter | mixed | unknown",
  "hiring_trigger": "growth | replacement | backfill | restructure | unknown",
  "reporting_line": "who this role reports to, verbatim from the ad, or empty string",
  "team_size": "integer number of reports/team members stated in the ad, or null",
  "ats_keywords": [list of 8-15 EXACT terms/phrases an ATS parser would weight — pulled verbatim from the ad],
  "confidence": "low | medium | high"
}}

HINTS
- "recruiter": ad written by an agency, no end-client name, or generic 'our client'.
- "direct": clear single employer named, application goes to the employer.
- confidence = "low" when the ad is short, vague, or recruiter-written with no end client.
- hiring_trigger: "growth"=new/expansion role, "replacement"=replacing a leaver, "backfill"=leave cover/fixed-term, "restructure"=new/reformed team.
- ats_keywords: copy the ad's own wording (tools, certifications, methodologies, named systems) — do not paraphrase.

JOB:
---
{text}
---""",
        },
    ]
    try:
        response, provider = _call_document_ai(
            local_settings, messages, temperature=0.05, max_tokens=2500, json_mode=True
        )
        data = _extract_json(response)
        if not data:
            log("Local job intelligence returned malformed JSON; using deterministic fallback.")
            return fallback, "deterministic fallback"
        merged = {**fallback, **data, "fallback": False}
        return merged, provider
    except Exception as exc:
        log(f"Local job intelligence failed; using deterministic fallback: {exc}")
        return fallback, "deterministic fallback"


def research_company_for_job(job_id: int, settings=None, log_callback=None):
    log = log_callback or print
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job with ID {job_id} not found.")
    existing = job["company_intelligence"] or "{}"
    prompt = f"""Build company intelligence for this job application.

ADVERTISER / COMPANY FIELD:
{job['company'] or ''}

JOB TITLE:
{job['title'] or ''}

CONTACT EMAIL:
{job['contact_email'] or ''}

APPLICATION URL:
{job['application_url'] or job['url'] or ''}

EXISTING LOCAL CLASSIFIER:
{existing}

FIT ANALYSIS:
{job['ai_analysis'] or 'No fit analysis yet.'}

JOB ADVERTISEMENT:
---
{(job['description'] or '')[:12000]}
---
"""
    response, provider_label = _call_document_ai(
        _settings_for_ai_task(settings, "research_ai_provider"),
        [
            {"role": "system", "content": COMPANY_RESEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=3500,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        raise ValueError(f"Company research did not return valid JSON. Response started: {response[:250]}")
    log(f"Company intelligence researched with {provider_label}.")
    return data, provider_label


def _hidden_market_strategy_text_legacy(target, lane_context="", settings=None):
    """Generate a short, tailored outreach angle + concrete next steps for a
    hidden-market target using the local model. Returns plain prose (not JSON)."""
    target = target or {}
    target_type = target.get("target_type") or "target"
    type_label = {
        "recruiter": "recruitment agency / consultant who repeatedly carries this role family",
        "direct_employer": "direct employer that keeps hiring this role family",
        "leadership_gap": "employer hiring junior/IC staff with no leadership role advertised (possible unadvertised leadership need)",
    }.get(target_type, "hidden-market target")

    facts = [
        f"Target type: {type_label}",
        f"Name: {target.get('name') or target.get('target_name') or 'Unknown'}",
    ]
    if target.get("sample_titles"):
        facts.append("Roles seen: " + ", ".join(str(t) for t in (target.get("sample_titles") or [])))
    for label, key in (("Best fit score", "best_score"), ("Relevant roles in window", "roles"),
                       ("Junior/IC hires with no leader", "ic_count"), ("Domain", "domain"),
                       ("Locations", "locations")):
        value = target.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value:
            facts.append(f"{label}: {value}")
    contact = " · ".join(
        str(target.get(field)) for field in ("contact_person", "contact_email", "contact_phone") if target.get(field)
    )
    if contact:
        facts.append(f"Known contact: {contact}")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a pragmatic outreach strategist for the Australian hidden job market "
                "(unadvertised roles). Give specific, actionable advice the candidate can use today. "
                "Australian English. Plain text only — no markdown headings, no preamble, no <think> tags."
            ),
        },
        {
            "role": "user",
            "content": (
                "Candidate / lane context:\n"
                + (lane_context or "Experienced candidate; specific context not provided.")
                + "\n\nHidden-market target:\n" + "\n".join(facts)
                + "\n\nIn under 140 words give: (1) one or two sentences on the angle — why approach this "
                "target now and how to position; (2) 2 to 4 concrete next steps (who to contact, which channel, "
                "and what to say). Be specific to this target, not generic advice."
            ),
        },
    ]
    text = _call_unsloth(messages, temperature=0.3, max_tokens=600, json_mode=False, settings=settings)
    return (text or "").strip()


def hidden_market_strategy(target, lane_context="", settings=None, contact_research=None):
    """Generate a structured, durable, evidence-grounded outreach strategy."""
    target = target or {}
    target_type = target.get("target_type") or "target"
    facts = [
        f"Target type: {target_type}",
        f"Name: {target.get('name') or target.get('target_name') or 'Unknown'}",
        f"Opportunity score: {target.get('opportunity_score') or 0}",
        f"Identity confidence: {target.get('confidence') or 'unknown'}",
        f"Recommended action: {target.get('recommended_action') or ''}",
    ]
    for label, key in (
        ("Roles seen", "sample_titles"), ("Classification evidence", "classification_reasons"),
        ("Counter evidence", "counter_evidence"), ("Domain", "domain"), ("Locations", "locations"),
    ):
        value = target.get(key)
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value)
        if value:
            facts.append(f"{label}: {value}")
    contact = " | ".join(
        str(target.get(field)) for field in ("contact_person", "contact_email", "contact_phone") if target.get(field)
    )
    if contact:
        facts.append(f"Known contact: {contact}")
    contact_research = contact_research or {}
    selected_id = contact_research.get("selected_candidate_id")
    selected = next((item for item in contact_research.get("candidates", []) if item.get("candidate_id") == selected_id), None)
    if selected:
        facts.extend([
            f"Selected person: {selected.get('name') or 'Unknown'}",
            f"Selected person's current role: {selected.get('role') or 'Not confirmed'}",
            f"Selected person's organisation: {selected.get('organisation') or target.get('name') or ''}",
            f"Selected person's email: {selected.get('email') or 'Not confirmed'}",
            f"Selected person's phone: {selected.get('phone') or 'Not confirmed'}",
            f"Selected person's public profile: {selected.get('profile_url') or 'Not found'}",
            f"Contact confidence: {selected.get('confidence') or 'unknown'} ({selected.get('confidence_score') or 0}/100)",
            "Contact source URLs: " + "; ".join(source.get("url") or "" for source in selected.get("sources", []) if source.get("url")),
            "Contact conflicts: " + ("; ".join(selected.get("conflicts") or []) or "None recorded"),
        ])
    messages = [
        {
            "role": "system",
            "content": (
                "You are a pragmatic Australian hidden-job-market outreach strategist. "
                "Use only supplied evidence. Address the selected person by name only when one is explicitly supplied. "
                "Never invent people, job titles, relationships, vacancies, or company facts. "
                "Return only one valid JSON object with the requested keys."
            ),
        },
        {
            "role": "user",
            "content": (
                "Candidate lane context:\n" + (lane_context or "Not supplied.")
                + "\n\nTarget evidence:\n" + "\n".join(facts)
                + "\n\nReturn exactly: "
                + '{"positioning_angle":"why approach now and how to position",'
                + '"contact_persona":"specific role or person type to contact",'
                + '"recommended_channel":"email | LinkedIn | phone | warm introduction | company site",'
                + '"opening_message":"concise editable first message",'
                + '"evidence_to_reference":["2-4 supplied facts"],'
                + '"questions_to_ask":["2-4 useful questions"],'
                + '"follow_up_sequence":["2-4 ordered steps"],'
                + '"cautions":["uncertainties and claims not to make"]}.'
            ),
        },
    ]
    text = _call_unsloth(messages, temperature=0.2, max_tokens=1200, json_mode=True, settings=settings)
    data = _extract_json(text)
    if isinstance(data, dict):
        return data
    return {
        "positioning_angle": (text or "").strip(),
        "contact_persona": "Relevant hiring or talent leader",
        "recommended_channel": "LinkedIn",
        "opening_message": "",
        "evidence_to_reference": [],
        "questions_to_ask": [],
        "follow_up_sequence": [],
        "cautions": ["Review the generated angle against source evidence before sending."],
    }
