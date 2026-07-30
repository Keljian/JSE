"""Application document content generation.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
from config import MY_INFO
import concurrency
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
    APPLICATION_DOCUMENT_SYSTEM_PROMPT,
)

def review_application_kit(application_payload, settings=None, log_callback=None):
    """Use the local model to review an application kit for quality and learning signals."""
    log = log_callback or (lambda _message: None)
    fallback = {
        "strongest_evidence_used": [],
        "missing_evidence": [],
        "overclaimed_risks": [],
        "alignment_score": 0,
        "recommended_manual_checks": ["Review generated documents manually before applying."],
        "fragments_to_strengthen": [],
        "fallback": True,
    }
    if (settings or {}).get("force_fallback") or not _local_is_configured():
        return fallback, "deterministic fallback"
    local_settings = {**(settings or {}), "doc_ai_provider": "local"}
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict reviewer of a generated application kit (tailored resume + cover letter) for one Australian role. "
                "Your job is to catch truthfulness risk, weak claims, and missed leverage — NOT to rewrite the documents. "
                "Return ONLY one valid JSON object. No <think> tags, no markdown, no prose. "
                "Australian English spelling. Be sceptical, not encouraging."
            ),
        },
        {
            "role": "user",
            "content": f"""Review the supplied application kit. Return JSON with EXACTLY this shape:

