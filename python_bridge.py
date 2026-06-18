"""JSON command bridge between Electron and the Python business logic.

One-shot UI calls can run in persistent worker mode via newline-delimited JSON
frames. Long-running cancellable tasks are still launched as fresh processes by
Electron so cancellation can terminate the whole task safely.
"""
import contextlib
import json
import os
import re
import shutil
import site
import sys
import threading
import time
from pathlib import Path

APP_ROOT = Path(os.environ.get("JSE_APP_ROOT") or Path(__file__).resolve().parent)
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

sys.path.append(site.getusersitepackages())

import concurrency
import database_manager as db
import scraper_plugins
from config import MY_INFO
from db_setup import setup_database
from job_liveness import check_job_liveness

# Protocol output. In one-shot mode emit() writes JSON lines to stdout exactly as
# before. In --serve (persistent worker) mode, _OUTPUT_STREAM is pinned to the real
# stdout while sys.stdout is redirected to stderr, so stray prints can never corrupt
# the framing, and every line carries the originating request id (thread-local).
_emit_lock = threading.Lock()
_request_ctx = threading.local()
_OUTPUT_STREAM = None


def emit(event_type, **payload):
    message = {"type": event_type, **payload}
    request_id = getattr(_request_ctx, "id", None)
    if request_id is not None:
        message["id"] = request_id
    stream = _OUTPUT_STREAM if _OUTPUT_STREAM is not None else sys.stdout
    line = json.dumps(message, default=str)
    with _emit_lock:
        stream.write(line + "\n")
        stream.flush()


def row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _configured_folder(setting_key, default_name):
    value = db.get_app_setting(setting_key)
    path = Path(value) if value else APP_ROOT / default_name
    if not path.is_absolute():
        path = APP_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def applications_dir():
    return _configured_folder("applications_dir", "applications")


def older_applications_dir():
    return _configured_folder("older_applications_dir", "older_applications")


def rows_to_dicts(rows):
    return [row_to_dict(row) for row in rows]


JOB_SUMMARY_FIELDS = {
    "id",
    "profile_id",
    "profile_name",
    "title",
    "company",
    "location",
    "source",
    "url",
    "pipeline_stage",
    "status",
    "priority",
    "match_score",
    "composite_score",
    "fragment_score",
    "closing_date",
    "closing_date_source",
    "salary",
    "application_date",
    "application_url",
    "contact_person",
    "contact_email",
    "contact_phone",
    "interview_date",
    "interview_type",
    "interview_people",
    "feedback",
    "notes",
    "next_action",
    "next_action_date",
    "retired_reason",
    "last_interaction_at",
    "date_scraped",
    "updated_at",
    "has_company_research",
    "employer_type",
    "actual_company",
    "advertiser_company",
    "company_confidence",
}


def compact_job_dict(row, extra_fields=()):
    data = row_to_dict(row) if row is not None else {}
    allowed = JOB_SUMMARY_FIELDS | set(extra_fields or ())
    return {key: data.get(key) for key in allowed if key in data}


def compact_job_dicts(rows, extra_fields=()):
    return [compact_job_dict(row, extra_fields) for row in rows]


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_json_payload():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def import_app_logic():
    # llm_handler prints configuration during import. Keep the bridge protocol clean.
    with contextlib.redirect_stdout(sys.stderr):
        import app_logic
    return app_logic


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


def safe_filename(value):
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value or "application").strip()
    return re.sub(r"\s+", "_", cleaned)[:90] or "application"


def copy_into_workspace(source_path, target_dir, prefix=""):
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"File not found: {source_path}")

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_source = source.resolve()
    resolved_target_dir = target_dir.resolve()

    try:
        if resolved_source.parent == resolved_target_dir:
            return str(resolved_source)
    except OSError:
        pass

    stem = safe_filename(f"{prefix}_{source.stem}" if prefix else source.stem)
    suffix = source.suffix.lower()
    candidate = resolved_target_dir / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = resolved_target_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    shutil.copy2(str(resolved_source), str(candidate))
    return str(candidate)


def _json_loads_maybe(value, default=None):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _memory_fragment_to_dict(row):
    data = row_to_dict(row)
    data["skills"] = _json_loads_maybe(data.pop("skills_json", None), [])
    data["domains"] = _json_loads_maybe(data.pop("domains_json", None), [])
    data["source_job_ids"] = _json_loads_maybe(data.pop("source_job_ids_json", None), [])
    data["source_doc_paths"] = _json_loads_maybe(data.pop("source_doc_paths_json", None), [])
    return data


def _resolve_existing_path(value):
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.exists() else None


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


def datetime_timestamp_days_ago(days):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days)).timestamp()


def datetime_from_timestamp(timestamp):
    from datetime import datetime
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _fallback_fragments_from_application(payload):
    source = payload.get("source") or {}
    docs = payload.get("saved_application_documents") or {}
    job_id = source.get("job_id")
    source_paths = [
        path for path in [
            source.get("resume_path"),
            source.get("cover_letter_path"),
            source.get("position_description_path"),
        ] if path
    ]
    fragments = []
    combined = "\n".join([
        docs.get("resume_text") or "",
        docs.get("cover_letter_text") or "",
    ])
    common_themes = [
        "vendor management", "stakeholder engagement", "IT strategy", "service delivery",
        "cloud", "cybersecurity", "automation", "team leadership", "systems integration",
        "incident response", "cost optimisation", "digital transformation",
    ]
    for theme in common_themes:
        if theme.lower() in combined.lower():
            match = re.search(r"([^.]{0,180}" + re.escape(theme) + r"[^.]{0,220}\.)", combined, flags=re.IGNORECASE)
            claim = _clean_text(match.group(1)) if match else f"Saved application document contains evidence for {theme}."
            fragments.append({
                "fragment_type": "evidence",
                "theme": theme.title(),
                "claim": claim[:900],
                "supporting_detail": f"Extracted from saved application documents for {source.get('title') or 'prior role'}.",
                "skills": [theme],
                "domains": [source.get("source") or ""],
                "seniority": "manager" if "manager" in str(source.get("title") or "").lower() else "unknown",
                "source_job_ids": [job_id],
                "source_doc_paths": source_paths,
                "reuse_guidance": "Use when the current role asks for this capability; rewrite freshly and preserve the underlying fact.",
                "confidence": "medium",
            })
    cover_text = docs.get("cover_letter_text") or ""
    first_paragraph = next((part.strip() for part in re.split(r"\n\s*\n", cover_text) if len(part.strip()) > 80), "")
    if first_paragraph:
        fragments.append({
            "fragment_type": "cover_angle",
            "theme": "Cover letter positioning",
            "claim": first_paragraph[:900],
            "supporting_detail": f"Opening/positioning pattern from saved cover letter for {source.get('title') or 'prior role'}.",
            "skills": [],
            "domains": [source.get("source") or ""],
            "seniority": "unknown",
            "source_job_ids": [job_id],
            "source_doc_paths": source_paths,
            "reuse_guidance": "Use as positioning guidance only; do not copy wording verbatim.",
            "confidence": "medium",
        })
    return fragments[:24]


def _tokenize_for_match(text):
    stop = {
        "and", "the", "for", "with", "that", "this", "from", "role", "job", "application",
        "candidate", "company", "manager", "senior", "lead", "will", "you", "your", "our",
    }
    return {
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9+/#.-]{2,}", str(text or ""))
        if word.lower() not in stop
    }


def _fallback_role_alignment(role_payload, fragments, max_fragments=12):
    role_text = " ".join(str(value) for value in role_payload.values() if value)
    role_terms = _tokenize_for_match(role_text)
    scored = []
    for fragment in fragments:
        fragment_text = " ".join(
            str(fragment.get(key) or "")
            for key in ("theme", "claim", "supporting_detail", "reuse_guidance")
        )
        fragment_terms = _tokenize_for_match(fragment_text)
        score = len(role_terms & fragment_terms)
        if score:
            scored.append((score, fragment))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [
        {
            "fragment_id": fragment.get("id"),
            "theme": fragment.get("theme"),
            "match_strength": "strong" if score >= 4 else "medium",
            "role_feature": ", ".join(sorted(role_terms & _tokenize_for_match(fragment.get("theme") or fragment.get("claim")))[:4]),
            "how_to_use": fragment.get("reuse_guidance") or "Use as truthful evidence and rewrite freshly.",
            "caution": "",
        }
        for score, fragment in scored[:max_fragments]
    ]
    role_features = sorted(list(role_terms))[:18]
    return {
        "role_features": role_features,
        "selected_fragments": selected,
        "gaps": [],
        "writing_strategy": "Use the selected lane/candidate memory as evidence guidance only. Prioritise fragments with stronger keyword overlap and rewrite all prose freshly for the current role.",
        "provider": "deterministic fallback",
    }


