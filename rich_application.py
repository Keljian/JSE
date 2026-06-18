"""
rich_application.py — shared engine for context-grounded application generation.

Pipeline:  assemble rich context (context_library)
        -> author tailored resume + cover letter (Gemini / Claude, via REST)
        -> render clean .docx (hybrid_renderer)
        -> verify every claim against the candidate's real evidence (review pass)

Used by both the CLI (generate_application.py) and the app bridge (python_bridge:
command_docs_generate_rich). REST-only on purpose — no google.generativeai / requests
dependency, so it runs in any interpreter that has python-docx + pdfplumber.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import context_library as clib
from hybrid_renderer import render_markdown_to_docx, render_cover_letter_to_docx

DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# Positioning brief (June 2026) — appended to every authoring system prompt.
# Single source of truth for narrative framing; update here when strategy moves.
# --------------------------------------------------------------------------- #
POSITIONING_BRIEF = """CANDIDATE POSITIONING (apply to every document; never contradict the evidence):
- Single identity: a technology leader for businesses whose product is physical, technical or creative work — the builder-practitioner who creates structure and foundations where none exist, and works in the tools himself.
- Narrative anchors: Flavorite (built the IT function from scratch; MSP and vendor governance; quantified multi-million savings), EPSA (Salesforce CPQ delivery), Bosch (commercial and creative-environment fluency), Firetail and the honours capstone (deep technical capability with real hardware).
- SENIOR LEADERSHIP roles: lead with leadership scope, structure-building, vendor/MSP governance, and quantified commercial outcomes. The in-progress engineering degree is supporting evidence of practitioner fluency and the long-run IT/OT path — mention it late and briefly, never lead with it, never apologise for it.
- ENGINEERING roles: lead with what was built (hardware, systems, measurable outcomes), never headcount or generic leadership language.
- The recent consulting period is a deliberate investment in the degree's heavy phase plus part-time delivery. Frame it exactly that way; never as unemployment, a gap, or job-seeking.
- Acknowledge genuine gaps honestly in one clause where the ad makes them relevant (e.g. no ITIL certification, sector unfamiliarity); do not over-explain or volunteer them unprompted.
- NEVER mention salary expectations, availability constraints, family logistics, or personal scheduling in any document."""


RESUME_SYSTEM = """You are an expert Australian resume writer. Write a tailored resume in **Markdown** for the target role, using ONLY the candidate's real evidence supplied below.

Rules:
- Use `## SECTION` for section headings, `### Company | Role | Dates` for each role, `* ` for achievement bullets, `**bold**` sparingly.
- If the evidence gives no date for a role, use `Prior experience` in its Dates position; never invent a date.
- Include a `## KEY SKILLS` section grouped under bold sub-labels.
- Lead with the evidence most relevant to THIS role. Mirror the job ad's terminology only where the candidate genuinely matches.
- NEVER invent or inflate employers, dates, titles, metrics, tools, or certifications beyond what the evidence states. If the evidence says "contributed to", do not write "led". Grounding over polish.
- Do NOT include a name/contact header (the renderer adds it). Start at the professional profile.
- Output ONLY the resume Markdown — no preamble.""" + "\n\n" + POSITIONING_BRIEF

COVER_SYSTEM = """You are an expert Australian cover-letter writer. Write a tailored cover letter for the target role, grounded ONLY in the candidate's real evidence supplied below, and mirroring the authentic voice/tone of the candidate's PRIOR COVER LETTERS provided.

Structure (plain text):
- Date (use the supplied date)
- Recipient name + company + location IF known from the job, else omit
- `Re: <role title>`
- `Dear <name or 'Hiring Manager'>,`
- 3–4 concise body paragraphs mapping the candidate's real achievements to the role's needs, using real metrics from the evidence.
- `Yours sincerely,`
- Candidate full name
Rules: keep the entire letter to 400 words or fewer so it fits on one page; never invent or inflate facts; don't include a sender contact header (the renderer adds it). Output ONLY the letter text.""" + "\n\n" + POSITIONING_BRIEF

REVIEW_SYSTEM = """You are a strict pre-submission fact-checker for a job application. You are given EVIDENCE (the candidate's real prior documents), the JOB, and a DRAFT resume + cover letter.

Check EVERY specific claim in the DRAFT — employers, titles, dates, team sizes, dollar figures, percentages, technologies, certifications — against the EVIDENCE. Protect the candidate from sending anything false or overstated.

Return ONLY JSON (no markdown fence):
{
  "grounding_issues": [{"text": "<exact phrase from draft>", "issue": "<why not supported by evidence>"}],
  "overstatements": [{"text": "<phrase>", "issue": "<how it overstates>"}],
  "missing_must_haves": ["<job requirement not addressed>"],
  "verdict": "ready | needs_review | rework",
  "summary": "<one-line assessment>"
}
Only flag a grounding_issue when the claim genuinely cannot be traced to the EVIDENCE."""


