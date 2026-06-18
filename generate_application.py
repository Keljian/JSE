"""
generate_application.py — Phase 1b end-to-end generator (standalone, safe to test).

Pipeline:
    target job  ->  assemble rich context (context_library)
                ->  author tailored resume + cover letter as Markdown (Gemini / Claude)
                ->  render clean .docx (hybrid_renderer)

Standalone on purpose: it doesn't touch the running app's command paths. Once the
output is validated we fold this into command_docs_generate.

Key resolution order (Gemini): --key arg  >  env GEMINI_API_KEY  >  profiles.gemini_api_key in DB.

Usage:
    python generate_application.py <job_id> [--provider gemini|claude] [--model NAME] [--key KEY]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import context_library as clib
from hybrid_renderer import render_markdown_to_docx, render_cover_letter_to_docx

try:
    from config import MY_INFO
except Exception:
    MY_INFO = {"first_name": "Candidate", "last_name": "", "phone": "",
               "email": "", "linkedin": ""}

DATA_DIR = clib.DATA_DIR
DB_PATH = clib.DB_PATH


RESUME_SYSTEM = """You are an expert Australian resume writer. Write a tailored resume in **Markdown** for the target role, using ONLY the candidate's real evidence supplied below.

Rules:
- Use `## SECTION` for section headings, `### Company | Role | Dates` for each role, `* ` for achievement bullets, `**bold**` sparingly for emphasis.
- If the evidence gives no date for a role, use `Prior experience` in its Dates position; never invent a date.
- Lead with the evidence most relevant to THIS role. Mirror the job ad's terminology only where the candidate genuinely matches.
- NEVER invent employers, dates, titles, metrics, tools, or certifications that are not present in the supplied evidence. Grounding over polish.
- Do NOT include a name/contact header (the renderer adds it). Start at the professional profile.
- Output ONLY the resume Markdown — no preamble, no commentary."""

COVER_SYSTEM = """You are an expert Australian cover-letter writer. Write a tailored cover letter for the target role, grounded ONLY in the candidate's real evidence supplied below, and mirroring the authentic voice/tone of the candidate's PRIOR COVER LETTERS provided.

Structure (plain text, one item per line / paragraph):
- Date (use the supplied date)
- Recipient name + company + location IF known from the job, else omit
- `Re: <role title>`
- `Dear <name or 'Hiring Manager'>,`
- 3–4 concise body paragraphs that map the candidate's real achievements to the role's needs. Be specific; use real metrics from the evidence.
- `Yours sincerely,`
- Candidate full name
Rules: keep the entire letter to 400 words or fewer so it fits on one page; never invent facts; don't include a sender contact header (the renderer adds it). Output ONLY the letter text."""


REVIEW_SYSTEM = """You are a strict pre-submission fact-checker for a job application. You are given:
1) EVIDENCE — the candidate's REAL prior documents (resumes, cover letters, KSC responses).
2) JOB — the target advertisement.
3) DRAFT — a generated resume + cover letter.

Your job is to protect the candidate from sending anything false or overstated. Check EVERY specific claim in the DRAFT — employers, job titles, dates, team sizes, dollar figures, percentages, technologies, certifications — against the EVIDENCE.

Return ONLY JSON (no markdown fence):
{
  "grounding_issues": [{"text": "<exact phrase from draft>", "issue": "<why it is not supported by the evidence>"}],
  "overstatements": [{"text": "<phrase>", "issue": "<how it overstates>"}],
  "missing_must_haves": ["<job requirement not addressed in the draft>"],
  "verdict": "ready | needs_review | rework",
  "summary": "<one-line overall assessment>"
}
Only list a grounding_issue when the claim genuinely cannot be traced to the EVIDENCE. Be precise, not pedantic."""


def _review(caller, context_block, job, resume_md, cover_txt):
    user = (f"JOB: {job['title']} @ {job['company']}\n{(job['description'] or '')[:3000]}\n\n"
            f"EVIDENCE:\n{context_block[:22000]}\n\n"
            f"DRAFT RESUME:\n{resume_md}\n\nDRAFT COVER LETTER:\n{cover_txt}")
    import json, re
    raw = caller(REVIEW_SYSTEM, user).strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else {"verdict": "needs_review", "summary": "review parse failed", "_raw": raw[:500]}


def _job(conn, job_id):
    row = conn.execute(
        "SELECT id,title,company,location,description,salary,closing_date,contact_person,ai_analysis "
        "FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return row


def _resume_text(conn):
    # prefer the best-scoring indexed resume as the 'base'; fall back to any
    row = conn.execute(
        "SELECT text FROM context_documents WHERE doc_type='resume' ORDER BY char_len DESC LIMIT 1"
    ).fetchone()
    return row["text"] if row else ""


def _gemini_key(args, conn):
    if args.key:
        return args.key
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    row = conn.execute("SELECT gemini_api_key FROM profiles WHERE id=1").fetchone()
    return (row["gemini_api_key"] or "").strip() if row else ""


def _call_gemini(api_key, model, system, user):
    # Direct REST (the google.generativeai lib is broken in this interpreter).
    import urllib.request, json
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 20000,
            # 2.5-Pro "thinks" against the output budget; cap it so tokens go to the document.
            "thinkingConfig": {"thinkingBudget": 2048},
        },
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise RuntimeError(f"Empty response (finishReason={cand.get('finishReason')}, "
                           f"usage={data.get('usageMetadata')})")
    return text