def _search_term_candidate(value):
    text = _clean_text(value)
    if not text:
        return ""
    text = re.split(r"\s[-|]\s", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    text = re.sub(
        r"\b(?:contract|temporary|temp|permanent|full[- ]?time|part[- ]?time|remote|hybrid|melbourne|sydney|brisbane|vic|nsw|qld)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = _clean_text(text.strip(" -|,/"))
    if len(text) < 4 or len(text.split()) > 6:
        return ""
    return text


def _compute_evolved_profile_terms(existing_terms, sources, fragments, max_terms=12):
    candidates = []
    candidates.extend(existing_terms)
    candidates.extend(_search_term_candidate(source.get("title")) for source in sources or [])

    theme_terms = {
        "Automation": "Automation Project Manager",
        "Cloud": "Cloud Infrastructure Manager",
        "Cost Optimisation": "IT Operations Manager",
        "Cybersecurity": "Cyber Security Manager",
        "Digital Transformation": "Digital Transformation Manager",
        "Incident Response": "IT Operations Manager",
        "IT Strategy": "IT Strategy Manager",
        "Service Delivery": "Service Delivery Manager",
        "Systems Integration": "Systems Integration Manager",
        "Team Leadership": "IT Manager",
        "Vendor Management": "IT Vendor Manager",
    }
    for fragment in fragments or []:
        mapped = theme_terms.get(str(fragment.get("theme") or "").strip())
        if mapped:
            candidates.append(mapped)

    evolved = []
    seen = set()
    for candidate in candidates:
        clean = _search_term_candidate(candidate)
        key = clean.casefold()
        if clean and key not in seen:
            evolved.append(clean)
            seen.add(key)
        if len(evolved) >= max_terms:
            break

    return evolved


def _evolve_profile_terms_from_memory(profile_id, sources, fragments, max_terms=12):
    evolved = _compute_evolved_profile_terms(db.get_lane_terms(profile_id), sources, fragments, max_terms)
    # Use the merge-aware writer so manual / interview-validated entries are
    # preserved. save_lane_terms (which the original code called) overwrites
    # the source/confidence of *every* lane term — that destroyed provenance.
    db.merge_lane_terms(profile_id, evolved, source="memory_evolution", confidence=0.78)
    return evolved


def import_resume_file(source_path):
    source = Path(source_path)
    if source.suffix.lower() != ".docx":
        raise ValueError("Profile resumes must be .docx files.")
    return copy_into_workspace(source, Path.cwd() / "Resumes")


def _resume_search_roots():
    return [
        Path.cwd() / "Resumes",
        Path.cwd() / "Application templates" / "CVs",
        Path.cwd(),
    ]


def _resume_option(path, source_label):
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "folder": source_label,
        "modified_at": stat.st_mtime,
        "size": stat.st_size,
    }


def command_resumes_list(payload):
    query = _clean_text(payload.get("query")).casefold()
    current = payload.get("current")
    resumes = {}

    for root in _resume_search_roots():
        if not root.exists():
            continue
        source_label = root.name if root != Path.cwd() else "Workspace"
        for path in root.glob("*.docx"):
            if path.name.startswith("~$"):
                continue
            haystack = f"{path.name} {path.parent} {source_label}".casefold()
            if query and query not in haystack:
                continue
            try:
                resolved = str(path.resolve())
                resumes[resolved] = _resume_option(path, source_label)
            except OSError:
                continue

    if current:
        current_path = Path(current)
        if not current_path.is_absolute():
            current_path = Path.cwd() / current_path
        if current_path.exists() and current_path.suffix.lower() == ".docx":
            try:
                resolved = str(current_path.resolve())
                if not query or query in f"{current_path.name} {current_path.parent}".casefold():
                    resumes[resolved] = _resume_option(current_path, "Current selection")
            except OSError:
                pass

    sorted_resumes = sorted(
        resumes.values(),
        key=lambda item: (-float(item["modified_at"]), item["name"].casefold()),
    )
    return {"resumes": sorted_resumes[:50]}


def command_app_init(_payload):
    with contextlib.redirect_stdout(sys.stderr):
        setup_database()
        db.migrate_profile_credentials_to_app_settings()
        scraper_plugins.ensure_registered()
    app_settings = db.get_app_settings()
    repaired_composites = db.recalculate_composite_scores()
    if repaired_composites:
        emit("log", message=f"Recalculated {repaired_composites} stale composite score(s).")
    db.dedupe_database(lambda message: emit("log", message=message))
    updated_company = db.backfill_missing_company_intelligence()
    if updated_company:
        emit("log", message=f"Company intelligence backfilled for {updated_company} jobs.")
    db.refresh_closing_date_metadata(log_callback=lambda message: emit("log", message=message))
    db.reject_low_match_jobs(50, log_callback=lambda message: emit("log", message=message))
    db.retire_expired_pipeline_jobs(lambda message: emit("log", message=message))
    profiles = [row_to_dict(row) for row in db.get_all_profiles()]
    active_profile_id = profiles[0]["id"] if profiles else 1
    search_sources = scraper_plugins.source_names(include_disabled=False)
    return {
        "profiles": profiles,
        "active_profile_id": active_profile_id,
        "sources": search_sources,
        "search_sources": search_sources,
        "app_settings": app_settings,
    }


def command_app_refresh(payload):
    profile_id = payload.get("profile_id", 1)
    include_all_profiles = bool(payload.get("include_all_profiles"))
    fragment_limit = payload.get("fragment_limit") or 12

    try:
        fragments = command_lanes_fragments_list({"profile_id": profile_id, "limit": fragment_limit})["fragments"]
    except Exception:
        fragments = []

    # The campaign plan/summary is intentionally NOT part of the refresh
    # payload: it regex-scores hundreds of jobs and is only relevant when the
    # Campaign view is open, which loads campaign:plan on demand.
    return {
        "profiles": command_profiles_list(payload)["profiles"],
        "sources": command_sources_list({
            "profile_id": profile_id,
            "include_all_profiles": include_all_profiles,
        })["sources"],
        "search_sources": scraper_plugins.source_names(profile_id=profile_id, include_disabled=False),
        "jobs": command_jobs_list({**payload, "compact": True})["jobs"],
        "dashboard": command_dashboard_get({
            "profile_id": profile_id,
            "include_all_profiles": include_all_profiles,
            "compact": True,
        }),
        "calendar": command_calendar_get({
            "profile_id": profile_id,
            "include_all_profiles": include_all_profiles,
        })["items"],
        "memory": command_memory_status({"profile_id": profile_id}),
        "fragments": fragments,
    }


def command_profiles_list(_payload):
    return {"profiles": [row_to_dict(row) for row in db.get_all_lanes()]}


def command_lanes_list(payload):
    return {"lanes": [row_to_dict(row) for row in db.get_all_lanes(bool(payload.get("include_inactive", True)))]}


def command_profiles_add(payload):
    resume_path = import_resume_file(payload["resume_path"])
    if not db.add_lane(payload["name"], resume_path, payload.get("settings")):
        raise ValueError("Could not add profile. The name may already exist.")
    return command_profiles_list(payload)


def command_lanes_add(payload):
    data = command_profiles_add(payload)
    return {"lanes": data["profiles"], "profiles": data["profiles"]}


def command_profiles_update(payload):
    resume_path = import_resume_file(payload["resume_path"])
    lane_id = payload.get("lane_id") or payload.get("profile_id")
    if not db.update_lane(lane_id, payload["name"], resume_path, payload.get("settings")):
        raise ValueError("Could not update profile. The name may already exist.")
    return command_profiles_list(payload)


def command_lanes_bootstrap(payload):
    """Finish a new lane's optional LLM-assisted setup in the background."""
    profile_id = int(payload.get("profile_id") or 0)
    lane = db.get_lane_by_id(profile_id)
    if not lane:
        raise ValueError("Lane not found.")

    settings = db.get_lane_settings(profile_id)
    resume_text = read_resume_text(profile_id)
    if not resume_text.strip():
        raise ValueError("The selected base resume did not contain readable text.")

    keyword_mode = str(payload.get("keyword_mode") or "manual").strip().lower()
    manual_terms = [
        str(term).strip()
        for term in (payload.get("terms") or [])
        if str(term).strip()
    ]
    if keyword_mode == "manual":
        db.save_lane_terms(profile_id, manual_terms, source="manual", confidence=0.8)
        emit("log", message=f"Saved {len(manual_terms)} manual search terms for {lane['name']}.")

    fragment_count = 0
    fragment_provider = None
    if payload.get("generate_fragments", True):
        with contextlib.redirect_stdout(sys.stderr):
            import corpus_miner

        emit("status", message=f"Mining reusable fragments for {lane['name']}…")
        fragments, fragment_provider = corpus_miner.mine_documents(
            [{"filename": Path(lane["resume_path"]).name or "base-resume.docx", "text": resume_text}],
            settings,
            lambda message: emit("log", message=message),
        )
        person_id = lane["person_id"] if "person_id" in lane.keys() and lane["person_id"] else 1
        db.upsert_candidate_fragments(person_id, fragments, replace=False)
        db.upsert_profile_memory_fragments(profile_id, fragments, replace=False)
        suggestions = db.suggest_lane_fragment_affinity(profile_id, limit=200)
        db.upsert_lane_fragment_affinity(profile_id, suggestions)
        fragment_count = len(fragments)
        emit("log", message=f"Stored {fragment_count} base-resume fragments for {lane['name']}.")

    terms = manual_terms
    if keyword_mode == "generate":
        emit("status", message=f"Generating search terms for {lane['name']} with the local LLM…")
        app_logic = import_app_logic()
        terms = app_logic.execute_keyword_generation(
            payload.get("optimism", 3),
            resume_text,
            lambda message: emit("log", message=message),
            profile_id,
        )

    return {
        "profile_id": profile_id,
        "terms": terms,
        "fragments": fragment_count,
        "fragment_provider": fragment_provider,
    }


def command_lanes_update(payload):
    payload = {**payload, "profile_id": payload.get("lane_id") or payload.get("profile_id")}
    data = command_profiles_update(payload)
    return {"lanes": data["profiles"], "profiles": data["profiles"]}


def command_profiles_delete(payload):
    db.delete_profile(payload.get("lane_id") or payload["profile_id"])
    return command_profiles_list(payload)


def command_lanes_delete(payload):
    data = command_profiles_delete({**payload, "profile_id": payload.get("lane_id") or payload.get("profile_id")})
    return {"lanes": data["profiles"], "profiles": data["profiles"]}


def command_resume_import(payload):
    return {"resume_path": import_resume_file(payload["path"])}


def command_settings_get(payload):
    profile_id = payload.get("lane_id") or payload.get("profile_id", 1)
    return {"settings": db.get_lane_settings(profile_id)}


def command_settings_update(payload):
    profile_id = payload.get("lane_id") or payload.get("profile_id", 1)
    return {"settings": db.update_lane_settings(profile_id, payload.get("settings", {}))}


def command_settings_global_get(_payload):
    return {"settings": db.get_app_settings()}


def command_settings_global_update(payload):
    settings = db.update_app_settings(payload.get("settings", {}))
    for key in ("applications_dir", "older_applications_dir"):
        value = settings.get(key)
        if value:
            Path(value).mkdir(parents=True, exist_ok=True)
    return {"settings": settings}


def command_ai_test_provider(payload):
    """Make a minimal real request with the provider settings currently in the UI."""
    provider = str(payload.get("provider") or "").strip().lower()
    if provider not in {"local", "chatgpt", "claude", "gemini"}:
        raise ValueError(f"Unsupported AI provider: {provider or '(blank)'}")

    supplied = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    settings = {**db.get_app_settings(), **supplied, "doc_ai_provider": provider}
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    started = time.monotonic()
    discovered_model = ""
    if provider == "local":
        local = llm_handler._local_ai_settings(settings)
        try:
            model_data = llm_handler._get_json(
                f"{local['base_url']}/models",
                llm_handler._local_auth_headers(local),
                timeout=15,
            )
            model_rows = model_data.get("data") if isinstance(model_data, dict) else None
            model_ids = [
                str(row.get("id") or "").strip()
                for row in (model_rows or [])
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            ]
            if not model_ids:
                return {
                    "ok": False,
                    "reachable": True,
                    "provider": provider,
                    "label": "Local endpoint",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "message": (
                        "Endpoint reachable, but no model is loaded. Load a model in Unsloth Studio "
                        "(Inference > Load), then test again. The Model field is the API model ID, not a folder path."
                    ),
                }
            discovered_model = model_ids[0]
            if local.get("model") not in model_ids:
                settings["local_model"] = discovered_model
        except Exception:
            # Some OpenAI-compatible servers do not expose /models. In that
            # case, fall through to the chat-completions health check.
            pass
    # Reasoning-capable Gemini/local models may spend the first several hundred
    # tokens internally even for a one-line answer. A 64-token ceiling can yield
    # finishReason=MAX_TOKENS with no response Part, which looks like a broken
    # connection despite successful authentication.
    test_token_budget = 4096 if provider == "gemini" else (1024 if provider == "local" else 256)
    response, label = llm_handler._call_document_ai(
        settings,
        [
            {"role": "system", "content": "You are testing an AI connection. Follow the user's response format exactly."},
            {"role": "user", "content": "Reply with exactly: JSE provider test OK"},
        ],
        temperature=0,
        max_tokens=test_token_budget,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not str(response or "").strip():
        raise RuntimeError(f"{label} returned an empty response.")
    return {
        "ok": True,
        "provider": provider,
        "label": label,
        "elapsed_ms": elapsed_ms,
        "model": discovered_model,
    }


def command_candidate_fragments_list(payload):
    person_id = payload.get("person_id") or 1
    return {
        "fragments": [_memory_fragment_to_dict(row) for row in db.get_candidate_fragments(person_id, payload.get("limit") or 500, payload.get("query"))]
    }


def command_lanes_fragments_list(payload):
    lane_id = payload.get("lane_id") or payload.get("profile_id", 1)
    return {
        "fragments": [_memory_fragment_to_dict(row) for row in db.get_lane_fragments(lane_id, payload.get("limit") or 180)]
    }


def command_lanes_fragments_update(payload):
    lane_id = payload.get("lane_id") or payload.get("profile_id", 1)
    count = db.upsert_lane_fragment_affinity(lane_id, payload.get("affinities") or [])
    return {"updated": count, "fragments": command_lanes_fragments_list({"lane_id": lane_id})["fragments"]}


def command_lanes_fragments_suggest(payload):
    lane_id = payload.get("lane_id") or payload.get("profile_id", 1)
    suggestions = db.suggest_lane_fragment_affinity(lane_id, payload.get("limit") or 80)
    return {"suggestions": suggestions}


def command_lanes_learning_refresh(payload):
    return db.refresh_lane_learning_metrics(payload.get("lane_id") or payload.get("profile_id"))


def _posting_to_payload(row):
    return row_to_dict(row) if row else {}


def command_enrichment_job_extract(payload):
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    posting_id = payload.get("job_posting_id")
    job_id = payload.get("job_id")
    if job_id and not posting_id:
        synced = db.sync_legacy_job_to_lane_model(job_id)
        posting_id = synced["job_posting_id"] if synced else None
    posting = db.get_job_posting(posting_id=posting_id, legacy_job_id=job_id)
    if not posting:
        raise ValueError("Job posting was not found.")
    lane_id = payload.get("lane_id") or payload.get("profile_id") or 1
    settings = db.get_lane_settings(lane_id)
    if payload.get("force_fallback"):
        settings = {**settings, "force_fallback": True}
    intelligence, provider = llm_handler.extract_job_intelligence(
        _posting_to_payload(posting),
        settings,
        lambda message: emit("log", message=message),
    )
    updated = db.save_job_intelligence(posting["id"], intelligence, provider)
    return {"job_posting": row_to_dict(updated), "intelligence": intelligence, "provider": provider}


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


def command_enrichment_application_review(payload):
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    kit_id = payload.get("application_kit_id")
    kits = rows_to_dicts(db.get_application_kits(job_id=payload.get("job_id"), lane_id=payload.get("lane_id") or payload.get("profile_id"), limit=20))
    if kit_id:
        kits = [kit for kit in kits if kit["id"] == kit_id] or rows_to_dicts(db.get_application_kits(limit=500))
        kits = [kit for kit in kits if kit["id"] == kit_id]
    if not kits:
        raise ValueError("Application kit was not found.")
    kit = kits[0]
    settings = db.get_lane_settings(kit["lane_id"])
    if payload.get("force_fallback"):
        settings = {**settings, "force_fallback": True}
    review, provider = llm_handler.review_application_kit(
        _load_application_kit_payload(kit),
        settings,
        lambda message: emit("log", message=message),
    )
    db.save_application_kit_review(kit["id"], review, provider)
    return {"application_kit_id": kit["id"], "review": review, "provider": provider}


def command_enrichment_process(payload):
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    task_type = payload.get("task_type")
    limit = int(payload.get("limit") or 10)
    processed = []
    for task in db.get_pending_local_llm_tasks(task_type, limit):
        db.mark_local_llm_task_running(task["id"])
        try:
            if task["task_type"] == "job_extract":
                posting = db.get_job_posting(posting_id=task["entity_id"])
                if not posting:
                    raise ValueError(f"Posting {task['entity_id']} was not found.")
                settings = db.get_lane_settings(task["lane_id"] or 1)
                if payload.get("force_fallback"):
                    settings = {**settings, "force_fallback": True}
                output, provider = llm_handler.extract_job_intelligence(
                    _posting_to_payload(posting),
                    settings,
                    lambda message: emit("log", message=message),
                )
                db.save_job_intelligence(posting["id"], output, provider)
            elif task["task_type"] == "application_review":
                kits = rows_to_dicts(db.get_application_kits(limit=1000))
                kit = next((item for item in kits if item["id"] == task["entity_id"]), None)
                if not kit:
                    raise ValueError(f"Application kit {task['entity_id']} was not found.")
                settings = db.get_lane_settings(task["lane_id"] or kit["lane_id"] or 1)
                if payload.get("force_fallback"):
                    settings = {**settings, "force_fallback": True}
                output, provider = llm_handler.review_application_kit(
                    _load_application_kit_payload(kit),
                    settings,
                    lambda message: emit("log", message=message),
                )
                db.save_application_kit_review(kit["id"], output, provider)
            else:
                output = {"skipped": True, "reason": f"Unsupported task type {task['task_type']}"}
            db.complete_local_llm_task(task["id"], output=output)
            processed.append({"task_id": task["id"], "task_type": task["task_type"], "status": "complete"})
        except Exception as exc:
            db.complete_local_llm_task(task["id"], error=exc)
            processed.append({"task_id": task["id"], "task_type": task["task_type"], "status": "failed", "error": str(exc)})
    return {"processed": processed, "count": len(processed)}


def command_enrichment_status(payload):
    task_type = payload.get("task_type")
    with db.get_db_connection() as conn:
        params = []
        clause = ""
        if task_type:
            clause = "WHERE task_type = ?"
            params.append(task_type)
        rows = conn.execute(
            f"""
            SELECT task_type, status, COUNT(*) AS count
            FROM local_llm_tasks
            {clause}
            GROUP BY task_type, status
            ORDER BY task_type, status
            """,
            params,
        ).fetchall()
    return {"tasks": rows_to_dicts(rows)}


def command_memory_status(payload):
    profile_id = payload.get("profile_id", 1)
    recent_days = payload.get("recent_days") or 30
    status = db.get_profile_memory_status(profile_id, recent_days)
    last_scan = status.get("last_scan") or {}
    last_triggered_at = last_scan.get("scanned_at")
    applied_sources = _saved_application_document_sources(profile_id, recent_days=None, limit=500, applied_only=True)
    if last_triggered_at:
        applied_sources = [
            source for source in applied_sources
            if str(source.get("applied_at") or source.get("last_interaction_at") or source.get("application_date") or "") > str(last_triggered_at)
        ]
    status["recent_unscanned_count"] = len(applied_sources)
    status["reminder_threshold"] = 6
    return status


def command_memory_scan(payload):
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    profile_id = payload.get("profile_id", 1)
    recent_days = payload.get("recent_days")
    limit = payload.get("limit") or 30
    settings = db.get_lane_settings(profile_id)
    try:
        lane_context = db.build_lane_context(profile_id, include_terms=True, include_fragments=True)
        settings = {**settings, "lane_context": lane_context}
    except Exception:
        pass
    sources = _saved_application_document_sources(profile_id, recent_days=recent_days, limit=limit, applied_only=True)
    all_fragments = []
    # Seed the per-call prior bank with whatever the lane already has on disk
    # so the first kit mined this scan sees prior themes, not an empty bank.
    # Each subsequent kit also sees this scan's accumulating fragments, so
    # reinforcement / dedup signals fire across the entire scan.
    lane_seed_fragments = []
    try:
        lane_seed_fragments = list((settings.get("lane_context") or {}).get("fragments") or [])
    except Exception:
        lane_seed_fragments = []
    newest = None
    used_llm = 0
    used_fallback = 0
    for index, job in enumerate(sources, start=1):
        if concurrency.cancel_event.is_set():
            emit("log", message=f"Memory scan stopped after {index - 1} applications.")
            break
        emit("status", message=f"Scanning application memory {index}/{len(sources)}")
        payload_for_job = _load_saved_application_payload(job)
        # Build the prior bank for THIS call: lane seed + everything mined so
        # far in this scan. Cap it so the prompt stays in budget — most-recent
        # fragments first since they reflect the current shape of the bank.
        prior_lane_fragments = (all_fragments + lane_seed_fragments)[:80]
        kit_outcome = job.get("pipeline_stage") or "applied"
        try:
            fragments, _provider = llm_handler.extract_application_memory_fragments(
                payload_for_job,
                settings,
                lambda message: emit("log", message=message),
                prior_lane_fragments=prior_lane_fragments,
                kit_outcome=kit_outcome,
            )
            used_llm += 1
        except Exception as exc:
            emit("log", message=f"Memory extraction used fallback for {job['title']}: {exc}")
            fragments = _fallback_fragments_from_application(payload_for_job)
            used_fallback += 1
        source = payload_for_job["source"]
        for fragment in fragments:
            fragment.setdefault("source_job_ids", [source["job_id"]])
            source_paths = [
                path for path in [
                    source.get("resume_path"),
                    source.get("cover_letter_path"),
                    source.get("position_description_path"),
                ] if path
            ]
            if source_paths:
                fragment.setdefault("source_doc_paths", source_paths)
        all_fragments.extend(fragments)
        if job["document_saved_at"] and (not newest or str(job["document_saved_at"]) > str(newest)):
            newest = job["document_saved_at"]
    upserted = db.upsert_profile_memory_fragments(profile_id, all_fragments, replace=True)
    lane = db.get_lane_by_id(profile_id)
    person_id = lane["person_id"] if lane and "person_id" in lane.keys() and lane["person_id"] else 1
    candidate_upserted = db.upsert_candidate_fragments(person_id, all_fragments, replace=False)

    # Cross-application convergence: if we have >=2 source kits this scan, ask
    # the LLM to dedupe + outcome-weight the bank. Promotion audit then decides
    # which emerging fragments have earned established status. Best-effort —
    # extraction quality is the load-bearing step, these are refinements.
    consolidation_summary = None
    promotion_summary = None
    if len(sources) >= 2:
        try:
            per_kit = []
            # Group fragments back by source kit so consolidation sees outcomes.
            by_job = {}
            for fragment in all_fragments:
                for job_id in fragment.get("source_job_ids") or []:
                    by_job.setdefault(job_id, []).append(fragment)
            for source_row in sources:
                kit_fragments = by_job.get(source_row["id"], [])
                if not kit_fragments:
                    continue
                per_kit.append({
                    "kit_id": source_row["id"],
                    "role_title": source_row.get("title"),
                    "outcome": source_row.get("pipeline_stage") or "applied",
                    "fragments": kit_fragments,
                })
            if per_kit:
                consolidated, _provider = llm_handler.consolidate_memory_fragments(
                    per_kit, settings, lambda message: emit("log", message=message)
                )
                consolidated_list = consolidated.get("consolidated_fragments") or []
                if consolidated_list:
                    db.upsert_profile_memory_fragments(profile_id, consolidated_list, replace=False)
                    db.upsert_candidate_fragments(person_id, consolidated_list, replace=False)
                    consolidation_summary = f"Consolidated {len(consolidated_list)} fragments (dropped {len(consolidated.get('dropped_fragments') or [])})."
                    emit("log", message=consolidation_summary)
        except Exception as exc:
            emit("log", message=f"Fragment consolidation skipped: {exc}")
        try:
            current_fragments = [dict(row) for row in db.get_lane_fragments(profile_id, limit=400)]
            outcome_history = [
                {"kit_id": source_row["id"], "outcome": source_row.get("pipeline_stage") or "applied", "role_title": source_row.get("title")}
                for source_row in sources
            ]
            promotion, _provider = llm_handler.promote_emerging_fragments(
                current_fragments, outcome_history, settings, lambda message: emit("log", message=message)
            )
            promotion_summary = (
                f"Promotion audit: {len(promotion.get('promotions') or [])} promoted, "
                f"{len(promotion.get('demotions') or [])} demoted, "
                f"{len(promotion.get('confidence_adjustments') or [])} confidence-adjusted."
            )
            emit("log", message=promotion_summary)
        except Exception as exc:
            emit("log", message=f"Promotion audit skipped: {exc}")

    suggestions = db.suggest_lane_fragment_affinity(profile_id, limit=200)
    db.upsert_lane_fragment_affinity(profile_id, suggestions)

    # Existing resume+theme-map term evolution (kept) PLUS fragment-driven term
    # generation merged in alongside. Merge mode protects manual / interview-
    # validated entries from getting clobbered.
    evolved_terms = _evolve_profile_terms_from_memory(profile_id, sources, all_fragments)
    if evolved_terms:
        emit("log", message=f"Lane search terms evolved from saved application documents: {', '.join(evolved_terms)}")
    fragment_terms = []
    try:
        post_consolidation_fragments = [dict(row) for row in db.get_lane_fragments(profile_id, limit=200)]
        if post_consolidation_fragments:
            fragment_terms, _provider = llm_handler.derive_search_terms_from_fragments(
                post_consolidation_fragments,
                optimism_level=3,
                settings=settings,
                log_callback=lambda message: emit("log", message=message),
            )
            if fragment_terms:
                db.merge_lane_terms(profile_id, fragment_terms, source="memory_evolution", confidence=0.78)
                emit("log", message=f"Fragment-driven search terms merged: {', '.join(fragment_terms[:10])}")
    except Exception as exc:
        emit("log", message=f"Fragment-driven term generation skipped: {exc}")

    # Recompute outcome scores from authoritative job stages and stamp the
    # re-mine schedule so memory:remineDue knows when to fire next.
    try:
        db.recompute_fragment_outcome_scores(profile_id)
    except Exception as exc:
        emit("log", message=f"Outcome recompute skipped: {exc}")
    next_due = None
    try:
        next_due = db.mark_memory_remine_complete(profile_id)
    except Exception as exc:
        emit("log", message=f"Re-mine schedule update skipped: {exc}")

    summary_parts = [
        f"Scanned {len(sources)} saved application document set(s)",
        f"upserted {upserted} lane fragments and {candidate_upserted} candidate fragments",
        f"LLM: {used_llm}; fallback: {used_fallback}",
    ]
    if consolidation_summary:
        summary_parts.append(consolidation_summary)
    if promotion_summary:
        summary_parts.append(promotion_summary)
    if next_due:
        summary_parts.append(f"next re-mine due {next_due}")
    summary = ". ".join(summary_parts) + "."
    if sources:
        db.record_profile_memory_scan(profile_id, len(sources), upserted, newest, summary)
    return {
        "applications_scanned": len(sources),
        "fragments_upserted": upserted,
        "candidate_fragments_upserted": candidate_upserted,
        "terms": evolved_terms,
        "fragment_terms": fragment_terms,
        "next_remine_due": next_due,
        "summary": summary,
        "status": command_memory_status({"profile_id": profile_id}),
    }


def command_memory_remine_due(payload):
    """Run memory:scan for every profile whose next_due_at has passed.

    Intended to be invoked by the GUI on launch (or by an external cron) so
    the fragment bank stays current without the user remembering to scan.
    Returns a per-profile result list. Honours the profile-list optional
    `profile_ids` payload key for explicit control.
    """
    explicit = payload.get("profile_ids") if isinstance(payload, dict) else None
    profile_ids = explicit if explicit else db.due_memory_remines()
    results = []
    for profile_id in profile_ids:
        try:
            result = command_memory_scan({
                "profile_id": profile_id,
                "recent_days": payload.get("recent_days") if isinstance(payload, dict) else None,
                "limit": payload.get("limit") if isinstance(payload, dict) else None,
            })
            results.append({"profile_id": profile_id, "result": result})
        except Exception as exc:
            results.append({"profile_id": profile_id, "error": str(exc)})
            emit("log", message=f"Re-mine failed for profile {profile_id}: {exc}")
    return {"profile_ids": profile_ids, "results": results}


def command_database_compact(_payload):
    return db.compact_database()


def command_terms_get(payload):
    return {"terms": db.get_lane_terms(payload.get("lane_id") or payload.get("profile_id", 1))}


def command_terms_save(payload):
    terms = [term.strip() for term in payload.get("terms", []) if str(term).strip()]
    db.save_lane_terms(payload.get("lane_id") or payload.get("profile_id", 1), terms, source="manual", confidence=0.8)
    return {"terms": terms}


def command_sources_list(payload):
    scraper_plugins.ensure_registered()
    stored_sources = db.get_all_sources(None if payload.get("include_all_profiles") else payload.get("profile_id"))
    plugin_sources = scraper_plugins.source_names(profile_id=payload.get("profile_id"), include_disabled=False)
    return {"sources": list(dict.fromkeys(plugin_sources + stored_sources))}


def command_scrapers_list(payload):
    scraper_plugins.ensure_registered()
    profile_id = payload.get("profile_id")
    return {"scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=profile_id)}


def command_scrapers_import(payload):
    path = payload.get("path")
    if not path:
        raise ValueError("Missing plugin path.")
    plugin = scraper_plugins.install_from_path(path)
    return {"plugin": plugin, "scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))}


def command_scrapers_remove(payload):
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    scraper_plugins.remove_plugin(plugin_id)
    return {"ok": True, "scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))}


def command_scrapers_update(payload):
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    updates = {}
    if "enabled" in payload:
        updates["enabled"] = 1 if payload.get("enabled") else 0
    if "config" in payload:
        updates["config_json"] = json.dumps(payload.get("config") or {}, separators=(",", ":"), sort_keys=True)
    plugin = db.update_scraper_plugin(plugin_id, updates)
    return {"plugin": plugin, "scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))}


def command_scrapers_lane_update(payload):
    profile_id = payload.get("profile_id") or payload.get("lane_id") or 1
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    db.update_lane_scraper_settings(
        profile_id,
        plugin_id,
        enabled=payload.get("enabled") if "enabled" in payload else None,
        config=payload.get("config") if "config" in payload else None,
    )
    return {"scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=profile_id)}


def command_scrapers_build(payload):
    import scraper_plugin_builder

    answers = payload.get("answers") or payload
    result = scraper_plugin_builder.build_and_install(
        answers,
        log_callback=lambda message: emit("log", message=message),
    )
    profile_id = payload.get("profile_id")
    result["scrapers"] = scraper_plugins.all_plugins(include_disabled=True, profile_id=profile_id)
    return result


def command_scrapers_test(payload):
    import scraper_plugin_builder

    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    return scraper_plugin_builder.test_plugin(
        plugin_id,
        profile_id=payload.get("profile_id") or 1,
        keyword=payload.get("keyword"),
        max_pages=payload.get("max_pages") or 1,
    )


def command_jobs_list(payload):
    rows = db.get_pipeline_jobs(payload)
    return {"jobs": compact_job_dicts(rows) if payload.get("compact") else rows_to_dicts(rows)}


def command_jobs_counts(payload):
    new_count, approved_count = db.get_job_counts(payload.get("profile_id"))
    return {"new": new_count, "approved": approved_count}


def command_jobs_update_status(payload):
    job = db.update_job_application(payload["job_id"], {"pipeline_stage": payload["status"]})
    return {"job": row_to_dict(job)}


def command_jobs_delete(payload):
    db.delete_job(payload["job_id"])
    return {"ok": True}


def command_jobs_add_manual(payload):
    """Track a job that never passed through the scrapers — recruiter calls,
    referrals, careers-page finds. Reuses the full add_job pipeline (dedupe,
    metadata extraction, company classification, lane sync); a missing URL gets
    a synthetic unique one since the column is NOT NULL UNIQUE."""
    import uuid

    profile_id = payload.get("profile_id", 1)
    title = _clean_text(payload.get("title"))
    if not title:
        raise ValueError("A job title is required.")
    url = str(payload.get("url") or "").strip() or f"manual://{uuid.uuid4().hex}"
    job_data = {
        "title": title,
        "company": _clean_text(payload.get("company")),
        "location": _clean_text(payload.get("location")) or "Melbourne VIC",
        "url": url,
        "description": str(payload.get("description") or "").strip() or f"Manually added role: {title}.",
        "pdf_text": "",
    }
    if payload.get("salary"):
        job_data["salary"] = _clean_text(payload.get("salary"))

    messages = []
    added = db.add_job(job_data, "Manual", profile_id, lambda message: messages.append(message))

    normalized = db.normalize_job_url(url)
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (normalized,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id FROM jobs WHERE profile_id = ? AND title = ? ORDER BY id DESC LIMIT 1",
                (profile_id, title),
            ).fetchone()
    job_id = row["id"] if row else None

    # Apply closing date / starting stage AFTER insert so an already-passed
    # closing date can't make add_job refuse a job the user explicitly wants
    # tracked (e.g. logging an application made elsewhere).
    if job_id:
        updates = {}
        if payload.get("closing_date"):
            updates["closing_date"] = str(payload["closing_date"])[:10]
            updates["closing_date_source"] = "provided"
        stage = str(payload.get("stage") or "").strip().lower()
        if added and stage and stage != "new":
            updates["pipeline_stage"] = stage
            if stage == "applied" and not payload.get("application_date"):
                from datetime import date
                updates["application_date"] = date.today().isoformat()
        if updates:
            db.update_job_application(job_id, updates)

    return {
        "added": bool(added),
        "job_id": job_id,
        "message": "; ".join(messages) if messages else ("Job added." if added else "Job matched an existing record."),
    }


def command_jobs_update(payload):
    job = db.update_job_application(payload["job_id"], payload.get("updates", {}))
    return {
        "job": row_to_dict(job),
        "events": rows_to_dicts(db.get_job_events(payload["job_id"])),
        "interviews": rows_to_dicts(db.get_interviews(payload["job_id"])),
    }


def command_jobs_cleanup_archive(payload):
    rows = db.archive_stale_applications(
        payload.get("job_ids") or [],
        payload.get("reason") or "No response after 30 days",
    )
    return {
        "archived": rows_to_dicts(rows),
        "count": len(rows),
    }


def command_jobs_move_profile(payload):
    job = db.move_job_to_profile(int(payload["job_id"]), int(payload["profile_id"]))
    return {
        "job": row_to_dict(job),
        "events": rows_to_dicts(db.get_job_events(payload["job_id"])),
        "interviews": rows_to_dicts(db.get_interviews(payload["job_id"])),
    }


def command_company_classify(payload):
    job = db.refresh_job_company_intelligence(payload["job_id"])
    return {"job": row_to_dict(job)}


def command_company_research(payload):
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    job = db.get_job_details(payload["job_id"])
    if not job:
        raise ValueError(f"Job {payload['job_id']} was not found.")
    settings = db.get_lane_settings(job["profile_id"])
    data, provider_label = llm_handler.research_company_for_job(
        payload["job_id"],
        settings,
        lambda message: emit("log", message=message),
    )
    updated = db.update_job_company_research(
        payload["job_id"],
        {"ai_research": data, **data},
        data.get("employer_type"),
        data.get("actual_company"),
        data.get("confidence"),
    )
    return {
        "job": row_to_dict(updated),
        "provider": provider_label,
        "events": rows_to_dicts(db.get_job_events(payload["job_id"])),
    }


def _job_has_researched_company_intel(job):
    if not job:
        return False
    try:
        intelligence = json.loads(job["company_intelligence"] or "{}")
    except (TypeError, json.JSONDecodeError):
        intelligence = {}
    return bool(intelligence.get("ai_research") or intelligence.get("cached_company_profile"))


def command_company_research_batch(payload):
    job_ids = [int(job_id) for job_id in payload.get("job_ids", []) if job_id]
    researched = 0
    skipped = 0
    failed = 0
    providers = set()
    for index, job_id in enumerate(job_ids, start=1):
        if concurrency.cancel_event.is_set():
            emit("log", message=f"Company research stopped after {researched} of {len(job_ids)} jobs.")
            break
        job = db.get_job_details(job_id)
        if not job:
            failed += 1
            emit("log", message=f"Company research skipped missing job {job_id}.")
            continue
        if _job_has_researched_company_intel(job):
            skipped += 1
            emit("log", message=f"Skipped already researched employer intel: {job['title']}")
            continue
        emit("status", message=f"Researching employer intel {index}/{len(job_ids)}")
        try:
            with contextlib.redirect_stdout(sys.stderr):
                import llm_handler
            settings = db.get_lane_settings(job["profile_id"])
            data, provider_label = llm_handler.research_company_for_job(
                job_id,
                settings,
                lambda message: emit("log", message=message),
            )
            db.update_job_company_research(
                job_id,
                {"ai_research": data, **data},
                data.get("employer_type"),
                data.get("actual_company"),
                data.get("confidence"),
            )
            providers.add(provider_label)
            researched += 1
        except Exception as exc:
            failed += 1
            emit("log", message=f"Company research failed for {job['title']}: {exc}")
    return {
        "researched": researched,
        "skipped": skipped,
        "failed": failed,
        "providers": sorted(providers),
    }


def command_jobs_detail(payload):
    job = db.get_job_details(payload["job_id"])
    if job and db.company_intelligence_needs_refresh(job):
        job = db.refresh_job_company_intelligence(payload["job_id"])
    return {
        "job": row_to_dict(job),
        "events": rows_to_dicts(db.get_job_events(payload["job_id"])),
        "interviews": rows_to_dicts(db.get_interviews(payload["job_id"])),
        "application_kits": rows_to_dicts(db.get_application_kits(job_id=payload["job_id"])),
    }


def command_interviews_add(payload):
    interview_id = db.add_interview(payload["job_id"], payload.get("interview", {}))
    return {
        "interview_id": interview_id,
        "job": row_to_dict(db.get_job_details(payload["job_id"])),
        "events": rows_to_dicts(db.get_job_events(payload["job_id"])),
        "interviews": rows_to_dicts(db.get_interviews(payload["job_id"])),
    }


def command_interviews_update(payload):
    updated = db.update_interview(payload["interview_id"], payload.get("interview", {}))
    if not updated:
        raise ValueError("No interview fields were supplied.")
    job_id = updated["job_id"]
    return {
        "interview": row_to_dict(updated),
        "job": row_to_dict(db.get_job_details(job_id)),
        "events": rows_to_dicts(db.get_job_events(job_id)),
        "interviews": rows_to_dicts(db.get_interviews(job_id)),
    }


def command_events_add(payload):
    db.add_application_event(
        payload["job_id"],
        payload.get("event_type", "note"),
        payload.get("title", "Application note"),
        payload.get("details"),
        payload.get("event_date"),
        payload.get("due_date"),
    )
    return {"events": rows_to_dicts(db.get_job_events(payload["job_id"]))}


# The dashboard is refreshed constantly (every filter change debounces into an
# app:refresh), but the retire-expired sweep scans and writes the jobs table.
# In the persistent worker, run it at most once per interval per scope.
_HOUSEKEEPING_INTERVAL_SECONDS = 600
_housekeeping_last_run = {}
_housekeeping_lock = threading.Lock()


def _housekeeping_due(scope_key):
    now = time.monotonic()
    with _housekeeping_lock:
        last = _housekeeping_last_run.get(scope_key, 0)
        if last and now - last < _HOUSEKEEPING_INTERVAL_SECONDS:
            return False
        _housekeeping_last_run[scope_key] = now
        return True


def command_dashboard_get(payload):
    housekeeping_profile_id = None if payload.get("include_all_profiles") else payload.get("profile_id")
    if _housekeeping_due(housekeeping_profile_id or "all"):
        db.retire_expired_pipeline_jobs(lambda message: emit("log", message=message), housekeeping_profile_id)
    data = db.get_dashboard(payload.get("profile_id"), bool(payload.get("include_all_profiles")))
    if payload.get("compact"):
        due_actions = compact_job_dicts(data["due_actions"])
        top_matches = compact_job_dicts(data["top_matches"])
        awaiting_feedback = compact_job_dicts(data["awaiting_feedback"])
        cleanup_due = compact_job_dicts(data["cleanup_due"], {"days_since_application"})
    else:
        due_actions = rows_to_dicts(data["due_actions"])
        top_matches = rows_to_dicts(data["top_matches"])
        awaiting_feedback = rows_to_dicts(data["awaiting_feedback"])
        cleanup_due = rows_to_dicts(data["cleanup_due"])
    return {
        "stage_counts": data["stage_counts"],
        "due_actions": due_actions,
        "top_matches": top_matches,
        "awaiting_feedback": awaiting_feedback,
        "cleanup_due": cleanup_due,
        "last_scrape": row_to_dict(data["last_scrape"]),
    }


def command_calendar_get(payload):
    return {
        "items": rows_to_dicts(
            db.get_calendar_items(
                payload.get("profile_id"),
                bool(payload.get("include_all_profiles")),
            )
        )
    }


def command_campaign_summary(payload):
    return db.get_campaign_summary(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
        payload.get("limit") or 12,
        payload.get("min_score") or 65,
    )


def command_campaign_plan(payload):
    return db.get_campaign_plan(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
        payload.get("limit") or 10,
    )


def command_campaign_stage_attack_queue(payload):
    return db.stage_campaign_attack_queue(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
        payload.get("limit") or 12,
        payload.get("min_score") or 65,
        payload.get("due_date"),
    )


def command_campaign_refresh_actions(payload):
    return db.refresh_campaign_actions(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
    )


def command_campaign_weekly_report(payload):
    return db.get_campaign_weekly_report(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
        payload.get("days") or 7,
    )


def command_campaign_hidden_market(payload):
    return db.get_hidden_market_intel(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
        payload.get("days") or 60,
    )


def command_stats_summary(payload):
    days = payload.get("days") or 7
    stats = db.get_activity_stats(
        payload.get("profile_id"),
        bool(payload.get("include_all_profiles")),
        days,
    )
    # Fold in the conversion calibration (band funnel + data-driven
    # recommendations) so the Stats tab carries the full retrospective —
    # this replaced the old on-demand Weekly Signal button on Campaign.
    try:
        report = db.get_campaign_weekly_report(
            payload.get("profile_id"),
            bool(payload.get("include_all_profiles")),
            days,
        )
        stats["band_funnel"] = report.get("band_funnel") or []
        stats["recommendations"] = report.get("recommendations") or []
    except Exception as exc:
        emit("log", message=f"Conversion calibration unavailable: {exc}")
    return stats


def command_terms_generate(payload):
    app_logic = import_app_logic()
    profile_id = payload.get("profile_id", 1)
    resume_text = read_resume_text(profile_id)
    terms = app_logic.execute_keyword_generation(
        payload.get("optimism", 3),
        resume_text,
        lambda message: emit("log", message=message),
        profile_id,
    )
    return {"terms": terms}


def command_scrape_run(payload):
    app_logic = import_app_logic()
    sources = payload.get("sources") or scraper_plugins.source_names(profile_id=payload.get("profile_id"), include_disabled=False)
    if not sources:
        raise ValueError("No scraper plugins are available. Import a plugin or create one in Settings > Searchers.")
    include_all = bool(payload.get("include_all_profiles"))
    profiles = db.get_all_profiles() if include_all else [db.get_profile_by_id(payload.get("profile_id", 1))]
    run_id = db.record_scraper_run(payload.get("profile_id"), "all_profiles" if include_all else "profile", sources, "running")
    try:
        for profile in profiles:
            if not profile:
                continue
            profile_id = profile["id"]
            emit("status", message=f"Scraping profile: {profile['name']}")
            terms = db.get_profile_terms(profile_id)
            resume_text = read_resume_text(profile_id)
            if not terms:
                emit("log", message=f"No saved terms for {profile['name']}. Generating terms first.")
                terms = app_logic.execute_keyword_generation(
                    payload.get("optimism", 3),
                    resume_text,
                    lambda message: emit("log", message=message),
                    profile_id,
                )
            app_logic.execute_scraping_and_analysis(
                terms,
                sources,
                resume_text,
                lambda message, _progress=False: emit("status", message=message),
                lambda message: emit("log", message=f"[{profile['name']}] {message}"),
                lambda updated_terms: emit("log", message=f"[{profile['name']}] Search terms now: {', '.join(updated_terms)}"),
                None,
                profile_id,
                db.get_lane_settings(profile_id),
            )
        db.dedupe_database(lambda message: emit("log", message=message))
        db.record_scraper_run(status="complete", summary="Scrape completed.", run_id=run_id)
    except Exception as exc:
        db.record_scraper_run(status="failed", summary=str(exc), run_id=run_id)
        raise
    return {"ok": True}


def command_analysis_run(payload):
    app_logic = import_app_logic()
    include_all = bool(payload.get("include_all_profiles"))
    profiles = db.get_all_profiles() if include_all else [db.get_profile_by_id(payload.get("profile_id", 1))]
    stage = payload.get("stage") or payload.get("status") or "new"
    for profile in profiles:
        if not profile:
            continue
        emit("status", message=f"Analyzing profile: {profile['name']}")
        resume_text = read_resume_text(profile["id"])
        app_logic.run_analysis_on_existing(
            resume_text,
            False,
            stage,
            lambda message: emit("log", message=f"[{profile['name']}] {message}"),
            profile["id"],
        )
    return {"ok": True}


def command_analysis_job(payload):
    app_logic = import_app_logic()
    job = db.get_job_details(payload["job_id"])
    if not job:
        raise ValueError(f"Job {payload['job_id']} was not found.")
    resume_text = read_resume_text(job["profile_id"])
    app_logic.run_analysis_on_specific_jobs(
        [payload["job_id"]],
        resume_text,
        lambda message: emit("log", message=message),
        job["profile_id"],
    )
    return {"job": row_to_dict(db.get_job_details(payload["job_id"]))}


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


def resolve_workspace_path(value):
    path = Path(value or "")
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


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
    import application_doc_builder
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    profile_id = payload.get("profile_id", 1)
    job_id = payload["job_id"]
    resume_text = read_resume_text(profile_id)
    job = db.get_job_details(job_id)
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
    import rich_application
    profile_id = payload.get("profile_id", 1)
    job_id = payload["job_id"]
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job {job_id} was not found.")
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

    emit("status", message="Assembling context and generating documents…")
    result = rich_application.generate_rich(
        job_id, profile_id=profile_id, settings=settings, personal_info=info,
        source_resume_text=source_resume_text,
        additional_candidate_context=additional_candidate_context,
        log=lambda m: emit("log", message=m),
        out_dir=applications_dir(),
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
            notes=f"Rich application generated with {result['provider']}. Review: {review.get('verdict', 'n/a')}.",
        )
    except Exception as exc:
        emit("log", message=f"Application kit record could not be saved: {exc}")
    db.add_application_event(
        job_id, "documents",
        f"Application documents generated with {result['provider']}",
        f"Resume: {result['resume_path']}\nCover letter: {result['cover_letter_path']}\n"
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
    }


def command_application_prompt_generate(payload):
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


def _corpus_conn():
    import context_library as clib
    import sqlite3 as _sql
    conn = _sql.connect(str(clib.DB_PATH)); conn.row_factory = _sql.Row
    clib.ensure_schema(conn)
    return conn, clib


def _person_id_for(profile_id):
    lane = db.get_lane_by_id(profile_id)
    return lane["person_id"] if lane and "person_id" in lane.keys() and lane["person_id"] else 1


def command_corpus_stats(payload):
    source = str(older_applications_dir())
    conn, clib = _corpus_conn()
    rows = conn.execute("SELECT doc_type, COUNT(*) c, SUM(char_len) s FROM context_documents "
                        "WHERE filename NOT LIKE '~$%' GROUP BY doc_type ORDER BY c DESC").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM context_documents WHERE filename NOT LIKE '~$%'").fetchone()[0]
    conn.close()
    person_id = _person_id_for(payload.get("profile_id", 1))
    with db.get_db_connection() as dconn:
        frag = dconn.execute("SELECT COUNT(*) FROM candidate_fragments WHERE person_id=?", (person_id,)).fetchone()[0]
    return {"total": total, "fragments": frag, "source": source,
            "by_type": [{"doc_type": r["doc_type"], "count": r["c"], "chars": r["s"] or 0} for r in rows]}


def command_corpus_reindex(payload):
    conn, clib = _corpus_conn(); conn.close()
    source = payload.get("source") or str(older_applications_dir())
    emit("status", message=f"Indexing corpus from {source}…")
    stats = clib.ingest(source, log=lambda m: emit("log", message=m))
    return {"ingest": stats, **command_corpus_stats(payload)}


def command_corpus_reclassify(payload):
    conn, clib = _corpus_conn()
    removed = conn.execute("DELETE FROM context_documents WHERE filename LIKE '~$%'").rowcount
    rows = conn.execute("SELECT id, filename, text FROM context_documents").fetchall()
    changed = 0
    for r in rows:
        conn.execute("UPDATE context_documents SET doc_type=?, role_family=? WHERE id=?",
                     (clib.classify(r["filename"], r["text"]), clib.detect_role_family(r["filename"], r["text"]), r["id"]))
        changed += 1
    conn.commit(); conn.close()
    result = command_corpus_stats(payload)
    result.update({"reclassified": changed, "removed_temp": removed})
    return result


class JobNotLiveError(ValueError):
    """A confident liveness check says document generation should be skipped."""


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


def command_corpus_mine(payload):
    import corpus_miner
    profile_id = payload.get("profile_id", 1)
    settings = db.get_lane_settings(profile_id)
    emit("status", message="Mining fragments from your evidence corpus…")
    fragments, label = corpus_miner.mine_corpus(settings, lambda m: emit("log", message=m))
    person_id = _person_id_for(profile_id)
    cand = db.upsert_candidate_fragments(person_id, fragments, replace=False)
    prof = db.upsert_profile_memory_fragments(profile_id, fragments, replace=False)
    return {"mined": len(fragments), "candidate_upserted": cand, "profile_upserted": prof, "provider": label}


def command_corpus_clear_docs(payload):
    conn, clib = _corpus_conn()
    n = conn.execute("DELETE FROM context_documents").rowcount
    conn.commit(); conn.close()
    return {"cleared_documents": n}


def command_corpus_clear_fragments(payload):
    profile_id = payload.get("profile_id", 1)
    person_id = _person_id_for(profile_id)
    with db.get_db_connection() as conn:
        c1 = conn.execute("DELETE FROM candidate_fragments WHERE person_id=?", (person_id,)).rowcount
        try:
            c2 = conn.execute("DELETE FROM profile_memory_fragments WHERE profile_id=?", (profile_id,)).rowcount
        except Exception:
            c2 = 0
        conn.commit()
    return {"cleared_candidate_fragments": c1, "cleared_profile_fragments": c2}


def command_corpus_list(payload):
    conn, clib = _corpus_conn()
    q = (payload.get("query") or "").strip()
    limit = int(payload.get("limit") or 300)
    if q:
        rows = conn.execute("SELECT id, filename, doc_type, role_family, char_len FROM context_documents "
                            "WHERE filename LIKE ? ORDER BY doc_type, filename LIMIT ?", (f"%{q}%", limit)).fetchall()
    else:
        rows = conn.execute("SELECT id, filename, doc_type, role_family, char_len FROM context_documents "
                            "ORDER BY doc_type, filename LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"documents": [dict(r) for r in rows]}


def command_corpus_remove_doc(payload):
    conn, clib = _corpus_conn()
    n = conn.execute("DELETE FROM context_documents WHERE id=?", (payload["id"],)).rowcount
    conn.commit(); conn.close()
    return {"removed": n}


def command_corpus_set_type(payload):
    conn, clib = _corpus_conn()
    conn.execute("UPDATE context_documents SET doc_type=? WHERE id=?", (payload["doc_type"], payload["id"]))
    conn.commit(); conn.close()
    return {"updated": 1}


COMMANDS = {
    "app:init": command_app_init,
    "app:refresh": command_app_refresh,
    "lanes:list": command_lanes_list,
    "lanes:add": command_lanes_add,
    "lanes:update": command_lanes_update,
    "lanes:delete": command_lanes_delete,
    "candidate:fragments:list": command_candidate_fragments_list,
    "lanes:fragments:list": command_lanes_fragments_list,
    "lanes:fragments:update": command_lanes_fragments_update,
    "lanes:fragments:suggest": command_lanes_fragments_suggest,
    "lanes:learning:refresh": command_lanes_learning_refresh,
    "enrichment:jobExtract": command_enrichment_job_extract,
    "enrichment:applicationReview": command_enrichment_application_review,
    "enrichment:process": command_enrichment_process,
    "enrichment:status": command_enrichment_status,
    "profiles:list": command_profiles_list,
    "profiles:add": command_profiles_add,
    "profiles:update": command_profiles_update,
    "profiles:delete": command_profiles_delete,
    "lanes:bootstrap": command_lanes_bootstrap,
    "resume:import": command_resume_import,
    "resumes:list": command_resumes_list,
    "settings:get": command_settings_get,
    "settings:update": command_settings_update,
    "settings:globalGet": command_settings_global_get,
    "settings:globalUpdate": command_settings_global_update,
    "ai:testProvider": command_ai_test_provider,
    "memory:status": command_memory_status,
    "memory:scan": command_memory_scan,
    "memory:remineDue": command_memory_remine_due,
    "database:compact": command_database_compact,
    "terms:get": command_terms_get,
    "terms:save": command_terms_save,
    "terms:generate": command_terms_generate,
    "sources:list": command_sources_list,
    "scrapers:list": command_scrapers_list,
    "scrapers:import": command_scrapers_import,
    "scrapers:remove": command_scrapers_remove,
    "scrapers:update": command_scrapers_update,
    "scrapers:laneUpdate": command_scrapers_lane_update,
    "scrapers:build": command_scrapers_build,
    "scrapers:test": command_scrapers_test,
    "jobs:list": command_jobs_list,
    "jobs:counts": command_jobs_counts,
    "jobs:addManual": command_jobs_add_manual,
    "jobs:updateStatus": command_jobs_update_status,
    "jobs:update": command_jobs_update,
    "jobs:cleanupArchive": command_jobs_cleanup_archive,
    "jobs:moveProfile": command_jobs_move_profile,
    "company:classify": command_company_classify,
    "company:research": command_company_research,
    "company:researchBatch": command_company_research_batch,
    "jobs:detail": command_jobs_detail,
    "jobs:delete": command_jobs_delete,
    "interviews:add": command_interviews_add,
    "interviews:update": command_interviews_update,
    "events:add": command_events_add,
    "dashboard:get": command_dashboard_get,
    "calendar:get": command_calendar_get,
    "campaign:summary": command_campaign_summary,
    "campaign:plan": command_campaign_plan,
    "campaign:stageAttackQueue": command_campaign_stage_attack_queue,
    "campaign:refreshActions": command_campaign_refresh_actions,
    "campaign:weeklyReport": command_campaign_weekly_report,
    "campaign:hiddenMarket": command_campaign_hidden_market,
    "stats:summary": command_stats_summary,
    "scrape:run": command_scrape_run,
    "analysis:run": command_analysis_run,
    "analysis:job": command_analysis_job,
    "document:extract": command_document_extract,
    "docs:generate": command_docs_generate,
    "docs:generateRich": command_docs_generate_rich,
    "docs:generateInterestedBatch": command_docs_generate_interested_batch,
    "application:prompt": command_application_prompt_generate,
    "corpus:stats": command_corpus_stats,
    "corpus:reindex": command_corpus_reindex,
    "corpus:reclassify": command_corpus_reclassify,
    "corpus:mine": command_corpus_mine,
    "corpus:clearDocs": command_corpus_clear_docs,
    "corpus:clearFragments": command_corpus_clear_fragments,
    "corpus:list": command_corpus_list,
    "corpus:removeDoc": command_corpus_remove_doc,
    "corpus:setType": command_corpus_set_type,
}


def main():
    if len(sys.argv) < 2:
        raise ValueError("Missing bridge command.")

    command = sys.argv[1]
    handler = COMMANDS.get(command)
    if handler is None:
        raise ValueError(f"Unknown bridge command: {command}")

    payload = load_json_payload()
    concurrency.cancel_event.clear()
    concurrency.paused.set()
    result = handler(payload)
    emit("result", data=result)


def _handle_serve_request(request_id, command, payload):
    _request_ctx.id = request_id
    try:
        handler = COMMANDS.get(command)
        if handler is None:
            emit("error", message=f"Unknown bridge command: {command}")
            return
        result = handler(payload or {})
        emit("result", data=result)
    except Exception as exc:
        emit("error", message=str(exc))
    finally:
        _request_ctx.id = None


def serve():
    """Persistent worker: handle newline-framed {id, command, payload} requests, one
    thread per request, so imports and the SQLite warmup are paid once for the whole
    session instead of per call. Used for the one-shot bridge:invoke path; long-running
    cancellable tasks still spawn their own process."""
    global _OUTPUT_STREAM

    # Pin protocol output to the real stdout, then send everything else to stderr so no
    # stray print can corrupt the JSON framing the Electron main process parses.
    _OUTPUT_STREAM = sys.stdout
    sys.stdout = sys.stderr

    concurrency.cancel_event.clear()
    concurrency.paused.set()

    with contextlib.redirect_stdout(sys.stderr):
        try:
            setup_database()
        except Exception as exc:
            emit("log", message=f"Worker warmup failed: {exc}")

    while True:
        raw = sys.stdin.readline()
        if not raw:
            break  # stdin closed -> Electron is shutting the worker down
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        thread = threading.Thread(
            target=_handle_serve_request,
            args=(request.get("id"), request.get("command"), request.get("payload") or {}),
            daemon=True,
        )
        thread.start()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        serve()
    else:
        try:
            main()
        except Exception as exc:
            emit("error", message=str(exc))
            sys.exit(1)
