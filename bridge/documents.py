"""Document extraction, application payload loading, and generation commands.

Split out of python_bridge.py, which re-exports everything here.
"""
import contextlib
import json
import sys
from pathlib import Path

import database_manager as db
from config import MY_INFO
from job_liveness import check_job_liveness
from .runtime import (
    JobNotLiveError,
    _resolve_existing_path,
    applications_dir,
    copy_into_workspace,
    datetime_from_timestamp,
    datetime_timestamp_days_ago,
    emit,
    resolve_workspace_path,
    row_to_dict,
    safe_filename,
)

def _read_docx_text(path):
    import docx
    from docx.oxml.ns import qn
    document = docx.Document(str(path))
    lines = []

    def add_xml_text(element):
        # Raw WordprocessingML includes ordinary paragraphs, table cells and
        # text boxes; python-docx's public paragraph list omits the latter two.
        for paragraph in element.iter(qn("w:p")):
            text = "".join(node.text or "" for node in paragraph.iter(qn("w:t"))).strip()
            if text:
                lines.append(text)

    # Contact details are commonly stored in a Word header or table, so reading
    # only document.paragraphs silently drops exactly the identity data needed
    # by generated resumes.
    for section in document.sections:
        add_xml_text(section.header._element)
    add_xml_text(document.element.body)

    # Linked headers and merged table cells can expose the same text repeatedly.
    unique_lines = []
    seen = set()
    for line in lines:
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            unique_lines.append(line)
    return "\n".join(unique_lines)


def read_resume_text(profile_id):
    profile = db.get_profile_by_id(profile_id)
    if not profile:
        raise ValueError(f"Profile {profile_id} was not found.")

    resume_path = Path(profile["resume_path"])
    if not resume_path.is_absolute():
        resume_path = Path.cwd() / resume_path
    if not resume_path.exists():
        raise FileNotFoundError(f"Resume file not found: {resume_path}")
    return _read_docx_text(resume_path)


def extract_document_text(file_path):
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {file_path}")

    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _read_docx_text(path)
    if suffix == ".doc":
        return _extract_legacy_doc_text(path)
    if suffix == ".pdf":
        import pdfplumber
        parts = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError("Supported document types are .docx, .doc, .pdf, .txt, and .md")