{{
  "alignment_score": int 0-100 (how well the kit hits the ad's named requirements, evidence-anchored),
  "strongest_evidence_used":  [3-6 specific resume artefacts the kit leaned on well — name role/employer/outcome],
  "missing_evidence":          [2-5 ad requirements the kit failed to evidence even though the resume could have],
  "overclaimed_risks":         [items in the resume/letter that go beyond what the base resume actually supports — flag any number, scale, or sector claim not in the source],
  "recommended_manual_checks": [2-4 things the candidate should verify before sending — specific facts, claims, or framing decisions],
  "fragments_to_strengthen":   [2-5 themes that, with a stronger reusable fragment in memory, would have made this kit sharper]
}}

SCORING GUIDE
- 90+: every named ad requirement has resume-anchored evidence; cover letter ties evidence to specific outcomes; no overclaim risk.
- 75-89: most requirements covered, 1-2 weak bridges, no overclaim risk.
- 60-74: meaningful gaps OR generic claims that could apply to many ads.
- <60: significant gap, generic positioning, or at least one overclaim risk.

APPLICATION KIT:
---
{json.dumps(application_payload, ensure_ascii=False)[:18000]}
---""",
        },
    ]
    try:
        response, provider = _call_document_ai(
            local_settings, messages, temperature=0.05, max_tokens=3000, json_mode=True
        )
        data = _extract_json(response)
        if not data:
            log("Local application review returned malformed JSON; using deterministic fallback.")
            return fallback, "deterministic fallback"
        return {**fallback, **data, "fallback": False}, provider
    except Exception as exc:
        log(f"Local application review failed; using deterministic fallback: {exc}")
        return fallback, "deterministic fallback"


def generate_application_documents(
    base_resume_text: str,
    job_id: int,
    log_callback=None,
    profile_id=1,
    position_description_text="",
):
    """Generate tailored resume and cover letter using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()
    log = log_callback if log_callback else print

    if not _local_is_configured():
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")

    if not base_resume_text:
        raise ValueError("Base resume text cannot be empty.")

    job_data = db.get_job_details(job_id)
    if not job_data:
        raise ValueError(f"Job with ID {job_id} not found.")

    job_title, company, job_description, pdf_text = (
        job_data['title'], job_data['company'], job_data['description'], job_data['pdf_text']
    )
    fit_analysis = job_data.get('ai_analysis') or "No prior fit analysis is available."

    full_job_text = job_description or ""
    if pdf_text:
        full_job_text += f"\n\n--- JOB DETAILS FROM PDF ---\n{pdf_text}"
    if position_description_text:
        full_job_text = (
            f"--- UPLOADED POSITION DESCRIPTION ---\n{position_description_text}\n\n"
            f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_job_text}"
        )
        log(f"Using uploaded position description ({len(position_description_text)} chars).")

    log(f"Generating formatted, tailored resume for {job_title}...")
    resume_prompt = f"""You are a senior Australian resume writer producing a tailored single-document resume for ONE specific application: '{job_title}' at '{company}'.

OUTPUT CONTRACT
- Output ONLY the resume markdown. No preamble, no commentary, no <think> tags, no code fences.
- The response MUST start with the original resume header name exactly as supplied, formatted as `# <Candidate Name>`.
- Use Australian English spelling throughout (organisation, optimise, programme/program, etc.).

EVIDENCE DISCIPLINE
1. Preserve every real employer, title, date, qualification, and contact detail exactly as it appears in the original resume. Never alter dates or invent dates.
2. Never invent achievements, metrics, responsibilities, tools, certifications, sectors, scale, or relationships. If a number is not in the source, do not state one.
3. Where the fit analysis names a gap, reposition adjacent evidence honestly — do not paper over it with vague claims.
4. Mirror the ad's language only where the resume genuinely backs it. Do not echo ad keywords that are not evidenced.

TAILORING STRATEGY
- The top third of the resume (summary + core capabilities + first role's leading bullets) must carry the application strategy for THIS ad.
- Reorder roles to keep chronology, but reorder bullets within each role to surface what matters for this job first.
- Older / less relevant roles may be compressed to 2-3 bullets; do not delete them outright if the source includes them.
- Each bullet starts with a strong verb (Led, Delivered, Owned, Designed, Reduced, Standardised, Migrated, Negotiated, Established, Recovered). No "Responsible for" / "Duties included".
- Where a real metric exists in the source, surface it in **bold**. Do not fabricate one.

MARKDOWN FORMAT (the downstream renderer depends on this exactly)
- `# Name` — candidate's name, top line only.
- Immediately under the name, contact lines (Phone, Email, LinkedIn) on separate lines. No special characters or icons.
- `## SECTION HEADING` for each section. Recommended order: PROFESSIONAL SUMMARY, CORE CAPABILITIES, PROFESSIONAL EXPERIENCE, EDUCATION, CERTIFICATIONS (if present in source), TECHNICAL SKILLS.
- `### Job Title` on its own line for each role.
- Immediately under, on its own line: `**Company Name** | City, State | Month Year - Month Year` (use exactly the city/state/dates from the source).
- `* Bullet text.` for every achievement/responsibility bullet. Use `**bold**` inline for key metrics, named tools, or platforms — sparingly.
- PROFESSIONAL SUMMARY is a single paragraph of 3-5 sentences, ad-targeted. CORE CAPABILITIES is a bulleted list of 8-12 capability phrases ordered by relevance to the ad.

LENGTH TARGET
- 2 pages of A4 equivalent. Trim padding before adding length.

INPUTS

Fit Analysis (use this to choose tailoring priorities):
---
{fit_analysis}
---

Job Advertisement (the target of all tailoring decisions):
---
{full_job_text}
---

Original Resume (source of truth — every fact must come from here):
---
{base_resume_text}
---
"""

    try:
        tailored_resume_draft = _call_unsloth(
            messages=[{"role": "user", "content": resume_prompt}],
            temperature=0.25,
            max_tokens=8000,
        )
    except Exception as e:
        log(f"Error generating tailored resume: {e}")
        raise
    log("Tailored resume draft generated.")

    log(f"Generating cover letter for {job_title} at {company}...")
    cover_letter_prompt = f"""You are a senior Australian cover letter writer. Write the cover letter body for '{job_title}' at '{company}'.

OUTPUT CONTRACT
- Output ONLY the cover letter body. No subject, no date, no addresses, no "Dear ..." salutation, no signoff line. The downstream renderer adds those.
- Start with the first paragraph of the letter. Do NOT use Markdown headings (`#`, `##`, `###`) or list bullets (`*`, `-`). Plain paragraphs separated by blank lines. `**bold**` is permitted, sparingly.
- Australian English spelling.

VOICE
- Confident, specific, conversational-professional. Plain sentences.
- Banned phrases: "I am writing to apply", "please find attached", "thank you for your consideration", "passionate", "dynamic", "results-driven", "team player", "proven track record", "wear many hats", "go the extra mile", "synergy".

EVIDENCE DISCIPLINE
- Every claim must trace to the tailored resume, fit analysis, or job ad. Do not invent facts, metrics, relationships, certifications, sectors, or scale.
- Where the fit analysis names a gap, address it once, honestly, with the strongest adjacent evidence. Do not apologise and do not pretend it isn't there.

STRUCTURE (4 paragraphs, ~300-380 words total)
1. Opening (~3 sentences): name the role and one concrete anchor — a specific past role/project/outcome from the tailored resume that maps to the ad's headline requirement. No throat-clearing.
2. Evidence paragraph 1 (~4 sentences): the strongest fit claim from the analysis, anchored to a specific employer/project in the resume. Mirror ad language only where the resume backs it.
3. Evidence paragraph 2 (~4 sentences): the second strongest claim. If a meaningful gap exists, handle it here in one honest sentence framed around the adjacent strength.
4. Forward-looking close (~2-3 sentences): what the candidate would prioritise in the first 90 days, grounded in the ad's named priorities. End with a real call to discuss specific examples in interview — no "thank you for considering".

INPUTS

Fit Analysis (use this to choose the argument):
---
{fit_analysis}
---

Job Advertisement (the target):
---
{full_job_text}
---

Tailored Resume (the source of every factual claim in the letter):
---
{tailored_resume_draft}
---
"""

    try:
        cover_letter_draft = _call_unsloth(
            messages=[{"role": "user", "content": cover_letter_prompt}],
            temperature=0.55,
            max_tokens=2500,
        )
    except Exception as e:
        log(f"Error generating cover letter: {e}")
        raise
    log("Cover letter draft generated.")

    return tailored_resume_draft, cover_letter_draft