# --------------------------------------------------------------------------- #
# Provider callers (REST)
# --------------------------------------------------------------------------- #
def _call_gemini(api_key, model, system, user, thinking_budget=2048, max_output_tokens=20000):
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_output_tokens,
                             "thinkingConfig": {"thinkingBudget": thinking_budget}},
    }

    def generate(model_name):
        return _http_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}",
            payload,
        )

    try:
        data = generate(model)
    except urllib.error.HTTPError as e:
        # A retired/renamed model 404s; degrade to the known-good default
        # instead of failing the whole generation.
        if e.code == 404 and model != DEFAULT_GEMINI_MODEL:
            data = generate(DEFAULT_GEMINI_MODEL)
        else:
            raise
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
    if not text:
        raise RuntimeError(f"Empty Gemini response (finishReason={cand.get('finishReason')}, usage={data.get('usageMetadata')})")
    return text


def _call_claude(api_key, model, system, user):
    payload = {"model": model, "max_tokens": 8192, "temperature": 0.3,
               "system": system, "messages": [{"role": "user", "content": user}]}
    data = _http_json(
        "https://api.anthropic.com/v1/messages",
        payload,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    return "\n".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")


def build_caller(settings):
    """Return (caller(system,user)->text, provider_label) from profile settings."""
    settings = settings or {}
    provider = (settings.get("document_ai_provider") or settings.get("doc_ai_provider") or "gemini").lower()

    gem_key = (settings.get("gemini_api_key") or "").strip()
    cla_key = (settings.get("claude_api_key") or "").strip()

    if provider == "claude" and cla_key:
        model = settings.get("claude_model") or DEFAULT_CLAUDE_MODEL
        return (lambda s, u: _call_claude(cla_key, model, s, u)), f"Claude ({model})"
    if provider == "gemini" and gem_key:
        model = settings.get("gemini_model") or DEFAULT_GEMINI_MODEL
        return (lambda s, u: _call_gemini(gem_key, model, s, u)), f"Gemini ({model})"
    # provider is 'local' or key missing — fall back to whichever cloud key exists
    if gem_key:
        model = settings.get("gemini_model") or DEFAULT_GEMINI_MODEL
        return (lambda s, u: _call_gemini(gem_key, model, s, u)), f"Gemini ({model})"
    if cla_key:
        model = settings.get("claude_model") or DEFAULT_CLAUDE_MODEL
        return (lambda s, u: _call_claude(cla_key, model, s, u)), f"Claude ({model})"
    raise RuntimeError("Rich generation needs a Gemini or Claude API key in Settings "
                       "(the local model is not used for authoring).")


# --------------------------------------------------------------------------- #
# Cached sessions — the 3 calls per application share one big evidence prefix.
# Caching it means we pay full price for that prefix once, then a fraction on
# the reuse (Gemini explicit cache / Claude cache_control). The shared prefix is
# a GENERIC system + the evidence/job context; only the short TASK varies.
# --------------------------------------------------------------------------- #
GENERIC_SYSTEM = (
    "You are an expert Australian job-application writer and reviewer. You are given a CANDIDATE EVIDENCE "
    "LIBRARY (the candidate's real prior documents) and a target JOB. Use ONLY that real evidence — never "
    "invent or inflate (if the evidence says 'contributed to', do not write 'led'). Each message ends with a "
    "TASK; do exactly what it asks and output only what it asks for."
    "\n\n" + POSITIONING_BRIEF
)

RESUME_TASK = (
    "TASK: Using only the evidence and job provided above, write a tailored resume in **Markdown**.\n"
    "- `## SECTION` headings, `### Company | Role | Dates` per role, `* ` achievement bullets, `**bold**` sparingly.\n"
    "- If the evidence gives no date for a role, use `Prior experience` in its Dates position; never invent a date.\n"
    "- Include a `## KEY SKILLS` section grouped under bold sub-labels.\n"
    "- Lead with the evidence most relevant to THIS role; mirror the ad's terminology only where genuinely matched.\n"
    "- No name/contact header (the renderer adds it). Start at the professional profile.\n"
    "Output ONLY the resume Markdown — no preamble."
)


def cover_task(today, name):
    return (
        "TASK: Using only the evidence and job above, write a tailored cover letter, mirroring the authentic "
        "voice/tone of the candidate's PRIOR COVER LETTERS.\n"
        f"Structure (plain text): date ({today}); recipient name+company+location IF known else omit; "
        "`Re: <role title>`; `Dear <name or 'Hiring Manager'>,`; 3–4 concise evidence-grounded paragraphs with real "
        f"metrics; `Yours sincerely,`; {name}.\n"
        "Keep the entire letter to 400 words or fewer so it fits on one page. "
        "No sender contact header (the renderer adds it). Output ONLY the letter text."
    )


def review_task(resume_md, cover_txt):
    return (
        "TASK: Fact-check the DRAFT below against the EVIDENCE provided above. Check every specific claim — "
        "employers, titles, dates, team sizes, dollar figures, percentages, tools, certifications. Flag anything "
        "not supported by the evidence and any overstatement; list job must-haves not addressed.\n"
        "Return ONLY JSON (no fence): {\"grounding_issues\":[{\"text\":\"\",\"issue\":\"\"}],"
        "\"overstatements\":[{\"text\":\"\",\"issue\":\"\"}],\"missing_must_haves\":[],"
        "\"verdict\":\"ready|needs_review|rework\",\"summary\":\"\"}\n\n"
        f"DRAFT RESUME:\n{resume_md}\n\nDRAFT COVER LETTER:\n{cover_txt}"
    )


def _http_json(url, body=None, method=None, headers=None, timeout=300, retries=3):
    """POST/GET JSON with bounded retries on transient failures.

    Retries 429/500/502/503 and network-level errors with linear backoff;
    4xx config errors (400/401/403/404) raise immediately so callers can make
    their own fallback decisions (e.g. retired-model 404 handling)."""
    data = json.dumps(body).encode() if body is not None else None
    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                last_error = e
                time.sleep(4 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                last_error = e
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise last_error


class _GeminiSession:
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, key, model, system, context_text, log=None):
        self.key, self.model, self.system, self.context = key, model, system, context_text
        self.log = log
        self.cache = None
        try:
            data = _http_json(f"{self.BASE}/cachedContents?key={key}", {
                "model": f"models/{model}",
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": context_text}]}],
                # Long TTL: the three sequential resume/cover/review calls on a slow
                # preview model can otherwise outlive a short cache, and an expired
                # cache makes generateContent 404 ("CachedContent not found").
                "ttl": "1800s",
            })
            self.cache = data.get("name")
            if log and self.cache:
                log(f"Cached evidence context ({self.cache.split('/')[-1]}) — reused across resume/cover/review.")
        except Exception as e:
            if log:
                log(f"Explicit cache unavailable ({str(e)[:70]}); relying on Gemini implicit caching.")
        self.label = f"Gemini ({model})" + (" + cache" if self.cache else "")

    def _inline_body(self, task, gen):
        return {"systemInstruction": {"parts": [{"text": self.system}]},
                "contents": [{"role": "user", "parts": [{"text": self.context + "\n\n" + task}]}],
                "generationConfig": gen}

    def ask(self, task, thinking_budget=2048, max_output_tokens=20000):
        gen = {"temperature": 0.3, "maxOutputTokens": max_output_tokens,
               "thinkingConfig": {"thinkingBudget": thinking_budget}}
        url = f"{self.BASE}/models/{self.model}:generateContent?key={self.key}"
        if self.cache:
            body = {"cachedContent": self.cache, "contents": [{"role": "user", "parts": [{"text": task}]}],
                    "generationConfig": gen}
        else:
            body = self._inline_body(task, gen)
        try:
            data = _http_json(url, body)
        except urllib.error.HTTPError as e:
            # An expired/evicted explicit cache makes generateContent 404 (or 403)
            # mid-run. Drop the cache and retry the same task inline so the
            # generation still completes instead of failing the whole application.
            if self.cache and e.code in (403, 404):
                if self.log:
                    self.log(f"Cached context unavailable ({e.code}); retrying inline.")
                self.cache = None
                data = _http_json(url, self._inline_body(task, gen))
            elif e.code == 404 and self.model != DEFAULT_GEMINI_MODEL:
                # The model itself is gone (retired/renamed). Fall back to the
                # default model so generation degrades instead of failing.
                if self.log:
                    self.log(f"Model {self.model} unavailable (404); falling back to {DEFAULT_GEMINI_MODEL}.")
                self.model = DEFAULT_GEMINI_MODEL
                self.label = f"Gemini ({self.model})"
                url = f"{self.BASE}/models/{self.model}:generateContent?key={self.key}"
                data = _http_json(url, self._inline_body(task, gen))
            else:
                raise
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
        if not text:
            raise RuntimeError(f"Empty Gemini response (finishReason={cand.get('finishReason')}, usage={data.get('usageMetadata')})")
        return text

    def close(self):
        if self.cache:
            try:
                _http_json(f"{self.BASE}/{self.cache}?key={self.key}", method="DELETE")
            except Exception:
                pass


class _ClaudeSession:
    def __init__(self, key, model, system, context_text, log=None):
        self.key, self.model = key, model
        # cache_control on the big context block → Anthropic caches it; the short
        # per-call TASK (the user message) is all that's re-billed at full rate.
        self.system = [
            {"type": "text", "text": system},
            {"type": "text", "text": context_text, "cache_control": {"type": "ephemeral"}},
        ]
        self.label = f"Claude ({model}) + cache"
        if log:
            log("Claude prompt caching enabled on the evidence context.")

    def ask(self, task, max_output_tokens=8192, **_):
        payload = {"model": self.model, "max_tokens": max_output_tokens, "temperature": 0.3,
                   "system": self.system, "messages": [{"role": "user", "content": task}]}
        data = _http_json("https://api.anthropic.com/v1/messages", payload,
                          headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        return "\n".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")

    def close(self):
        pass


def build_session(settings, system, context_text, log=None):
    settings = settings or {}
    provider = (settings.get("document_ai_provider") or settings.get("doc_ai_provider") or "gemini").lower()
    gem = (settings.get("gemini_api_key") or "").strip()
    cla = (settings.get("claude_api_key") or "").strip()
    if provider == "claude" and cla:
        return _ClaudeSession(cla, settings.get("claude_model") or DEFAULT_CLAUDE_MODEL, system, context_text, log)
    if provider == "gemini" and gem:
        return _GeminiSession(gem, settings.get("gemini_model") or DEFAULT_GEMINI_MODEL, system, context_text, log)
    if gem:
        return _GeminiSession(gem, settings.get("gemini_model") or DEFAULT_GEMINI_MODEL, system, context_text, log)
    if cla:
        return _ClaudeSession(cla, settings.get("claude_model") or DEFAULT_CLAUDE_MODEL, system, context_text, log)
    raise RuntimeError("Rich generation needs a Gemini or Claude API key in Settings.")


def _parse_review(raw):
    raw = re.sub(r"^```(json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else {"verdict": "needs_review", "summary": "review parse failed"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe(value):
    return re.sub(r"\s+", "_", re.sub(r"[^A-Za-z0-9._ -]+", "", value or "application").strip())[:80] or "application"


def _personal_info_from_resume(personal_info, resume_text):
    """Fill blank renderer contact fields from the candidate's source resume."""
    info = dict(personal_info or {})
    text = str(resume_text or "")

    if not (info.get("email") or "").strip():
        match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        if match:
            info["email"] = match.group(0)
    if not (info.get("phone") or "").strip():
        match = re.search(r"(?<!\d)(?:\+?61\s?[2-478]|0[2-478])(?:[\s()-]*\d){8}(?!\d)", text)
        if match:
            info["phone"] = re.sub(r"\s+", " ", match.group(0)).strip()
    if not (info.get("linkedin") or "").strip():
        match = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w%-]+/?", text, re.I)
        if match:
            info["linkedin"] = match.group(0)

    supplied_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
    if supplied_name.lower() in {"", "candidate", "curriculum vitae"}:
        excluded = {"resume", "curriculum vitae", "professional profile", "professional summary",
                    "profile", "summary", "contact", "contact details", "key selection criteria",
                    "key selection criteria responses", "selection criteria", "selection criteria responses",
                    "cover letter", "application", "application response"}
        role_words = {"technology", "leader", "manager", "consultant", "engineer", "director",
                      "specialist", "professional", "executive", "analyst", "developer", "architect"}
        for raw in text.splitlines()[:20]:
            line = re.sub(r"^name\s*:\s*", "", raw.strip(), flags=re.I)
            # Resume headers often put "Name | role title" in one paragraph.
            line = re.split(r"\s+[|•]\s+", line, maxsplit=1)[0]
            line = re.sub(r"\s+", " ", line).strip(" |•\t")
            words = line.split()
            if (2 <= len(words) <= 5 and line.lower() not in excluded
                    and "@" not in line and not re.search(r"\d|https?://|linkedin", line, re.I)
                    and not any(word.casefold() in role_words for word in words)
                    and all(re.fullmatch(r"[A-Za-z][A-Za-z'’-]*", word) and word[0].isupper()
                            for word in words)):
                info["first_name"] = words[0]
                info["last_name"] = " ".join(words[1:])
                break
    return info


def _review(caller, context_block, job, resume_md, cover_txt):
    user = (f"JOB: {job['title']} @ {job['company']}\n{(job['description'] or '')[:3000]}\n\n"
            f"EVIDENCE:\n{context_block[:22000]}\n\n"
            f"DRAFT RESUME:\n{resume_md}\n\nDRAFT COVER LETTER:\n{cover_txt}")
    raw = caller(REVIEW_SYSTEM, user).strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else {"verdict": "needs_review", "summary": "review parse failed"}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def generate_rich(job_id, profile_id=1, settings=None, personal_info=None,
                  source_resume_text=None,
                  log=print, out_dir="applications", conn=None, do_review=True):
    owns_conn = conn is None
    if conn is None:
        conn = sqlite3.connect(str(clib.DB_PATH)); conn.row_factory = sqlite3.Row
    clib.ensure_schema(conn)

    job = conn.execute(
        "SELECT id,title,company,location,description,salary,closing_date,contact_person "
        "FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise ValueError(f"Job {job_id} not found.")

    info = personal_info or {"first_name": "Candidate", "last_name": "", "phone": "",
                             "email": "", "linkedin": ""}

    log("Assembling context from your prior applications…")
    job_text = f"{job['title']}\n{job['company']}\n{job['description'] or ''}"
    context_block, selected = clib.assemble_context(conn, job_text)
    base = conn.execute("SELECT text FROM context_documents WHERE doc_type='resume' ORDER BY char_len DESC LIMIT 1").fetchone()
    base_resume = str(source_resume_text or (base["text"] if base else ""))
    info = _personal_info_from_resume(info, base_resume)
    log(f"Context: {len(context_block):,} chars from {len(selected)} prior documents.")

    job_brief = (f"TARGET ROLE: {job['title']}\nEMPLOYER: {job['company']}\nLOCATION: {job['location']}\n"
                 f"CLOSING: {job['closing_date']}\nCONTACT: {job['contact_person'] or '(unknown)'}\n\n"
                 f"JOB ADVERTISEMENT:\n{(job['description'] or '')[:6000]}\n\n{context_block}\n\n"
                 f"CANDIDATE BASE RESUME (reference):\n{base_resume[:6000]}")
    today = datetime.now().strftime("%d %B %Y")

    # One cached session shares the big evidence prefix across all three calls.
    session = build_session(settings, GENERIC_SYSTEM, job_brief, log=log)
    provider_label = session.label
    try:
        log(f"Authoring resume with {provider_label}…")
        resume_md = session.ask(RESUME_TASK).strip()
        log("Authoring cover letter…")
        cover_txt = session.ask(cover_task(today, f"{info['first_name']} {info['last_name']}")).strip()

        review = {}
        if do_review:
            log("Verifying claims against your evidence…")
            try:
                review = _parse_review(session.ask(review_task(resume_md, cover_txt)))
            except Exception as e:
                review = {"verdict": "needs_review", "summary": f"review failed: {e}"}
    finally:
        try:
            session.close()
        except Exception:
            pass

    out = Path(out_dir); out.mkdir(exist_ok=True)
    safe = _safe(job["title"])
    rpath = out / f"{safe}_targeted_resume.docx"
    cpath = out / f"{safe}_cover_letter.docx"
    mdpath = out / f"{safe}_application_content.json"
    render_markdown_to_docx(resume_md, rpath, personal_info=info)
    render_cover_letter_to_docx(cover_txt, cpath, personal_info=info)
    mdpath.write_text(json.dumps({
        "resume_markdown": resume_md, "cover_letter_text": cover_txt,
        "evidence_used": selected, "review": review, "provider": provider_label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    log(f"Done. Review verdict: {review.get('verdict', 'n/a')}.")
    if owns_conn:
        conn.close()
    return {
        "resume_path": str(rpath), "cover_letter_path": str(cpath),
        "content_json_path": str(mdpath), "provider": provider_label,
        "review": review, "evidence_used": selected,
        "resume_markdown": resume_md, "cover_letter_text": cover_txt,
    }