def _extract_legacy_doc_text(path):
    """Read a legacy binary .doc via Microsoft Word automation (pywin32).

    python-docx can't read .doc, so we drive Word over COM. This only works on
    Windows with Word installed; any failure raises a clear, actionable error so
    the caller can tell the user to convert to .docx/PDF rather than failing silently.
    Runs on the worker's per-request thread, so COM is initialised per call.
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ValueError(
            "Reading .doc files needs Microsoft Word (pywin32 is unavailable). "
            "Please save the document as .docx or PDF and re-upload."
        ) from exc

    pythoncom.CoInitialize()
    word = None
    document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0  # wdAlertsNone: never block on a modal dialog
        document = word.Documents.Open(
            str(path.resolve()), ReadOnly=True, ConfirmConversions=False, AddToRecentFiles=False
        )
        return document.Content.Text
    except Exception as exc:
        raise ValueError(
            "Could not read this .doc file with Microsoft Word. "
            "Please save it as .docx or PDF and re-upload."
        ) from exc
    finally:
        try:
            if document is not None:
                document.Close(False)
        finally:
            if word is not None:
                word.Quit()
            pythoncom.CoUninitialize()


def _load_saved_application_payload(job):
    resume_path = _resolve_existing_path(job["resume_used"])
    cover_path = _resolve_existing_path(job["cover_letter_path"])
    position_path = _resolve_existing_path(job["position_description_path"])
    resume_text = job["resume_text"] or ""
    cover_text = job["cover_letter_text"] or ""
    position_text = job["position_description_text"] or ""
    if resume_path and not resume_text:
        resume_text = extract_document_text(resume_path)
    if cover_path and not cover_text:
        cover_text = extract_document_text(cover_path)
    if position_path and not position_text:
        position_text = extract_document_text(position_path)
    return {
        "source": {
            "job_id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "source": job["source"],
            "match_score": job["match_score"],
            "pipeline_stage": job["pipeline_stage"],
            "document_saved_at": job["document_saved_at"],
            "resume_path": str(resume_path) if resume_path else "",
            "cover_letter_path": str(cover_path) if cover_path else "",
            "position_description_path": str(position_path) if position_path else "",
        },
        "fit_analysis": job["ai_analysis"] or "",
        "saved_application_documents": {
            "resume_text": resume_text[:9000],
            "cover_letter_text": cover_text[:6000],
            "position_description_text": position_text[:6000],
        },
    }


def _saved_application_document_sources(profile_id, recent_days=None, limit=30, applied_only=False):
    cutoff = datetime_timestamp_days_ago(recent_days) if recent_days else None
    with db.get_db_connection() as conn:
        applied_clause = "AND jobs.pipeline_stage = 'applied'" if applied_only else ""
        rows = conn.execute(
            f"""
            SELECT jobs.*,
                   COALESCE(updated_at, last_interaction_at, application_date, date_scraped, id) AS document_saved_at
                   ,(
                       SELECT MAX(COALESCE(application_events.event_date, application_events.created_at))
                       FROM application_events
                       WHERE application_events.job_id = jobs.id
                       AND application_events.event_type = 'stage'
                       AND application_events.title = 'Moved to Applied'
                   ) AS applied_at
            FROM jobs
            WHERE jobs.profile_id = ?
            {applied_clause}
            AND (
                NULLIF(resume_used, '') IS NOT NULL
                OR NULLIF(cover_letter_path, '') IS NOT NULL
            )
            ORDER BY document_saved_at DESC, id DESC
            LIMIT ?
            """,
            (profile_id, limit * 3),
        ).fetchall()
    results = []
    for row in rows:
        resume_path = _resolve_existing_path(row["resume_used"])
        cover_path = _resolve_existing_path(row["cover_letter_path"])
        if not resume_path and not cover_path:
            continue
        timestamps = [path.stat().st_mtime for path in (resume_path, cover_path) if path]
        if cutoff and timestamps and max(timestamps) < cutoff:
            continue
        data = row_to_dict(row)
        if timestamps:
            data["document_saved_at"] = datetime_from_timestamp(max(timestamps))
        results.append(data)
        if len(results) >= limit:
            break
    return results


def _posting_to_payload(row):
    return row_to_dict(row) if row else {}


def _load_application_kit_payload(kit):
    data = row_to_dict(kit)
    for key in ("resume_path", "cover_letter_path", "prompt_path", "structured_content_path", "position_description_path"):
        path = _resolve_existing_path(data.get(key))
        if path and key.endswith("_path"):
            try:
                data[f"{key}_text"] = extract_document_text(path)[:8000] if path.suffix.lower() in {".docx", ".doc", ".pdf", ".txt", ".md"} else ""
            except Exception as exc:
                data[f"{key}_text"] = ""
                data[f"{key}_read_error"] = str(exc)
    return data


def command_document_extract(payload):
    input_path = payload["path"]
    doc_type = payload.get("doc_type")
    stored_path = input_path
    if payload.get("job_id"):
        target_dir = applications_dir() / "uploaded_documents" / str(payload["job_id"])
        if doc_type == "resume":
            stored_path = copy_into_workspace(input_path, target_dir, "resume")
        elif doc_type in {"cover_letter", "position_description"}:
            stored_path = copy_into_workspace(input_path, target_dir, doc_type)

    text = extract_document_text(stored_path)
    updates = {}
    if payload.get("job_id") and doc_type == "resume":
        updates = {"resume_used": stored_path, "resume_text": text}
        db.update_job_application(payload["job_id"], updates)
    elif payload.get("job_id") and doc_type == "cover_letter":
        updates = {"cover_letter_path": stored_path, "cover_letter_text": text}
        db.update_job_application(payload["job_id"], updates)
    elif payload.get("job_id") and doc_type == "position_description":
        updates = {"position_description_path": stored_path, "position_description_text": text}
        db.update_job_application(payload["job_id"], updates)
    return {"path": stored_path, "text": text, "updates": updates}


def resolve_additional_candidate_context(payload, job):
    """Return and, when supplied by the UI, persist application-only evidence."""
    db.ensure_application_context_schema()
    if "additional_candidate_context" in payload:
        value = str(payload.get("additional_candidate_context") or "").strip()
    else:
        value = str(
            job["additional_candidate_context"]
            if "additional_candidate_context" in job.keys()
            else ""
        ).strip()
    if len(value) > 12000:
        raise ValueError("Additional candidate evidence must be 12,000 characters or fewer.")
    if "additional_candidate_context" in payload:
        db.update_job_application(job["id"], {"additional_candidate_context": value})
    return value


def command_docs_generate(payload):
    from .jobs import report_job_flags
    # Imported here rather than at module scope: command_docs_generate needs a
    # module that imports this one back.
    import application_doc_builder
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    profile_id = payload.get("profile_id", 1)
    job_id = payload["job_id"]
    resume_text = read_resume_text(profile_id)
    job = db.get_job_details(job_id)
    report_job_flags(job)
    additional_candidate_context = resolve_additional_candidate_context(payload, job)
    settings = db.get_lane_settings(profile_id)
    try:
        lane_context = db.build_lane_context(profile_id, include_terms=True, include_fragments=True)
        settings = {**settings, "lane_context": lane_context}
    except Exception:
        pass
    resume_template = resolve_workspace_path(payload.get("resume_template_path") or settings.get("resume_template_path"))
    cover_template = resolve_workspace_path(payload.get("cover_letter_template_path") or settings.get("cover_letter_template_path"))
    if not resume_template.exists():
        raise FileNotFoundError(f"Resume template not found: {resume_template}")
    if not cover_template.exists():
        raise FileNotFoundError(f"Cover letter template not found: {cover_template}")

    emit("status", message="Generating structured application content...")
    data, provider_label = llm_handler.generate_template_application_content(
        job_id,
        resume_text,
        settings,
        lambda message: emit("log", message=message),
        position_description_text=payload.get("position_description_text") or job["position_description_text"] or "",
        additional_candidate_context=additional_candidate_context,
    )
    output_folder = applications_dir()
    output_folder.mkdir(exist_ok=True)
    safe_title = safe_filename(job["title"])
    resume_path = output_folder / f"{safe_title}_targeted_resume.docx"
    letter_path = output_folder / f"{safe_title}_cover_letter.docx"
    json_path = output_folder / f"{safe_title}_application_content.json"
    emit("status", message="Rendering DOCX templates...")
    application_doc_builder.render_resume_template(resume_template, resume_path, data)
    application_doc_builder.render_cover_letter_template(cover_template, letter_path, data)
    application_doc_builder.write_generation_json(json_path, data)
    db.update_job_application(
        job_id,
        {
            "resume_used": str(resume_path),
            "cover_letter_path": str(letter_path),
        },
    )
    fragment_ids = []
    try:
        fragment_ids = [row["id"] for row in db.get_lane_fragments(profile_id, limit=20)]
    except Exception:
        fragment_ids = []
    try:
        db.create_application_kit(
            job_id,
            profile_id,
            resume_path=resume_path,
            cover_letter_path=letter_path,
            structured_content_path=json_path,
            position_description_path=job["position_description_path"],
            position_description_text=job["position_description_text"],
            additional_candidate_context=additional_candidate_context,
            fragment_ids=fragment_ids,
            notes=f"Application documents generated with {provider_label}.",
        )
    except Exception as exc:
        emit("log", message=f"Application kit document record could not be saved: {exc}")
    db.add_application_event(
        job_id,
        "documents",
        f"Application documents generated with {provider_label}",
        f"Resume: {resume_path}\nCover letter: {letter_path}\nStructured content: {json_path}",
    )
    return {
        "resume_path": str(resume_path),
        "cover_letter_path": str(letter_path),
        "content_json_path": str(json_path),
        "provider": provider_label,
    }


def command_docs_generate_rich(payload):
    """Context-grounded generation: rich evidence + Gemini/Claude + clean render + review."""
    from .jobs import report_job_flags
    # Imported here rather than at module scope: command_docs_generate_rich needs a
    # module that imports this one back.
    import rich_application
    profile_id = payload.get("profile_id", 1)
    job_id = payload["job_id"]
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job {job_id} was not found.")
    report_job_flags(job)
    additional_candidate_context = resolve_additional_candidate_context(payload, job)
    emit("status", message=f"Checking whether {job['title']} is still live…")
    liveness = check_job_liveness(job)
    if liveness["status"] == "closed":
        reason = f"Document generation skipped: {liveness['reason']}"
        db.update_job_application(job_id, {
            "status": "archived",
            "pipeline_stage": "archived",
            "retired_reason": reason,
            "next_action": "",
            "next_action_date": "",
        })
        db.add_application_event(job_id, "retired", "Job listing auto-archived", reason)
        raise JobNotLiveError(reason)
    if liveness["status"] == "live":
        emit("log", message=f"Live listing check passed for {job['title']}: {liveness['reason']}")
    else:
        emit("log", message=f"Listing could not be confirmed for {job['title']}; proceeding cautiously. {liveness['reason']}")
    settings = db.get_lane_settings(profile_id)
    source_resume_text = read_resume_text(profile_id)
    try:
        from config import MY_INFO as info
    except Exception:
        info = None

    track = db.resolve_document_track(job_id)
    emit("log", message=(
        f"Document track: {db.DOC_TRACK_LABELS.get(track['track'], track['track'])} "
        f"({track['source']}) — {' '.join(track['reasons'])}"
    ))

    emit("status", message="Assembling context and generating documents…")
    result = rich_application.generate_rich(
        job_id, profile_id=profile_id, settings=settings, personal_info=info,
        source_resume_text=source_resume_text,
        additional_candidate_context=additional_candidate_context,
        log=lambda m: emit("log", message=m),
        out_dir=applications_dir(),
        document_track=track["track"],
    )

    db.update_job_application(job_id, {
        "resume_used": result["resume_path"],
        "cover_letter_path": result["cover_letter_path"],
        "resume_text": result.get("resume_markdown") or "",
        "cover_letter_text": result.get("cover_letter_text") or "",
    })
    try:
        fragment_ids = [row["id"] for row in db.get_lane_fragments(profile_id, limit=20)]
    except Exception:
        fragment_ids = []
    job = db.get_job_details(job_id)
    review = result.get("review") or {}
    try:
        db.create_application_kit(
            job_id, profile_id,
            resume_path=result["resume_path"],
            resume_text=result.get("resume_markdown") or "",
            cover_letter_path=result["cover_letter_path"],
            cover_letter_text=result.get("cover_letter_text") or "",
            structured_content_path=result["content_json_path"],
            position_description_path=job["position_description_path"] if job else None,
            position_description_text=job["position_description_text"] if job else None,
            additional_candidate_context=additional_candidate_context,
            fragment_ids=fragment_ids,
            notes=(
                f"Rich application generated with {result['provider']} on the "
                f"{db.DOC_TRACK_LABELS.get(track['track'], track['track'])} track. "
                f"Review: {review.get('verdict', 'n/a')}."
            ),
        )
    except Exception as exc:
        emit("log", message=f"Application kit record could not be saved: {exc}")
    db.add_application_event(
        job_id, "documents",
        f"Application documents generated with {result['provider']}",
        f"Resume: {result['resume_path']}\nCover letter: {result['cover_letter_path']}\n"
        f"Document track: {db.DOC_TRACK_LABELS.get(track['track'], track['track'])} ({track['source']}) — "
        f"{' '.join(track['reasons'])}\n"
        f"Review verdict: {review.get('verdict', 'n/a')} — {review.get('summary', '')}",
    )
    return {
        "resume_path": result["resume_path"],
        "cover_letter_path": result["cover_letter_path"],
        "resume_text": result.get("resume_markdown") or "",
        "cover_letter_text": result.get("cover_letter_text") or "",
        "content_json_path": result["content_json_path"],
        "provider": result["provider"],
        "review": review,
        "evidence_used": result.get("evidence_used", []),
        "document_track": track["track"],
        "document_track_reasons": track["reasons"],
    }


def command_application_prompt_generate(payload):
    from .corpus import _fallback_role_alignment, command_memory_status
    from .lanes import _memory_fragment_to_dict
    # Imported here rather than at module scope: command_application_prompt_generate needs a
    # module that imports this one back.
    llm_handler = None

    profile_id = payload.get("profile_id", 1)
    job_id = payload["job_id"]
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job {job_id} was not found.")
    additional_candidate_context = resolve_additional_candidate_context(payload, job)
    settings = db.get_lane_settings(profile_id)
    resume_text = read_resume_text(profile_id)
    full_description = job["description"] or ""
    if job["pdf_text"]:
        full_description += f"\n\n--- ADDITIONAL PDF TEXT ---\n{job['pdf_text']}"
    if job["position_description_text"]:
        full_description = (
            f"--- UPLOADED POSITION DESCRIPTION ---\n{job['position_description_text']}\n\n"
            f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_description}"
        )
    role_payload = {
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "salary": job["salary"],
        "closing_date": job["closing_date"],
        "fit_analysis": job["ai_analysis"] or "",
        "company_intelligence": job["company_intelligence"] or "",
        "description": full_description[:10000],
    }
    memory_fragments = [_memory_fragment_to_dict(row) for row in db.get_lane_fragments(profile_id, limit=180)]
    if not memory_fragments:
        memory_fragments = [_memory_fragment_to_dict(row) for row in db.get_profile_memory_fragments(profile_id, limit=180)]
    alignment = {
        "role_features": [],
        "selected_fragments": [],
        "gaps": [],
        "writing_strategy": "No lane/candidate memory fragments were available. Use the base resume and job description.",
        "provider": "none",
    }
    selected_fragment_details = []
    if memory_fragments:
        with contextlib.redirect_stdout(sys.stderr):
            import llm_handler
        try:
            alignment, provider_label = llm_handler.align_memory_fragments_to_role(
                role_payload,
                memory_fragments,
                settings,
                lambda message: emit("log", message=message),
            )
            alignment["provider"] = provider_label
        except Exception as exc:
            emit("log", message=f"Memory alignment used fallback: {exc}")
            alignment = _fallback_role_alignment(role_payload, memory_fragments)
        selected_ids = {
            item.get("fragment_id")
            for item in alignment.get("selected_fragments", [])
            if item.get("fragment_id")
        }
        selected_fragment_details = [
            fragment for fragment in memory_fragments
            if fragment.get("id") in selected_ids
        ][:14]
        if not selected_fragment_details:
            selected_fragment_details = memory_fragments[:10]
            alignment["selected_fragments"] = [
                {
                    "fragment_id": fragment.get("id"),
                    "theme": fragment.get("theme"),
                    "match_strength": "context",
                    "role_feature": "general profile evidence",
                    "how_to_use": fragment.get("reuse_guidance") or "Use only if it genuinely fits this role.",
                    "caution": "Context fragment only; ignore it if the role does not call for this evidence.",
                }
                for fragment in selected_fragment_details
            ]
            alignment["writing_strategy"] = (
                "No exact memory matches were selected, so broad profile-memory candidates were included. "
                "Use only the fragments that genuinely fit the advertisement."
            )
    memory_pack = {
        "status": command_memory_status({"profile_id": profile_id}),
        "alignment": alignment,
        "selected_fragments": selected_fragment_details,
    }
    prompt = f"""You are an expert Australian resume writer and cover letter writer.