def generate_template_application_content(
    job_id: int,
    resume_text: str,
    settings=None,
    log_callback=None,
    position_description_text="",
    additional_candidate_context="",
):
    settings = _settings_for_ai_task(settings, "document_ai_provider")
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()
    log = log_callback or print
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job with ID {job_id} not found.")
    if not resume_text:
        raise ValueError("Base resume text cannot be empty.")

    additional_candidate_context = str(
        additional_candidate_context
        or (job["additional_candidate_context"] if "additional_candidate_context" in job.keys() else "")
        or ""
    ).strip()
    additional_context_block = f"""
ADDITIONAL CANDIDATE EVIDENCE (USER-SUPPLIED FOR THIS APPLICATION):
Treat this as first-party evidence. Use only what is stated; do not infer or embellish beyond it. If it expresses a preference or instruction rather than a fact, use it as writing guidance.
---
{additional_candidate_context[:12000]}
---
""" if additional_candidate_context else ""

    uploaded_position_description = position_description_text or job["position_description_text"] or ""
    full_job_text = job["description"] or ""
    if job["pdf_text"]:
        full_job_text += f"\n\n--- ADDITIONAL JOB TEXT ---\n{job['pdf_text']}"
    if uploaded_position_description:
        full_job_text = (
            f"--- UPLOADED POSITION DESCRIPTION ---\n{uploaded_position_description}\n\n"
            f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_job_text}"
        )
        log(f"Using uploaded position description ({len(uploaded_position_description)} chars).")
    company_context = job["company_intelligence"] or "{}"
    provider = ((settings or {}).get("doc_ai_provider") or "local").lower()
    is_local_provider = provider == "local"
    # Qwen3 32K context budget — keep inputs generous so the model has the
    # source material to ground every claim, and give the JSON output enough
    # room for 3-5 roles with 4-8 evidence-anchored bullets each.
    resume_limit = 14000 if is_local_provider else 18000
    job_limit = 10000 if is_local_provider else 12000
    cover_resume_limit = 9000 if is_local_provider else 10000
    cover_job_limit = 7000 if is_local_provider else 9000
    output_tokens = 8000 if is_local_provider else 10000
    lane_context = (settings or {}).get("lane_context") or {}
    lane = lane_context.get("lane") or {}
    lane_settings = lane_context.get("settings") or {}
    lane_fragments = lane_context.get("fragments") or []
    fragment_lines = []
    for fragment in lane_fragments[:30]:
        fragment_lines.append(
            f"- [{fragment.get('id')}] {fragment.get('theme')}: {fragment.get('claim')} "
            f"Guidance: {fragment.get('reuse_guidance') or ''}"
        )
    lane_prompt_context = f"""
LANE / POSITIONING STRATEGY:
Name: {lane.get('name') or ''}
Intent: {lane_settings.get('lane_intent') or ''}
Target titles: {lane_settings.get('target_titles') or ''}
Target domains: {lane_settings.get('target_domains') or ''}
Seniority: {lane_settings.get('seniority') or ''}
Document strategy: {lane_settings.get('document_strategy') or ''}
Must-have signals: {lane_settings.get('must_have_terms') or ''}
Avoid signals: {lane_settings.get('avoid_terms') or ''}

SELECTED CANDIDATE FRAGMENTS:
{chr(10).join(fragment_lines) if fragment_lines else 'No lane-selected fragments were available.'}
"""

    def build_resume_messages(local_retry=False):
        retry_resume_limit = 6000 if local_retry else resume_limit
        retry_job_limit = 3500 if local_retry else job_limit
        retry_analysis_limit = 2500 if local_retry else None
        fit_analysis = job['ai_analysis'] or 'No prior analysis is available.'
        if retry_analysis_limit:
            fit_analysis = fit_analysis[:retry_analysis_limit]
        return [
        {"role": "system", "content": APPLICATION_DOCUMENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"""Create structured content for a targeted application.

CANDIDATE:
{MY_INFO.get('first_name', '')} {MY_INFO.get('last_name', '')}
{MY_INFO.get('phone', '')}
{MY_INFO.get('email', '')}
{MY_INFO.get('linkedin', '')}

{lane_prompt_context}

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}
Application URL: {job['application_url'] or job['url'] or ''}
Salary / rate: {job['salary'] or ''}
Closing date: {job['closing_date'] or ''}
Contact: {job['contact_person'] or ''} {job['contact_email'] or ''} {job['contact_phone'] or ''}

FIT ANALYSIS:
---
{fit_analysis}
---

COMPANY INTELLIGENCE:
---
{company_context}
---

JOB ADVERTISEMENT:
---
{full_job_text[:retry_job_limit]}
---

BASE RESUME:
---
{resume_text[:retry_resume_limit]}
---

{additional_context_block}""",
        },
    ]

    messages = build_resume_messages()
    try:
        response, provider_label = _call_document_ai(
            settings or {}, messages, temperature=0.2, max_tokens=output_tokens, json_mode=True
        )
    except Exception as exc:
        if not is_local_provider:
            raise
        log(f"Local document AI failed on the full application prompt; retrying with compact context. Error: {exc}")
        response, provider_label = _call_document_ai(
            settings or {}, build_resume_messages(local_retry=True),
            temperature=0.2, max_tokens=5000, json_mode=True,
        )
    data = _extract_json(response)
    if not data:
        log("The selected AI returned malformed JSON. Asking it to repair the response...")
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Convert the supplied text into one valid JSON object only. "
                    "Do not add commentary, markdown fences, or new content. "
                    "Escape line breaks inside string values as \\n. "
                    "If a field is incomplete, keep the valid completed content and close the JSON correctly."
                ),
            },
            {"role": "user", "content": response[:30000]},
        ]
        repaired, _ = _call_document_ai(
            settings or {}, repair_messages, temperature=0.0, max_tokens=output_tokens, json_mode=True
        )
        data = _extract_json(repaired)
    if not data:
        raise ValueError(f"The selected AI did not return valid JSON. Response started: {response[:300]}")
    log("Generating cover letter content separately...")
    cover_messages = [
        {
            "role": "system",
            "content": (
                "You are a senior Australian cover letter writer for the candidate described in the supplied resume. "
                "Return ONLY one valid JSON object. No markdown fences, commentary, or <think> tags. "
                "Escape every internal newline inside strings as \\n.\n\n"
                "VOICE: confident, specific, conversational-professional. Australian English. "
                "Plain sentences over corporate jargon. No 'passionate', 'dynamic', 'team player', "
                "'results-driven', 'I am writing to apply', 'please find attached', 'thank you for your consideration'.\n\n"
                "EVIDENCE DISCIPLINE: every claim must trace to the resume, fit analysis, lane fragments, or job ad. "
                "Do not invent employers, dates, qualifications, certifications, tools, metrics, sectors, or relationships. "
                "Where the fit analysis names a gap, neutralise it once, honestly, with the strongest adjacent evidence.\n\n"
                "STRUCTURE:\n"
                "- subject: 'RE: <Role title> application' (Australian convention).\n"
                "- greeting: 'Dear Hiring Manager,' unless a named contact is supplied.\n"
                "- opening (1 short paragraph): name the role and one concrete anchor — a specific resume artefact, an outcome that maps to the ad, or a relevant sector pattern. No throat-clearing.\n"
                "- body (2 paragraphs in the array): each one is evidence-led. Paragraph 1 anchors the strongest fit claim from the analysis to a specific past role/project. Paragraph 2 covers the second strongest claim and, if there's a gap, addresses it in one sentence without apology.\n"
                "- value_proposition (1 short paragraph): what the candidate would do in the first 90 days, grounded in the ad's named priorities. Avoid generic 'add value'.\n"
                "- closing (1 short paragraph): a real call-to-action — happy to talk through specific examples in interview. No 'thank you for considering'.\n"
                "- signoff: 'Kind regards\\n<Candidate Name>' using the exact name from the resume header.\n\n"
                "LENGTH TARGET: ~300-380 words total. Trim before padding.\n\n"
                "REQUIRED SHAPE:\n"
                "{\"cover_letter\":{\"subject\":\"...\",\"greeting\":\"Dear Hiring Manager,\","
                "\"opening\":\"...\",\"body\":[\"...\",\"...\"],\"value_proposition\":\"...\","
                "\"closing\":\"...\",\"signoff\":\"Kind regards\\n<Candidate Name>\"}}"
            ),
        },
        {
            "role": "user",
            "content": f"""Write the cover letter content for this application.

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}

{lane_prompt_context}

FIT ANALYSIS:
---
{job['ai_analysis'] or 'No prior analysis is available.'}
---

COMPANY INTELLIGENCE:
---
{company_context}
---

JOB ADVERTISEMENT:
---
{full_job_text[:cover_job_limit]}
---

BASE RESUME:
---
{resume_text[:cover_resume_limit]}
---

{additional_context_block}""",
        },
    ]
    try:
        cover_response, _ = _call_document_ai(
            settings or {}, cover_messages, temperature=0.35, max_tokens=5000, json_mode=True
        )
    except Exception as exc:
        if not is_local_provider:
            raise
        log(f"Local document AI failed on the cover letter prompt; retrying with compact context. Error: {exc}")
        compact_cover_messages = [
            cover_messages[0],
            {
                "role": "user",
                "content": f"""Write the cover letter content for this application.

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}

FIT ANALYSIS:
---
{(job['ai_analysis'] or 'No prior analysis is available.')[:1800]}
---

JOB ADVERTISEMENT:
---
{full_job_text[:3000]}
---

BASE RESUME:
---
{resume_text[:4500]}
---

{additional_context_block}""",
            },
        ]
        cover_response, _ = _call_document_ai(
            settings or {}, compact_cover_messages, temperature=0.35, max_tokens=3500, json_mode=True
        )
    cover_data = _extract_json(cover_response)
    if cover_data and isinstance(cover_data.get("cover_letter"), dict):
        data["cover_letter"] = cover_data["cover_letter"]
    elif cover_data:
        cover_keys = {"subject", "greeting", "opening", "body", "value_proposition", "closing", "signoff"}
        flattened = {key: cover_data.get(key) for key in cover_keys if cover_data.get(key)}
        if flattened:
            data["cover_letter"] = flattened
    if not isinstance(data.get("cover_letter"), dict):
        log("Cover letter generation did not return usable content; inserting a manual-review placeholder.")
        data["cover_letter"] = {
            "subject": f"RE: {job['title']} application",
            "greeting": "Dear Hiring Manager,",
            "opening": "Cover letter generation did not return usable content. Please regenerate or review the AI provider settings.",
            "body": [],
            "value_proposition": "",
            "closing": "",
            "signoff": "Kind regards\nCandidate",
        }
    log(f"Application content generated with {provider_label}.")
    return data, provider_label