def _call_claude(api_key, model, system, user):
    import urllib.request, json
    payload = {"model": model, "max_tokens": 8192, "temperature": 0.3,
               "system": system, "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return "\n".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("job_id", type=int)
    ap.add_argument("--provider", default="gemini", choices=["gemini", "claude"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--key", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    clib.ensure_schema(conn)
    job = _job(conn, args.job_id)
    if not job:
        print(f"Job {args.job_id} not found."); return

    job_text = f"{job['title']}\n{job['company']}\n{job['description'] or ''}"
    context_block, selected = clib.assemble_context(conn, job_text)
    base_resume = _resume_text(conn)

    job_brief = (f"TARGET ROLE: {job['title']}\nEMPLOYER: {job['company']}\n"
                 f"LOCATION: {job['location']}\nCLOSING: {job['closing_date']}\n"
                 f"CONTACT: {job['contact_person'] or '(unknown)'}\n\n"
                 f"JOB ADVERTISEMENT:\n{(job['description'] or '')[:6000]}\n\n"
                 f"{context_block}\n\n"
                 f"CANDIDATE BASE RESUME (for reference):\n{base_resume[:6000]}")

    if args.provider == "gemini":
        key = _gemini_key(args, conn)
        if not key:
            print("NO GEMINI KEY. Pass --key, set env GEMINI_API_KEY, or store in profiles.gemini_api_key.")
            return
        model = args.model or "gemini-2.5-pro"
        caller = lambda system, user: _call_gemini(key, model, system, user)
    else:
        row = conn.execute("SELECT claude_api_key FROM profiles WHERE id=1").fetchone()
        key = args.key or os.environ.get("ANTHROPIC_API_KEY") or (row["claude_api_key"] if row else "")
        if not key:
            print("NO CLAUDE KEY."); return
        model = args.model or "claude-sonnet-4-6"
        caller = lambda system, user: _call_claude(key, model, system, user)

    print(f"Generating with {args.provider} ({model}) for: {job['title']} @ {job['company']}")
    print(f"Context: {len(context_block):,} chars, evidence: {[s['filename'] for s in selected]}\n")

    from datetime import datetime
    today = datetime.now().strftime("%d %B %Y")

    print("Authoring resume…")
    resume_md = caller(RESUME_SYSTEM, job_brief).strip()
    print("Authoring cover letter…")
    cover_user = job_brief + f"\n\nTODAY'S DATE: {today}\nCANDIDATE NAME: {MY_INFO['first_name']} {MY_INFO['last_name']}"
    cover_txt = caller(COVER_SYSTEM, cover_user).strip()

    print("Reviewing against your evidence…")
    import json as _json
    review = _review(caller, context_block, job, resume_md, cover_txt)

    safe = clib_safe(job["title"])
    out_dir = Path("applications"); out_dir.mkdir(exist_ok=True)
    rpath = out_dir / f"{safe}_GEN_resume.docx"
    cpath = out_dir / f"{safe}_GEN_cover.docx"
    mdpath = out_dir / f"{safe}_GEN_source.md"
    vpath = out_dir / f"{safe}_GEN_review.json"
    render_markdown_to_docx(resume_md, rpath, personal_info=MY_INFO)
    render_cover_letter_to_docx(cover_txt, cpath, personal_info=MY_INFO)
    mdpath.write_text(f"# RESUME\n\n{resume_md}\n\n---\n\n# COVER LETTER\n\n{cover_txt}", encoding="utf-8")
    vpath.write_text(_json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDONE:\n  {rpath}\n  {cpath}\n  {mdpath} (raw markdown)\n  {vpath} (review)")
    print(f"\n--- REVIEW: {review.get('verdict','?').upper()} — {review.get('summary','')}")
    gi = review.get("grounding_issues") or []
    if gi:
        print(f"  ⚠ {len(gi)} claim(s) to VERIFY before sending:")
        for x in gi:
            print(f"    • \"{x.get('text','')[:80]}\" — {x.get('issue','')[:90]}")
    over = review.get("overstatements") or []
    if over:
        print(f"  ⚠ {len(over)} possible overstatement(s):")
        for x in over:
            print(f"    • \"{x.get('text','')[:80]}\" — {x.get('issue','')[:90]}")
    miss = review.get("missing_must_haves") or []
    if miss:
        print(f"  ◦ {len(miss)} job requirement(s) not addressed:")
        for x in miss[:6]:
            print(f"    • {x[:100]}")
    if not (gi or over):
        print("  ✓ No unsupported claims flagged.")


def clib_safe(value):
    import re
    return re.sub(r"\s+", "_", re.sub(r"[^A-Za-z0-9._ -]+", "", value or "application").strip())[:80] or "application"


if __name__ == "__main__":
    main()