Create a targeted application for this role. Produce:
1. A tailored resume in clean Markdown.
2. A concise, persuasive cover letter.
3. A short list of the strongest positioning points and any risks/gaps to handle.

Rules:
- Use only truthful evidence from the resume and job advertisement.
- Use the lane/candidate memory fragments as evidence guidance, not as copy/paste prose.
- Do not copy previous application wording verbatim. Rewrite freshly for this role.
- Do not invent employers, titles, qualifications, certifications, dates, metrics, responsibilities, or tools.
- Mirror the job advertisement language where accurate.
- Keep the resume ATS-friendly, direct, and achievement-focused.
- Make the cover letter specific to the employer and role, not generic.
- If there are gaps, frame adjacent evidence honestly.

CANDIDATE DETAILS:
Name: {MY_INFO.get('first_name', '')} {MY_INFO.get('last_name', '')}
Email: {MY_INFO.get('email', '')}
Phone: {MY_INFO.get('phone', '')}
LinkedIn: {MY_INFO.get('linkedin', '')}

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}
Application URL: {job['application_url'] or job['url'] or ''}
Salary / rate: {job['salary'] or ''}
Closing date: {job['closing_date'] or ''}

LANE / CANDIDATE MEMORY ALIGNMENT:
The following pack was selected from prior saved application documents and shared candidate fragments for this lane. Use it to identify relevant evidence and positioning; ignore anything that does not genuinely fit this role.
---
{json.dumps(memory_pack, indent=2, ensure_ascii=False)}
---

FIT ANALYSIS:
---
{job['ai_analysis'] or 'No prior analysis is available.'}
---

JOB ADVERTISEMENT:
---
{full_description}
---

BASE RESUME:
---
{resume_text}
---

ADDITIONAL CANDIDATE EVIDENCE (USER-SUPPLIED FOR THIS APPLICATION):
Treat this as first-party evidence. Use only what is stated; do not infer or embellish beyond it. If it expresses a preference or instruction rather than a fact, use it as writing guidance rather than presenting it as evidence.
---
{additional_candidate_context or 'No additional candidate evidence was supplied.'}
---
"""
    output_folder = applications_dir()
    output_folder.mkdir(exist_ok=True)
    prompt_path = output_folder / f"{safe_filename(job['title'])}_external_llm_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    selected_fragment_ids = [
        item.get("fragment_id")
        for item in alignment.get("selected_fragments", [])
        if item.get("fragment_id")
    ]
    try:
        db.create_application_kit(
            job_id,
            profile_id,
            prompt_path=prompt_path,
            position_description_path=job["position_description_path"],
            position_description_text=job["position_description_text"],
            additional_candidate_context=additional_candidate_context,
            fragment_ids=selected_fragment_ids,
            notes="External LLM prompt generated.",
        )
    except Exception as exc:
        emit("log", message=f"Application kit prompt record could not be saved: {exc}")
    db.add_application_event(
        job_id,
        "prompt",
        "External LLM prompt saved",
        f"{prompt_path}\nMemory fragments selected: {len(selected_fragment_details)}",
    )
    return {
        "prompt_path": str(prompt_path),
        "prompt": prompt,
        "memory_alignment": memory_pack,
    }


def command_docs_generate_interested_batch(payload):
    """Generate application documents sequentially for an explicit Interested list."""
    raw_ids = payload.get("job_ids") or []
    job_ids = []
    seen = set()
    for value in raw_ids:
        try:
            job_id = int(value)
        except (TypeError, ValueError):
            continue
        if job_id not in seen:
            seen.add(job_id)
            job_ids.append(job_id)
    if not job_ids:
        raise ValueError("No Interested jobs were supplied for document generation.")

    total = len(job_ids)
    succeeded = 0
    failed = 0
    skipped = 0
    results = []
    emit("progress", current=0, total=total, succeeded=0, failed=0, skipped=0,
         status="starting", message=f"Preparing {total} Interested job(s)…")

    for index, job_id in enumerate(job_ids, start=1):
        job = db.get_job_details(job_id)
        if not job:
            failed += 1
            results.append({"job_id": job_id, "ok": False, "error": "Job not found."})
            emit("progress", current=index, total=total, succeeded=succeeded, failed=failed, skipped=skipped,
                 job_id=job_id, status="failed", message=f"Skipped missing job {job_id}.")
            continue

        title = str(job["title"] or f"Job {job_id}")
        emit("progress", current=index - 1, total=total, succeeded=succeeded, failed=failed, skipped=skipped,
             job_id=job_id, title=title, status="generating",
             message=f"Generating {index} of {total}: {title}")
        try:
            result = command_docs_generate_rich({
                "job_id": job_id,
                "profile_id": job["profile_id"],
                "position_description_text": job["position_description_text"] or "",
            })
            succeeded += 1
            results.append({
                "job_id": job_id,
                "title": title,
                "ok": True,
                "resume_path": result.get("resume_path"),
                "cover_letter_path": result.get("cover_letter_path"),
            })
            emit("progress", current=index, total=total, succeeded=succeeded, failed=failed, skipped=skipped,
                 job_id=job_id, title=title, status="completed",
                 message=f"Completed {index} of {total}: {title}")
        except JobNotLiveError as exc:
            skipped += 1
            reason = str(exc)
            results.append({"job_id": job_id, "title": title, "ok": False, "skipped": True, "error": reason})
            emit("log", message=reason)
            emit("progress", current=index, total=total, succeeded=succeeded, failed=failed, skipped=skipped,
                 job_id=job_id, title=title, status="skipped",
                 message=f"Skipped closed job {index} of {total}: {title}")
        except Exception as exc:
            failed += 1
            error = str(exc)
            if "429" in error or "Too Many Requests" in error or "RESOURCE_EXHAUSTED" in error:
                error = (
                    f"{error} — the document AI provider is rate-limiting requests. "
                    "This is usually a free-tier quota limit: wait a minute and retry a "
                    "smaller batch, or switch the Application-documents provider/model in "
                    "Settings (a 'flash' Gemini model has far higher free limits than a "
                    "'pro-preview' one)."
                )
            results.append({"job_id": job_id, "title": title, "ok": False, "error": error})
            emit("log", message=f"Document generation failed for {title}: {error}")
            emit("progress", current=index, total=total, succeeded=succeeded, failed=failed, skipped=skipped,
                 job_id=job_id, title=title, status="failed",
                 message=f"Failed {index} of {total}: {title}")

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "document:extract": command_document_extract,
    "docs:generate": command_docs_generate,
    "docs:generateRich": command_docs_generate_rich,
    "docs:generateInterestedBatch": command_docs_generate_interested_batch,
    "application:prompt": command_application_prompt_generate,
}
