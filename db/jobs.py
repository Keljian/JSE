"""Job records: capture, dedupe, analysis fields, pipeline stages, kits, interviews.

Split out of database_manager.py, which re-exports everything here.
"""
import sqlite3
import re
import hashlib
import json
from datetime import datetime, timedelta
from .connection import (
    _execute_with_retry,
    ensure_application_context_schema,
    get_db_connection,
)
from .constants import (
    ACTIVE_PRE_APPLICATION_STAGES,
    APPLIED_EMPLOYER_DECLINE_DAYS,
    AUTO_REJECT_THRESHOLD,
    BROAD_RELEVANT_TITLES,
    BROAD_UNRELATED_TITLES,
    KEYWORD_FILTERED_SOURCES,
    PIPELINE_STAGES,
    WORK_MODE_OPTIONS,
)
from .text import (
    _clean,
    _closing_date_is_expired,
    _company_key,
    _default_closing_date,
    _extract_explicit_closing_date,
    _is_meaningful_job_identity,
    _job_identity_key,
    _role_tokens,
    _split_csv,
    description_fingerprint,
    location_aliases,
    make_analysis_signature,
    normalize_job_url,
    normalize_source,
    source_aliases,
)
from .companies import (
    apply_company_profile_cache,
    classify_company_intelligence,
)
from .settings import (
    get_lane_settings,
)
from .lanes import (
    _canonical_person_name,
    _contact_email_name,
    _contact_names_overlap,
    _profile_filter_clause,
    _schedule_interview_fragment_mining,
    _upsert_lane_opportunity_from_row,
    get_profile_by_id,
    record_fragment_outcomes,
)
from .outcomes import (
    OUTCOME_GHOSTED,
    OUTCOME_INTERVIEW,
    _sync_outcome_for_stage,
    calculate_composite_score,
    composite_score_with_prior,
    set_application_outcome,
)

def _find_existing_equivalent_job(conn, profile_id, title, company):
    if not _is_meaningful_job_identity(title, company):
        return None
    title_key, company_key = _job_identity_key(title, company)
    rows = conn.execute(
        """
        SELECT id, title, company, profile_id, pipeline_stage, status
        FROM jobs
        WHERE pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
        ORDER BY CASE WHEN profile_id = ? THEN 0 ELSE 1 END, id
        """,
        (profile_id,),
    ).fetchall()
    for row in rows:
        if _job_identity_key(row["title"], row["company"]) == (title_key, company_key):
            return row
    return None


def _stage_dedupe_rank(row):
    stage = normalize_stage(row["pipeline_stage"] or row["status"] or "new")
    ranks = {
        "offer": 70,
        "interviewing": 60,
        "applied": 50,
        "interested": 30,
        "new": 20,
        "rejected_by_company": 10,
        "rejected": 10,
        "archived": 0,
    }
    return ranks.get(stage, 20)


def _source_has_keyword_search(source, job_data=None):
    if (job_data or {}).get("search_keyword"):
        return True
    normalized = normalize_source(source).lower()
    return normalized in KEYWORD_FILTERED_SOURCES


def _job_is_broadly_plausible(job_data):
    """Permissive pre-filter for broad feeds that are not searched by keyword."""
    title = str(job_data.get("title") or "")
    title_tokens = _role_tokens(title)

    relevant = title_tokens & BROAD_RELEVANT_TITLES
    if relevant:
        return True, f"title has broad professional signal ({', '.join(sorted(relevant))})"
    unrelated = title_tokens & BROAD_UNRELATED_TITLES
    if unrelated:
        return False, f"title appears unrelated ({', '.join(sorted(unrelated))})"
    return True, "no obvious unrelated title signal"


def _should_store_scraped_job(job_data, source, profile_id, log_callback=None):
    if _source_has_keyword_search(source, job_data):
        return True
    matched, reason = _job_is_broadly_plausible(job_data)
    if not matched and log_callback:
        log_callback(f"Skipped broad-feed job '{job_data.get('title') or 'Untitled'}' from {source}: {reason}.")
    return matched


def extract_contact_records(text, provided=None):
    """Extract evidence-local contact tuples without pairing unrelated globals."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    records = []
    name_pattern = re.compile(
        r"(?i:(?:please\s+)?contact|enquiries?(?:\s+to)?|for further information(?:\s+contact)?)"
        r"[^\n]{0,40}?\b([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3})\b"
    )
    email_pattern = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
    phone_pattern = re.compile(r"(?:\+?61\s?\d[\d\s]{7,12}|0\d[\d\s]{8,12})")

    for index, line in enumerate(lines):
        for email_match in email_pattern.finditer(line):
            email = email_match.group(0).lower()
            nearby = "\n".join(lines[max(0, index - 2): min(len(lines), index + 3)])
            explicit = ""
            for candidate_line in lines[max(0, index - 2): min(len(lines), index + 2)]:
                match = name_pattern.search(candidate_line)
                if match:
                    explicit = _canonical_person_name(match.group(1))
                    if explicit:
                        break
            inferred = _contact_email_name(email)
            if explicit and inferred and not _contact_names_overlap(explicit, inferred):
                name, raw_label, quality = inferred, explicit, "email-derived"
            else:
                name, raw_label, quality = explicit or inferred, "", "explicit" if explicit else "email-derived"
            phone_match = phone_pattern.search(nearby)
            records.append({
                "name": name, "email": email,
                "phone": _clean(phone_match.group(0)) if phone_match else "",
                "quality": quality, "raw_label": raw_label,
                "evidence_text": _clean(nearby)[:400],
            })

    # Contact blocks without an email are still useful when a nearby phone is present.
    for index, line in enumerate(lines):
        match = name_pattern.search(line)
        if not match:
            continue
        name = _canonical_person_name(match.group(1))
        nearby = "\n".join(lines[max(0, index - 1): min(len(lines), index + 2)])
        phone_match = phone_pattern.search(nearby)
        phone = _clean(phone_match.group(0)) if phone_match else ""
        phone_digits = re.sub(r"\D", "", phone)
        phone_already_bound = any(phone_digits and phone_digits == re.sub(r"\D", "", item.get("phone") or "") for item in records)
        if name and phone_match and not phone_already_bound and not any(_contact_names_overlap(name, item.get("name")) for item in records):
            records.append({"name": name, "email": "", "phone": phone, "quality": "explicit", "raw_label": "", "evidence_text": _clean(nearby)[:400]})

    for item in provided or []:
        email = _clean(item.get("email") or item.get("contact_email")).lower()
        explicit = _canonical_person_name(item.get("name") or item.get("contact_person"))
        inferred = _contact_email_name(email)
        name = explicit if explicit and (not inferred or _contact_names_overlap(explicit, inferred)) else inferred or explicit
        if name or email or item.get("phone") or item.get("contact_phone"):
            records.append({"name": name, "email": email, "phone": _clean(item.get("phone") or item.get("contact_phone")), "quality": "provided", "raw_label": "" if name == explicit else explicit, "evidence_text": "Scraper-provided contact"})

    merged = {}
    for item in records:
        key = item.get("email") or _company_key(item.get("name")) or re.sub(r"\D", "", item.get("phone") or "")
        if not key:
            continue
        current = merged.setdefault(key, item)
        current["name"] = current.get("name") or item.get("name")
        current["email"] = current.get("email") or item.get("email")
        current["phone"] = current.get("phone") or item.get("phone")
        if item.get("quality") in {"provided", "explicit"}:
            current["quality"] = item["quality"]
    return list(merged.values())


def extract_job_metadata(job_data):
    """Best-effort extraction of closing date, contact details, and salary from ad text."""
    raw_text = "\n".join([
        str(job_data.get("title") or ""),
        str(job_data.get("company") or ""),
        str(job_data.get("description") or ""),
        str(job_data.get("pdf_text") or ""),
    ])
    text = _clean(raw_text)
    metadata = {}
    for key in ("contact_person", "contact_email", "contact_phone", "salary", "closing_date"):
        if job_data.get(key):
            metadata[key] = _clean(str(job_data.get(key)))
            if key == "closing_date":
                metadata["closing_date_source"] = "provided"

    explicit_closing = _extract_explicit_closing_date(text)
    if explicit_closing:
        metadata["closing_date"] = explicit_closing
        metadata["closing_date_source"] = "advertisement"

    salary_match = re.search(
        r"(\$ ?\d[\d,]*(?:\s*[-–]\s*\$? ?\d[\d,]*)?(?:\s*(?:pa|p\.a\.|per annum|plus super|\+ super|super|day|hour|hr))?)",
        text,
        flags=re.IGNORECASE,
    )
    if salary_match:
        metadata.setdefault("salary", _clean(salary_match.group(1)))

    provided_contacts = list(job_data.get("contact_records") or [])
    if any(job_data.get(key) for key in ("contact_person", "contact_email", "contact_phone")):
        provided_contacts.append({"name": job_data.get("contact_person"), "email": job_data.get("contact_email"), "phone": job_data.get("contact_phone")})
    contact_records = extract_contact_records(raw_text, provided_contacts)
    if contact_records:
        primary = max(contact_records, key=lambda item: ({"provided": 3, "explicit": 2, "email-derived": 1}.get(item.get("quality"), 0), bool(item.get("email")), bool(item.get("phone"))))
        metadata["contact_records"] = contact_records
        metadata["contact_records_json"] = json.dumps(contact_records, ensure_ascii=False)
        metadata["contact_person"] = primary.get("name") or metadata.get("contact_person")
        metadata["contact_email"] = primary.get("email") or metadata.get("contact_email")
        metadata["contact_phone"] = primary.get("phone") or metadata.get("contact_phone")

    if "closing_date" not in metadata:
        metadata["closing_date"] = _default_closing_date()
        metadata["closing_date_source"] = "default"
    metadata.setdefault("closing_date_source", "provided" if job_data.get("closing_date") else "default")
    return metadata


def _update_existing_scraped_job(conn, job_id, job_data, metadata, fingerprint=None):
    company = apply_company_profile_cache(classify_company_intelligence({**job_data, **metadata}), conn)
    updates = {
        "description": job_data.get("description"),
        "pdf_text": job_data.get("pdf_text"),
        "closing_date": metadata.get("closing_date"),
        "contact_person": metadata.get("contact_person"),
        "contact_email": metadata.get("contact_email"),
        "contact_phone": metadata.get("contact_phone"),
        "contact_records_json": metadata.get("contact_records_json"),
        "salary": metadata.get("salary"),
        "closing_date_source": metadata.get("closing_date_source"),
        "description_fingerprint": fingerprint,
        **company,
    }
    if _closing_date_is_expired(metadata):
        updates.update({
            "status": "rejected",
            "pipeline_stage": "rejected",
            "retired_reason": f"Applications closed on {metadata.get('closing_date')}.",
            "next_action": None,
            "next_action_date": None,
        })
    assignments = []
    params = []
    for column, value in updates.items():
        if value is not None:
            assignments.append(f"{column} = ?")
            params.append(value)
    scraped_position_text = job_data.get("position_description_text") or job_data.get("pdf_text")
    if scraped_position_text:
        # A scraper refresh may discover the linked position-description PDF
        # after the job was first saved. Attach it to the application workspace,
        # but never replace a document the user has already uploaded.
        assignments.append(
            "position_description_text = CASE "
            "WHEN NULLIF(position_description_text, '') IS NULL THEN ? "
            "ELSE position_description_text END"
        )
        params.append(scraped_position_text)
    if not assignments:
        assignments = []
    assignments.append("last_seen_at = datetime('now')")
    assignments.append("missing_sweeps = 0")
    assignments.append("updated_at = datetime('now')")
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)


# --- Job flags --------------------------------------------------------------
# Flags are observations recorded against a job: the specific, checkable
# concerns raised at triage. They deliberately do not gate anything — no code
# path branches on them — so they are stored beside the score rather than
# folded into it.

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


def normalize_flag_type(value):
    """Coerce a flag type to one of JOB_FLAG_TYPES.

    Unrecognised types become "evidence_gap" rather than being dropped: the
    requirement text is the valuable part, and losing an observation because
    its label was misspelled would be the wrong trade.
    """
    flag_type = str(value or "").strip().lower().replace("-", "_")
    return flag_type if flag_type in JOB_FLAG_TYPES else "evidence_gap"


def _flag_summary_types(flags):
    """Denormalised type list, so SQL can filter without parsing JSON."""
    seen = []
    for flag in flags:
        if flag["type"] not in seen:
            seen.append(flag["type"])
    return ",".join(seen)


def update_job_flags(job_id, payload, replace_manual=False):
    """Store the flags raised for a job.

    Manual flags survive re-analysis by default. Someone who added "recruiter
    would not name the client" does not want it erased the next time triage
    runs, and re-deriving it is not possible — only the person knows.
    """
    payload = payload or {}
    incoming = []
    for flag in payload.get("flags") or []:
        requirement = str(flag.get("requirement") or "").strip()
        if not requirement:
            continue
        flag_type = normalize_flag_type(flag.get("type"))
        confidence = str(flag.get("confidence") or "").strip().lower()
        incoming.append({
            "type": flag_type,
            "label": JOB_FLAG_LABELS[flag_type],
            "requirement": requirement,
            "detail": str(flag.get("detail") or "").strip(),
            "confidence": confidence if confidence in {"high", "medium", "low"} else "low",
            "source": "manual" if str(flag.get("source") or "") == "manual" else "auto",
        })

    if not replace_manual:
        existing = (get_job_flags(job_id) or {}).get("flags") or []
        manual = [flag for flag in existing if flag.get("source") == "manual"]
        incoming = [flag for flag in incoming if flag.get("source") != "manual"] + manual

    record = {
        "flags": incoming,
        "domain_match": str(payload.get("domain_match") or "").strip(),
        "seniority_match": str(payload.get("seniority_match") or "").strip(),
        "seniority_direction": str(payload.get("seniority_direction") or "unknown").strip().lower(),
        "summary": str(payload.get("summary") or "").strip(),
    }
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET job_flags_json = ?,
                job_flags_types = ?,
                job_flags_checked_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(record, ensure_ascii=False),
                _flag_summary_types(incoming) or None,
                now,
                now,
                job_id,
            ),
        )
        conn.commit()
    return record


def get_job_flags(job_id):
    """Return the stored flags for a job as a plain dict."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT job_flags_json, job_flags_checked_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if not row:
        return None
    record = {}
    if row["job_flags_json"]:
        try:
            record = json.loads(row["job_flags_json"]) or {}
        except (TypeError, ValueError):
            record = {}
    record.setdefault("flags", [])
    record.setdefault("summary", "")
    record.setdefault("seniority_direction", "unknown")
    record["checked_at"] = row["job_flags_checked_at"] or ""
    return record


def add_job_flag(job_id, flag_type, requirement, detail="", confidence="high"):
    """Add a flag by hand. Marked manual so re-analysis will not erase it."""
    record = get_job_flags(job_id) or {"flags": []}
    record["flags"] = list(record.get("flags") or []) + [{
        "type": normalize_flag_type(flag_type),
        "requirement": str(requirement or "").strip(),
        "detail": str(detail or "").strip(),
        "confidence": confidence,
        "source": "manual",
    }]
    return update_job_flags(job_id, record, replace_manual=True)


def dismiss_job_flag(job_id, requirement):
    """Remove one flag by its requirement text."""
    record = get_job_flags(job_id) or {"flags": []}
    target = str(requirement or "").strip().lower()
    record["flags"] = [
        flag for flag in record.get("flags") or []
        if str(flag.get("requirement") or "").strip().lower() != target
    ]
    return update_job_flags(job_id, record, replace_manual=True)


def clear_job_flags(job_id):
    """Drop every flag on a job, including manual ones."""
    return update_job_flags(job_id, {"flags": []}, replace_manual=True)


def update_job_fragment_alignment(job_id, fragment_score, composite_score, alignment_json):
    """Persist the fragment-bank alignment score and composite UI score on a job.

    The composite is re-derived here with the bounded conversion prior (item 6)
    from the job's own dimensions, so the outcome feedback loop reaches the score
    the UI sorts by. The caller-supplied composite is used only as a fallback
    when the job row can't be read.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        job = conn.execute(
            "SELECT match_score, title, company, advertiser_company, employer_type, source FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is not None and job["match_score"] is not None:
            composite_score = composite_score_with_prior(job["match_score"], fragment_score, dict(job))
        conn.execute(
            """
            UPDATE jobs
            SET fragment_score = ?,
                composite_score = ?,
                fragment_alignment_json = ?,
                fragment_alignment_updated_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (fragment_score, composite_score, alignment_json, now, now, job_id),
        )
        conn.commit()


def _upsert_job_posting_from_row(conn, row):
    normalized_url = normalize_job_url(row["url"])
    # If the legacy_job_id (row["id"]) already exists in another job_postings row,
    # set it to NULL to avoid UNIQUE constraint violations. The URL-based dedup
    # still works via ON CONFLICT(url), and the shared job_posting_id is the
    # canonical link between lanes and opportunities.
    existing_legacy = conn.execute(
        "SELECT id FROM job_postings WHERE legacy_job_id = ? AND legacy_job_id IS NOT NULL",
        (row["id"],),
    ).fetchone()
    legacy_job_id = None if existing_legacy else row["id"]
    conn.execute(
        """
        INSERT INTO job_postings (
            legacy_job_id, title, company, location, url, description, source, pdf_text,
            date_scraped, closing_date, closing_date_source, contact_person, contact_email,
            contact_phone, salary, description_fingerprint, advertiser_company, actual_company,
            employer_type, company_confidence, company_intelligence, company_research_updated_at,
            job_intelligence_json, job_intelligence_updated_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), COALESCE(?, datetime('now')))
        ON CONFLICT(url) DO UPDATE SET
            legacy_job_id = COALESCE(job_postings.legacy_job_id, excluded.legacy_job_id),
            title = excluded.title,
            company = excluded.company,
            location = excluded.location,
            description = excluded.description,
            source = excluded.source,
            pdf_text = excluded.pdf_text,
            closing_date = excluded.closing_date,
            closing_date_source = excluded.closing_date_source,
            contact_person = excluded.contact_person,
            contact_email = excluded.contact_email,
            contact_phone = excluded.contact_phone,
            salary = excluded.salary,
            description_fingerprint = excluded.description_fingerprint,
            advertiser_company = excluded.advertiser_company,
            actual_company = excluded.actual_company,
            employer_type = excluded.employer_type,
            company_confidence = excluded.company_confidence,
            company_intelligence = excluded.company_intelligence,
            company_research_updated_at = excluded.company_research_updated_at,
            updated_at = datetime('now')
        """,
        (
            legacy_job_id, row["title"], row["company"], row["location"], normalized_url,
            row["description"], row["source"], row["pdf_text"], row["date_scraped"],
            row["closing_date"], row["closing_date_source"], row["contact_person"],
            row["contact_email"], row["contact_phone"], row["salary"],
            row["description_fingerprint"], row["advertiser_company"], row["actual_company"],
            row["employer_type"], row["company_confidence"], row["company_intelligence"],
            row["company_research_updated_at"],
            row["job_intelligence_json"] if "job_intelligence_json" in row.keys() else None,
            row["job_intelligence_updated_at"] if "job_intelligence_updated_at" in row.keys() else None,
            row["date_scraped"], row["updated_at"],
        ),
    )
    return conn.execute("SELECT id FROM job_postings WHERE url = ?", (normalized_url,)).fetchone()["id"]


def sync_legacy_job_to_lane_model(job_id, lane_id=None, source=None, keyword=None, route_result=None):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        posting_id = _upsert_job_posting_from_row(conn, row)
        opportunity_id = _upsert_lane_opportunity_from_row(conn, row, posting_id, lane_id)
        if source or keyword:
            conn.execute(
                """
                INSERT OR IGNORE INTO search_hits (
                    lane_id, job_posting_id, source, keyword, route_score, route_reason
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lane_id or row["profile_id"] or 1,
                    posting_id,
                    source or row["source"],
                    keyword,
                    (route_result or {}).get("route_score"),
                    (route_result or {}).get("route_reason"),
                ),
            )
        task_hash = hashlib.sha256(
            "\n".join([
                str(row["title"] or ""),
                str(row["company"] or ""),
                str(row["description"] or ""),
                str(row["pdf_text"] or ""),
            ]).encode("utf-8", errors="replace")
        ).hexdigest()
        conn.execute(
            """
            INSERT INTO local_llm_tasks (task_type, entity_type, entity_id, lane_id, status, input_hash)
            SELECT 'job_extract', 'job_posting', ?, ?, 'pending', ?
            WHERE NOT EXISTS (
                SELECT 1 FROM local_llm_tasks
                WHERE task_type = 'job_extract'
                  AND entity_type = 'job_posting'
                  AND entity_id = ?
                  AND input_hash = ?
                  AND status IN ('pending', 'running', 'complete')
            )
            """,
            (posting_id, lane_id or row["profile_id"] or 1, task_hash, posting_id, task_hash),
        )
        conn.commit()
        return {"job_posting_id": posting_id, "lane_opportunity_id": opportunity_id}


def route_job_to_lane(job_data, lane_id):
    settings = get_lane_settings(lane_id)
    intelligence = {}
    try:
        if job_data.get("job_intelligence_json"):
            intelligence = json.loads(job_data.get("job_intelligence_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        intelligence = {}
    lane_text = " ".join(str(settings.get(key) or "") for key in (
        "lane_intent", "target_titles", "target_domains", "seniority",
        "must_have_terms", "boost_terms"
    )).lower()
    avoid_text = " ".join(str(settings.get(key) or "") for key in ("avoid_terms", "penalty_terms")).lower()
    intelligence_text = " ".join(
        json.dumps(intelligence.get(key) or "", ensure_ascii=False)
        for key in ("role_family", "seniority", "core_skills", "domains", "responsibilities", "hard_requirements", "soft_requirements", "dealbreakers")
    )
    job_text = " ".join([
        str(job_data.get(key) or "")
        for key in ("title", "company", "location", "description", "pdf_text")
    ] + [intelligence_text]).lower()
    lane_tokens = set(re.findall(r"[a-z0-9]{3,}", lane_text))
    avoid_tokens = set(re.findall(r"[a-z0-9]{3,}", avoid_text))
    job_tokens = set(re.findall(r"[a-z0-9]{3,}", job_text))
    matched = lane_tokens & job_tokens
    negatives = avoid_tokens & job_tokens
    score = min(1.0, len(matched) / max(8, len(lane_tokens) or 8))
    role_family = str(intelligence.get("role_family") or "").lower()
    if role_family and role_family in lane_text:
        score = min(1.0, score + 0.18)
    if str(intelligence.get("seniority") or "").lower() and str(intelligence.get("seniority") or "").lower() in lane_text:
        score = min(1.0, score + 0.08)
    score = max(0.0, score - len(negatives) * 0.08)
    return {
        "should_create_opportunity": score >= 0.12 or not lane_tokens,
        "route_score": round(score, 3),
        "matched_signals": sorted(matched)[:12],
        "negative_signals": sorted(negatives)[:12],
        "route_reason": (
            f"Matched {', '.join(sorted(matched)[:8]) or 'default active lane'}"
            + (f"; avoided {', '.join(sorted(negatives)[:6])}" if negatives else "")
        ),
    }


def create_application_kit(job_id, lane_id=None, resume_path=None, resume_text=None, cover_letter_path=None,
                           cover_letter_text=None, prompt_path=None, structured_content_path=None,
                           position_description_path=None, position_description_text=None,
                           additional_candidate_context=None, fragment_ids=None, notes=None,
                           applied_at=None, outcome=None):
    ensure_application_context_schema()
    synced = sync_legacy_job_to_lane_model(job_id, lane_id)
    if not synced:
        raise ValueError(f"Job {job_id} was not found.")
    lane_id = lane_id or get_job_details(job_id)["profile_id"] or 1
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO application_kits (
                legacy_job_id, lane_opportunity_id, lane_id, job_posting_id,
                resume_path, resume_text, cover_letter_path, cover_letter_text,
                prompt_path, structured_content_path, position_description_path,
                position_description_text, additional_candidate_context, applied_at, outcome, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, synced["lane_opportunity_id"], lane_id, synced["job_posting_id"],
                str(resume_path) if resume_path else None, resume_text,
                str(cover_letter_path) if cover_letter_path else None, cover_letter_text,
                str(prompt_path) if prompt_path else None,
                str(structured_content_path) if structured_content_path else None,
                str(position_description_path) if position_description_path else None,
                position_description_text, additional_candidate_context, applied_at, outcome, notes,
            ),
        )
        kit_id = cursor.lastrowid
        for fragment_id in fragment_ids or []:
            conn.execute(
                """
                INSERT OR IGNORE INTO application_kit_fragments (application_kit_id, fragment_id, usage_type, weight)
                VALUES (?, ?, 'selected', 1.0)
                """,
                (kit_id, fragment_id),
            )
        conn.commit()
    queue_application_review_task(kit_id, lane_id)
    return kit_id


def get_application_kits(job_id=None, lane_id=None, limit=50):
    clauses = ["1 = 1"]
    params = []
    if job_id:
        clauses.append("application_kits.legacy_job_id = ?")
        params.append(job_id)
    if lane_id:
        clauses.append("application_kits.lane_id = ?")
        params.append(lane_id)
    params.append(limit)
    with get_db_connection() as conn:
        return conn.execute(
            f"""
            SELECT application_kits.*, profiles.name AS lane_name, job_postings.title AS job_title
            FROM application_kits
            LEFT JOIN profiles ON profiles.id = application_kits.lane_id
            LEFT JOIN job_postings ON job_postings.id = application_kits.job_posting_id
            WHERE {' AND '.join(clauses)}
            ORDER BY generated_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def get_job_posting(posting_id=None, legacy_job_id=None):
    clauses = []
    params = []
    if posting_id:
        clauses.append("id = ?")
        params.append(posting_id)
    if legacy_job_id:
        clauses.append("legacy_job_id = ?")
        params.append(legacy_job_id)
    if not clauses:
        return None
    with get_db_connection() as conn:
        return conn.execute(
            f"SELECT * FROM job_postings WHERE {' OR '.join(clauses)} LIMIT 1",
            params,
        ).fetchone()


def save_job_intelligence(posting_id, intelligence, provider="local"):
    payload = json.dumps({"provider": provider, **(intelligence or {})}, ensure_ascii=False)
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE job_postings
            SET job_intelligence_json = ?,
                job_intelligence_updated_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (payload, posting_id),
        )
        conn.commit()
    return get_job_posting(posting_id=posting_id)


def get_pending_local_llm_tasks(task_type=None, limit=10):
    clauses = ["status = 'pending'"]
    params = []
    if task_type:
        clauses.append("task_type = ?")
        params.append(task_type)
    params.append(limit)
    with get_db_connection() as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM local_llm_tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()


def mark_local_llm_task_running(task_id):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE local_llm_tasks SET status = 'running', started_at = datetime('now'), error = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()


def complete_local_llm_task(task_id, output=None, error=None):
    status = "failed" if error else "complete"
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE local_llm_tasks
            SET status = ?,
                output_json = ?,
                error = ?,
                completed_at = datetime('now')
            WHERE id = ?
            """,
            (
                status,
                json.dumps(output or {}, ensure_ascii=False) if output is not None else None,
                str(error or "") or None,
                task_id,
            ),
        )
        conn.commit()


def queue_application_review_task(application_kit_id, lane_id=None):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM application_kits WHERE id = ?", (application_kit_id,)).fetchone()
        if not row:
            return False
        payload_hash = hashlib.sha256(
            "\n".join([
                str(row["resume_text"] or row["resume_path"] or ""),
                str(row["cover_letter_text"] or row["cover_letter_path"] or ""),
                str(row["prompt_path"] or ""),
                str(row["structured_content_path"] or ""),
            ]).encode("utf-8", errors="replace")
        ).hexdigest()
        conn.execute(
            """
            INSERT INTO local_llm_tasks (task_type, entity_type, entity_id, lane_id, status, input_hash)
            SELECT 'application_review', 'application_kit', ?, ?, 'pending', ?
            WHERE NOT EXISTS (
                SELECT 1 FROM local_llm_tasks
                WHERE task_type = 'application_review'
                  AND entity_type = 'application_kit'
                  AND entity_id = ?
                  AND input_hash = ?
                  AND status IN ('pending', 'running', 'complete')
            )
            """,
            (application_kit_id, lane_id or row["lane_id"], payload_hash, application_kit_id, payload_hash),
        )
        conn.commit()
    return True


def save_application_kit_review(application_kit_id, review, provider="local"):
    payload = json.dumps({"provider": provider, **(review or {})}, ensure_ascii=False)
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE application_kits
            SET review_json = ?,
                review_updated_at = datetime('now')
            WHERE id = ?
            """,
            (payload, application_kit_id),
        )
        conn.commit()
    return True


def add_job(job_data, source, profile_id=1, log_callback=None):
    """Adds a new job to the database, ignoring duplicates."""
    source = normalize_source(source)
    if not _should_store_scraped_job(job_data, source, profile_id, log_callback):
            return False
    metadata = extract_job_metadata(job_data)
    if _closing_date_is_expired(metadata):
        if log_callback:
            log_callback(f"Skipped closed job '{job_data.get('title') or 'Untitled'}' from {source}: applications closed on {metadata.get('closing_date')}.")
        return False
    company = apply_company_profile_cache(classify_company_intelligence({**job_data, **metadata}))
    normalized_url = normalize_job_url(job_data.get('url'))
    fingerprint = description_fingerprint(job_data.get('description'))
    query = """
        INSERT OR IGNORE INTO jobs 
        (title, company, location, url, description, source, pdf_text, position_description_text, profile_id,
         date_scraped, last_interaction_at, updated_at, closing_date, contact_person,
         contact_email, contact_phone, contact_records_json, salary, closing_date_source, description_fingerprint, advertiser_company,
         actual_company, employer_type, company_confidence, company_intelligence,
         company_research_updated_at, last_seen_at, missing_sweeps) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 0)
    """
    try:
        with get_db_connection() as conn:
            existing = conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (normalized_url,)).fetchone()
            if existing:
                _update_existing_scraped_job(conn, existing["id"], job_data, metadata, fingerprint)
                conn.commit()
                sync_legacy_job_to_lane_model(existing["id"], profile_id, source=source, keyword=job_data.get("search_keyword"))
                if log_callback:
                    log_callback(f"Duplicate skipped by normalized URL and refreshed metadata: {job_data.get('title')}")
                return False
            equivalent = _find_existing_equivalent_job(
                conn,
                profile_id,
                job_data.get("title"),
                job_data.get("company"),
            )
            if equivalent:
                _update_existing_scraped_job(conn, equivalent["id"], job_data, metadata, fingerprint)
                conn.commit()
                sync_legacy_job_to_lane_model(equivalent["id"], profile_id, source=source, keyword=job_data.get("search_keyword"))
                if log_callback:
                    log_callback(
                        "Duplicate skipped by matching title/company: "
                        f"{job_data.get('title')} at {job_data.get('company')} "
                        f"(already tracked as {equivalent['pipeline_stage'] or equivalent['status']})."
                    )
                return False
            if fingerprint:
                duplicate = conn.execute(
                    """
                    SELECT id FROM jobs
                    WHERE description_fingerprint = ?
                    LIMIT 1
                    """,
                    (fingerprint,),
                ).fetchone()
                if duplicate:
                    _update_existing_scraped_job(conn, duplicate["id"], job_data, metadata, fingerprint)
                    conn.commit()
                    sync_legacy_job_to_lane_model(duplicate["id"], profile_id, source=source, keyword=job_data.get("search_keyword"))
                    if log_callback:
                        log_callback(f"Duplicate skipped by identical description: {job_data.get('title')}")
                    return False
            params = (
                job_data.get('title'), job_data.get('company'), job_data.get('location'),
                normalized_url, job_data.get('description'), source,
                job_data.get('pdf_text'),
                job_data.get('position_description_text') or job_data.get('pdf_text'),
                profile_id, metadata.get("closing_date"),
                metadata.get("contact_person"), metadata.get("contact_email"),
                metadata.get("contact_phone"), metadata.get("contact_records_json"), metadata.get("salary"),
                metadata.get("closing_date_source"), fingerprint,
                company.get("advertiser_company"), company.get("actual_company"),
                company.get("employer_type"), company.get("company_confidence"),
                company.get("company_intelligence"), company.get("company_research_updated_at")
            )
            _execute_with_retry(conn, query, params, is_commit=True)
            # rowcount is unreliable for INSERT OR IGNORE; check existence instead
            inserted = conn.execute("SELECT id FROM jobs WHERE url = ?", (normalized_url,)).fetchone()
            if inserted:
                sync_legacy_job_to_lane_model(inserted["id"], profile_id, source=source, keyword=job_data.get("search_keyword"))
            return bool(inserted)
    except sqlite3.Error as e:
        if log_callback:
            log_callback(f"DB Error in add_job: {e}")
        return False


def update_job_analysis(job_id, analysis_text, score, analysis_signature=None):
    """Updates a job record with the AI analysis results."""
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT pipeline_stage, status, fragment_score, title, company,
                   advertiser_company, employer_type, source
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        # Bounded conversion prior (item 6): the score the UI sorts by reflects
        # observed outcomes, but never crosses the auto-reject line on its own.
        composite_score = (
            composite_score_with_prior(score, row["fragment_score"], dict(row))
            if row else calculate_composite_score(score, None)
        )
        normalized_stage = normalize_stage(row["pipeline_stage"] or row["status"]) if row else "new"
        if score is not None and int(score) < AUTO_REJECT_THRESHOLD and normalized_stage not in {"applied", "interviewing", "offer", "rejected", "rejected_by_company", "archived"}:
            conn.execute(
                """
                UPDATE jobs
                SET ai_analysis = ?,
                    match_score = ?,
                    composite_score = ?,
                    analysis_signature = ?,
                    status = 'rejected',
                    pipeline_stage = 'rejected',
                    next_action = NULL,
                    next_action_date = NULL,
                    updated_at = datetime('now'),
                    last_interaction_at = datetime('now')
                WHERE id = ?
                """,
                (analysis_text, score, composite_score, analysis_signature, job_id),
            )
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
                (job_id, "stage", "Auto-rejected low match", f"Match score {score}% is below the {AUTO_REJECT_THRESHOLD}% threshold."),
            )
            conn.commit()
        else:
            _execute_with_retry(
                conn,
                "UPDATE jobs SET ai_analysis = ?, match_score = ?, composite_score = ?, analysis_signature = ?, updated_at = datetime('now') WHERE id = ?",
                (analysis_text, score, composite_score, analysis_signature, job_id),
                is_commit=True,
            )
    sync_legacy_job_to_lane_model(job_id)


def reject_low_match_jobs(threshold=AUTO_REJECT_THRESHOLD, profile_id=None, log_callback=None):
    """Move analysed, low-scoring pre-application jobs out of active pipeline views."""
    params = [threshold]
    profile_clause = ""
    if profile_id:
        profile_clause = "AND profile_id = ?"
        params.append(profile_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, match_score
            FROM jobs
            WHERE match_score IS NOT NULL
            AND match_score < ?
            AND pipeline_stage NOT IN ('applied', 'interviewing', 'offer', 'rejected', 'rejected_by_company', 'archived')
            {profile_clause}
            """,
            params,
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'rejected',
                    pipeline_stage = 'rejected',
                    next_action = NULL,
                    next_action_date = NULL,
                    updated_at = datetime('now'),
                    last_interaction_at = datetime('now')
                WHERE id = ?
                """,
                (row["id"],),
            )
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
                (row["id"], "stage", "Auto-rejected low match", f"Match score {row['match_score']}% is below the {threshold}% threshold."),
            )
        conn.commit()
    if rows and log_callback:
        log_callback(f"Auto-rejected {len(rows)} analysed jobs below {threshold}% match.")
    return len(rows)


def reset_rejected_to_new(profile_id=None):
    """Move all auto/manually rejected jobs back to 'new' and clear their analysis so they are re-scored.

    Only touches pipeline_stage='rejected'. Leaves 'rejected_by_company' and
    'archived' alone — those reflect human or employer decisions.
    """
    profile_clause = "AND profile_id = ?" if profile_id else ""
    params = [profile_id] if profile_id else []
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id FROM jobs
            WHERE pipeline_stage = 'rejected'
            {profile_clause}
            """,
            params,
        ).fetchall()
        job_ids = [row["id"] for row in rows]
        if not job_ids:
            return 0
        placeholders = ",".join("?" * len(job_ids))
        conn.execute(
            f"""
            UPDATE jobs
            SET status = 'new',
                pipeline_stage = 'new',
                match_score = NULL,
                composite_score = NULL,
                ai_analysis = NULL,
                analysis_signature = NULL,
                next_action = NULL,
                next_action_date = NULL,
                updated_at = datetime('now'),
                last_interaction_at = datetime('now')
            WHERE id IN ({placeholders})
            """,
            job_ids,
        )
        for job_id in job_ids:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
                (job_id, "stage", "Reset to new", "Bulk reset from rejected to new for re-analysis."),
            )
        conn.commit()
    return len(job_ids)


def refresh_closing_date_metadata(limit=2000, log_callback=None):
    """Re-parse active job ads for explicit closing dates and reject ads already closed."""
    updated = 0
    rejected = 0
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE pipeline_stage NOT IN ('applied', 'interviewing', 'offer', 'rejected', 'rejected_by_company', 'archived')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            data = {key: row[key] for key in row.keys()}
            metadata = extract_job_metadata(data)
            if metadata.get("closing_date_source") != "advertisement":
                continue
            if _closing_date_is_expired(metadata):
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'rejected',
                        pipeline_stage = 'rejected',
                        closing_date = ?,
                        closing_date_source = ?,
                        retired_reason = ?,
                        next_action = NULL,
                        next_action_date = NULL,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        metadata["closing_date"],
                        metadata["closing_date_source"],
                        f"Applications closed on {metadata['closing_date']}.",
                        row["id"],
                    ),
                )
                conn.execute(
                    "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
                    (row["id"], "retired", "Automatically retired", f"Applications closed on {metadata['closing_date']}."),
                )
                rejected += 1
            elif row["closing_date"] != metadata["closing_date"] or row["closing_date_source"] != "advertisement":
                conn.execute(
                    """
                    UPDATE jobs
                    SET closing_date = ?,
                        closing_date_source = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (metadata["closing_date"], metadata["closing_date_source"], row["id"]),
                )
                updated += 1
        conn.commit()
    if log_callback and (updated or rejected):
        log_callback(f"Closing dates refreshed for {updated} jobs; retired {rejected} closed ads.")
    return {"updated": updated, "rejected": rejected}


def get_jobs_by_status(status, profile_id=None):
    """Fetches jobs from the database filtered by status."""
    query = "SELECT * FROM jobs WHERE status = ?"
    params = [status]
    if profile_id:
        query += " AND profile_id = ?"
        params.append(profile_id)
    query += " ORDER BY match_score DESC, id DESC"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()


def get_job_details(job_id):
    """Fetches full details for a single job by its ID."""
    query = """
        SELECT jobs.*, profiles.name AS profile_name
        FROM jobs
        LEFT JOIN profiles ON profiles.id = jobs.profile_id
        WHERE jobs.id = ?
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, (job_id,))
        return cursor.fetchone()


def refresh_job_company_intelligence(job_id):
    job = get_job_details(job_id)
    if not job:
        return None
    data = {key: job[key] for key in job.keys()}
    with get_db_connection() as conn:
        company = apply_company_profile_cache(classify_company_intelligence(data), conn)
        conn.execute(
            """
            UPDATE jobs
            SET advertiser_company = ?,
                actual_company = ?,
                employer_type = ?,
                company_confidence = ?,
                company_intelligence = ?,
                company_research_updated_at = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                company["advertiser_company"],
                company["actual_company"],
                company["employer_type"],
                company["company_confidence"],
                company["company_intelligence"],
                company["company_research_updated_at"],
                job_id,
            ),
        )
        conn.commit()
    sync_legacy_job_to_lane_model(job_id)
    return get_job_details(job_id)


def update_job_company_research(job_id, intelligence, employer_type=None, actual_company=None, confidence=None):
    job = get_job_details(job_id)
    if not job:
        return None
    current = {}
    if job["company_intelligence"]:
        try:
            current = json.loads(job["company_intelligence"])
        except Exception:
            current = {"previous_raw": job["company_intelligence"]}
    merged = {**current, **(intelligence or {})}
    if employer_type:
        merged["employer_type"] = employer_type
    if actual_company:
        merged["actual_company"] = actual_company
    if confidence:
        merged["confidence"] = confidence
    advertiser = merged.get("advertiser_company") or job["advertiser_company"] or job["company"] or "Unknown advertiser"
    actual = merged.get("actual_company") or actual_company or job["actual_company"] or advertiser
    key = _company_key(actual if actual != "Unknown" else advertiser)
    updated_at = datetime.now().isoformat(timespec="seconds")
    payload = json.dumps(merged, ensure_ascii=False)
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET actual_company = ?,
                employer_type = ?,
                company_confidence = ?,
                company_intelligence = ?,
                company_research_updated_at = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                actual,
                employer_type or merged.get("employer_type") or job["employer_type"],
                confidence or merged.get("confidence") or job["company_confidence"],
                payload,
                updated_at,
                job_id,
            ),
        )
        if key:
            conn.execute(
                """
                INSERT INTO company_profiles (
                    company_key, display_name, employer_type, website_domain,
                    intelligence, confidence, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    employer_type = excluded.employer_type,
                    website_domain = excluded.website_domain,
                    intelligence = excluded.intelligence,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    actual,
                    employer_type or merged.get("employer_type") or job["employer_type"],
                    (merged.get("evidence") or {}).get("application_domain") if isinstance(merged.get("evidence"), dict) else "",
                    payload,
                    confidence or merged.get("confidence") or job["company_confidence"],
                    updated_at,
                ),
            )
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, title, details) VALUES (?, ?, ?, ?)",
            (job_id, "company", "Company intelligence updated", payload[:4000]),
        )
        conn.commit()
    return get_job_details(job_id)


def backfill_missing_company_intelligence(limit=500):
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE company_intelligence IS NULL OR company_intelligence = ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            data = {key: row[key] for key in row.keys()}
            company = apply_company_profile_cache(classify_company_intelligence(data), conn)
            conn.execute(
                """
                UPDATE jobs
                SET advertiser_company = ?,
                    actual_company = ?,
                    employer_type = ?,
                    company_confidence = ?,
                    company_intelligence = ?,
                    company_research_updated_at = ?
                WHERE id = ?
                """,
                (
                    company["advertiser_company"],
                    company["actual_company"],
                    company["employer_type"],
                    company["company_confidence"],
                    company["company_intelligence"],
                    company["company_research_updated_at"],
                    row["id"],
                ),
            )
        conn.commit()
    return len(rows)


def delete_job(job_id):
    """Deletes a job from the database by its ID."""
    query = "DELETE FROM jobs WHERE id = ?"
    with get_db_connection() as conn:
        conn.execute(query, (job_id,))
        conn.commit()


def get_job_counts(profile_id=None):
    """Gets the count of new and approved jobs."""
    base = " WHERE "
    if profile_id:
        base += "profile_id = ? AND "
    query_new = f"SELECT COUNT(*) FROM jobs{base}status = 'new'"
    query_approved = f"SELECT COUNT(*) FROM jobs{base}status = 'approved'"
    with get_db_connection() as conn:
        if profile_id:
            params = (profile_id,)
        else:
            params = ()
        new_count = conn.execute(query_new, params).fetchone()[0]
        approved_count = conn.execute(query_approved, params).fetchone()[0]
        return new_count, approved_count


# Location, salary and the two company columns are here for the deterministic
# screen that runs at the top of the analysis loop, not for the model. Without
# them the screener reads None for every field, resolves nothing, and passes
# every job — silently, because "unresolved always passes" is exactly what it is
# supposed to do when the data really is missing.
_ANALYSIS_COLUMNS = (
    "id, title, description, pdf_text, position_description_text, "
    "analysis_signature, ai_analysis, location, salary, company, "
    "actual_company, advertiser_company"
)


def get_jobs_to_analyze(status_filter, re_analyze, profile_id=None, resume_text=""):
    """Fetches jobs that need AI analysis."""
    base_query = f"SELECT {_ANALYSIS_COLUMNS} FROM jobs WHERE status = ? AND description IS NOT NULL"
    params = [status_filter]
    if profile_id:
        base_query += " AND profile_id = ?"
        params.append(profile_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(base_query, params)
        rows = cursor.fetchall()
    if re_analyze:
        return rows
    return [
        row for row in rows
        if not row["ai_analysis"]
        or row["analysis_signature"] != make_analysis_signature(
            resume_text,
            row["description"],
            row["pdf_text"],
            row["position_description_text"],
        )
    ]


def get_jobs_to_analyze_by_ids(job_ids, profile_id=None):
    """Fetches specific jobs by a list of IDs for analysis."""
    if not job_ids:
        return []
    placeholders = ','.join('?' for _ in job_ids)
    query = f"SELECT {_ANALYSIS_COLUMNS} FROM jobs WHERE id IN ({placeholders})"
    params = list(job_ids)
    if profile_id:
        query += " AND profile_id = ?"
        params.append(profile_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()


def clear_all_jobs(profile_id=None):
    """Deletes all jobs from the database, optionally scoped to a profile."""
    with get_db_connection() as conn:
        if profile_id:
            conn.execute("DELETE FROM jobs WHERE profile_id = ?", (profile_id,))
        else:
            conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
        conn.commit()


def get_jobs_with_filters(status_filter, min_score=None, source=None, date_from=None, profile_id=None):
    """Fetches jobs with optional filtering by score, source, and date."""
    base_query = "SELECT * FROM jobs WHERE status = ? AND description IS NOT NULL"
    params = [status_filter]
    if profile_id:
        base_query += " AND profile_id = ?"
        params.append(profile_id)

    if min_score is not None:
        base_query += " AND match_score >= ?"
        params.append(min_score)

    if source:
        aliases = source_aliases(source)
        base_query += f" AND source IN ({','.join('?' for _ in aliases)})"
        params.extend(aliases)

    if date_from:
        base_query += " AND date_scraped >= ?"
        params.append(date_from)

    base_query += " ORDER BY match_score DESC, id DESC"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(base_query, params)
        return cursor.fetchall()


def normalize_stage(stage):
    """Maps old statuses and UI stage ids into supported pipeline stages."""
    mapping = {
        "approved": "interested",
        "stale": "archived",
        "docs_drafted": "interested",
        "interview_1": "interviewing",
        "interview_2": "interviewing",
        "interview_3": "interviewing",
        "final": "offer",
        "company_rejected": "rejected_by_company",
        "declined_by_company": "rejected_by_company",
    }
    stage = mapping.get(stage, stage)
    return stage if stage in PIPELINE_STAGES else "new"


def retire_expired_pipeline_jobs(log_callback=None, profile_id=None):
    """
    Rejects pre-application jobs when an explicit ad closing date passes,
    interested jobs when there has been no interaction for 30 days, and
    applied jobs when no interview has been recorded after 50 days.
    """
    profile_clause = ""
    params = []
    if profile_id:
        profile_clause = " AND profile_id = ?"
        params.append(profile_id)

    query = f"""
        SELECT id, title, pipeline_stage, closing_date, closing_date_source, last_interaction_at
        FROM jobs
        WHERE pipeline_stage NOT IN ('applied', 'interviewing', 'offer', 'rejected', 'rejected_by_company', 'archived')
        {profile_clause}
    """
    now = datetime.now()
    retired = []
    employer_declined = []
    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            reason = None
            if row["closing_date"]:
                try:
                    closing = datetime.fromisoformat(row["closing_date"][:10])
                    if closing.date() < now.date() and row["closing_date_source"] in ("advertisement", "provided"):
                        reason = f"Closing date passed ({row['closing_date'][:10]})."
                except ValueError:
                    pass
            if reason is None and row["pipeline_stage"] in ACTIVE_PRE_APPLICATION_STAGES and row["last_interaction_at"]:
                try:
                    last_interaction = datetime.fromisoformat(row["last_interaction_at"].replace("Z", "").split(".")[0])
                    if last_interaction < now - timedelta(days=30):
                        reason = "No interaction for 30 days."
                except ValueError:
                    pass

            if reason:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'rejected',
                        pipeline_stage = 'rejected',
                        retired_reason = ?,
                        next_action = NULL,
                        next_action_date = NULL,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (reason, row["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO application_events (job_id, event_type, title, details)
                    VALUES (?, 'retired', 'Automatically retired', ?)
                    """,
                    (row["id"], reason),
                )
                retired.append(row["id"])

        applied_threshold = (now - timedelta(days=APPLIED_EMPLOYER_DECLINE_DAYS)).date().isoformat()
        applied_rows = conn.execute(
            f"""
            SELECT id, title, company, application_date
            FROM jobs
            WHERE pipeline_stage = 'applied'
            AND application_date IS NOT NULL
            AND date(application_date) <= date(?)
            AND NOT EXISTS (
                SELECT 1 FROM interviews
                WHERE interviews.job_id = jobs.id
            )
            {profile_clause}
            """,
            [applied_threshold] + params,
        ).fetchall()
        for row in applied_rows:
            reason = (
                f"No interview recorded {APPLIED_EMPLOYER_DECLINE_DAYS} days after "
                f"application ({str(row['application_date'])[:10]})."
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = 'rejected_by_company',
                    pipeline_stage = 'rejected_by_company',
                    retired_reason = ?,
                    next_action = NULL,
                    next_action_date = NULL,
                    last_interaction_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (reason, row["id"]),
            )
            conn.execute(
                """
                INSERT INTO application_events (job_id, event_type, title, details)
                VALUES (?, 'stage', 'Automatically marked declined by employer', ?)
                """,
                (row["id"], reason),
            )
            # A silent no-response after 50 days is a ghost, not an explicit
            # employer decline — the outcome funnel distinguishes the two.
            try:
                set_application_outcome(conn, row["id"], OUTCOME_GHOSTED)
            except Exception as exc:
                print(f"Outcome snapshot sync failed for ghosted job {row['id']}: {exc}")
            employer_declined.append(row["id"])
        conn.commit()
    if log_callback and retired:
        log_callback(f"Retired {len(retired)} inactive/expired pipeline jobs.")
    if log_callback and employer_declined:
        log_callback(
            f"Marked {len(employer_declined)} application"
            f"{'' if len(employer_declined) == 1 else 's'} declined by employer "
            f"after {APPLIED_EMPLOYER_DECLINE_DAYS} days without an interview."
        )
    for job_id in employer_declined:
        try:
            record_fragment_outcomes(job_id, "rejected_by_company")
        except Exception as exc:
            print(f"Fragment outcome propagation failed for job {job_id} -> rejected_by_company: {exc}")
    return len(retired) + len(employer_declined)


# Columns the UI's job list actually renders (matches python_bridge's
# JOB_SUMMARY_FIELDS). The compact path projects to these in SQL so the
# heavy text columns (description, pdf_text, ai_analysis, resume/cover text,
# fragment_alignment_json) never leave SQLite — with thousands of jobs that
# is the difference between shipping kilobytes and tens of megabytes per refresh.
PIPELINE_SUMMARY_COLUMNS = (
    "id", "profile_id", "title", "company", "location", "source", "url",
    "pipeline_stage", "status", "priority", "match_score", "composite_score",
    "fragment_score", "closing_date", "closing_date_source", "salary",
    "application_date", "application_url", "contact_person", "contact_email",
    "contact_phone", "interview_date", "interview_type", "interview_people",
    "feedback", "notes", "next_action", "next_action_date", "retired_reason",
    "last_interaction_at", "date_scraped", "updated_at",
    "employer_type", "actual_company", "advertiser_company", "company_confidence",
    "job_flags_types", "job_flags_json", "channel",
    # Screening results travel with the summary because a set-aside job must
    # still be able to say why. A blocked role keeps its row and its reason;
    # hiding the reason from the list would make it look like a disappearance.
    "commute_km", "commute_sector", "commute_verdict", "commute_reason",
    "salary_min", "salary_max", "salary_currency", "salary_period",
    "salary_confidence", "screen_score_delta", "screened_at",
)


def recurrence_key(company, title):
    """Cheap normalised (company, title) key for repost matching."""
    return (str(company or "").strip().lower(), str(title or "").strip().lower())


def recurrence_index(profile_id=None, include_all_profiles=False):
    """Map of normalised (company, title) -> count, only for roles seen 2+ times.

    A single grouped query with HAVING returns just the recurring set (usually a
    handful), so the board can flag reposts without an N+1 lookup or a full scan.
    """
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT LOWER(TRIM(company)) AS c, LOWER(TRIM(title)) AS t, COUNT(*) AS n
            FROM jobs
            WHERE TRIM(COALESCE(title, '')) <> '' {profile_clause}
            GROUP BY c, t
            HAVING n >= 2
            """,
            params,
        ).fetchall()
    return {(row["c"], row["t"]): row["n"] for row in rows}


def recurrence_count_for(job):
    """Repost count for a single job dict/row (used by the detail view)."""
    company = job.get("company") if hasattr(job, "get") else job["company"]
    title = job.get("title") if hasattr(job, "get") else job["title"]
    if not str(title or "").strip():
        return 0
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE LOWER(TRIM(company)) = ? AND LOWER(TRIM(title)) = ?",
            (str(company or "").strip().lower(), str(title or "").strip().lower()),
        ).fetchone()
    return row["n"] if row else 0


def get_pipeline_jobs(filters=None):
    filters = filters or {}
    include_all_profiles = bool(filters.get("include_all_profiles"))
    profile_id = filters.get("profile_id")
    params = []
    clauses = ["1 = 1"]

    if profile_id and not include_all_profiles:
        clauses.append("jobs.profile_id = ?")
        params.append(profile_id)
    if filters.get("stage"):
        clauses.append("jobs.pipeline_stage = ?")
        params.append(normalize_stage(filters["stage"]))
    if filters.get("source"):
        aliases = source_aliases(filters["source"])
        clauses.append(f"jobs.source IN ({','.join('?' for _ in aliases)})")
        params.extend(aliases)
    if filters.get("company"):
        clauses.append("jobs.company LIKE ?")
        params.append(f"%{filters['company']}%")
    if filters.get("location"):
        aliases = location_aliases(filters["location"])
        clauses.append(f"({' OR '.join('jobs.location LIKE ?' for _ in aliases)})")
        params.extend([f"%{alias}%" for alias in aliases])
    work_modes = [mode for mode in _split_csv(filters.get("work_modes")) if mode in WORK_MODE_OPTIONS]
    if set(work_modes) >= set(WORK_MODE_OPTIONS):
        work_modes = []
    if work_modes:
        mode_clauses = []
        mode_params = []
        mode_terms = {
            "hybrid": ["hybrid"],
            "remote": ["remote", "work remotely"],
            "wfh": ["wfh", "work from home", "working from home"],
            "onsite": ["on site", "on-site", "onsite", "office based", "office-based"],
        }
        haystack = "LOWER(COALESCE(jobs.description, '') || ' ' || COALESCE(jobs.location, '') || ' ' || COALESCE(jobs.ai_analysis, ''))"
        for mode in work_modes:
            for term in mode_terms.get(mode, []):
                mode_clauses.append(f"{haystack} LIKE ?")
                mode_params.append(f"%{term}%")
        if mode_clauses:
            clauses.append(f"({' OR '.join(mode_clauses)})")
            params.extend(mode_params)
    if filters.get("date_from"):
        clauses.append("COALESCE(jobs.date_scraped, jobs.updated_at, jobs.last_interaction_at) >= ?")
        params.append(filters["date_from"])
    if filters.get("min_score") not in (None, ""):
        clauses.append("(jobs.match_score IS NULL OR jobs.match_score >= ?)")
        params.append(int(filters["min_score"]))
    if filters.get("max_score") not in (None, ""):
        clauses.append("COALESCE(jobs.match_score, 0) <= ?")
        params.append(int(filters["max_score"]))
    if filters.get("has_interview"):
        clauses.append("(jobs.pipeline_stage = 'interviewing' OR EXISTS (SELECT 1 FROM interviews WHERE interviews.job_id = jobs.id))")
    if filters.get("has_feedback"):
        clauses.append("jobs.feedback IS NOT NULL AND jobs.feedback != ''")
    if filters.get("query"):
        query = f"%{filters['query']}%"
        clauses.append(
            """
            (
                jobs.title LIKE ? OR jobs.company LIKE ? OR jobs.location LIKE ? OR
                jobs.description LIKE ? OR jobs.ai_analysis LIKE ? OR jobs.notes LIKE ? OR
                profiles.name LIKE ?
            )
            """
        )
        params.extend([query] * 7)

    if filters.get("compact"):
        # The multi-KB company_intelligence JSON blob is only consulted by the
        # list UI to ask "has this employer been researched?" — compute that
        # bit in SQL instead of shipping ~10 MB of JSON every refresh.
        select_clause = (
            ", ".join(f"jobs.{column}" for column in PIPELINE_SUMMARY_COLUMNS)
            + ", CASE WHEN jobs.company_intelligence LIKE '%ai_research%'"
            + " OR jobs.company_intelligence LIKE '%cached_company_profile%'"
            + " THEN 1 ELSE 0 END AS has_company_research"
            # Truncated text so deterministic ad-signals (friction, trigger,
            # reporting line) can be derived without shipping the full blob.
            + ", SUBSTR(COALESCE(jobs.description, ''), 1, 2500) AS ad_text"
        )
    else:
        select_clause = "jobs.*"
    sql = f"""
        SELECT {select_clause}, profiles.name AS profile_name
        FROM jobs
        LEFT JOIN profiles ON profiles.id = jobs.profile_id
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE jobs.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 WHEN 'low' THEN 2 ELSE 1 END,
            COALESCE(jobs.next_action_date, '9999-12-31') ASC,
            COALESCE(jobs.match_score, 0) DESC,
            jobs.id DESC
    """
    with get_db_connection() as conn:
        return conn.execute(sql, params).fetchall()


def get_interviewed_jobs(profile_id=None, include_all_profiles=False, limit=100):
    """Jobs that reached an interview, with round count and latest date — the
    candidates for interview-validated mining."""
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        return conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.pipeline_stage, jobs.profile_id,
                   profiles.name AS profile_name,
                   COUNT(interviews.id) AS interview_rounds,
                   MAX(interviews.interview_date) AS latest_interview_date
            FROM jobs
            JOIN interviews ON interviews.job_id = jobs.id
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE 1 = 1 {profile_clause}
            GROUP BY jobs.id
            ORDER BY latest_interview_date DESC, jobs.id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()


def archive_stale_applications(job_ids, reason="No response after 30 days"):
    if not job_ids:
        return []
    placeholders = ",".join("?" for _ in job_ids)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, company
            FROM jobs
            WHERE id IN ({placeholders})
            AND pipeline_stage = 'applied'
            """,
            list(job_ids),
        ).fetchall()
        valid_ids = [row["id"] for row in rows]
        if not valid_ids:
            return []

        update_placeholders = ",".join("?" for _ in valid_ids)
        conn.execute(
            f"""
            UPDATE jobs
            SET pipeline_stage = 'archived',
                status = 'archived',
                retired_reason = ?,
                next_action = NULL,
                next_action_date = NULL,
                last_interaction_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id IN ({update_placeholders})
            """,
            [reason] + valid_ids,
        )
        conn.executemany(
            """
            INSERT INTO application_events (job_id, event_type, title, details)
            VALUES (?, 'cleanup', 'Archived as no response', ?)
            """,
            [(job_id, reason) for job_id in valid_ids],
        )
        conn.commit()
        return rows


def move_job_to_profile(job_id, profile_id):
    target_profile = get_profile_by_id(profile_id)
    if not target_profile:
        raise ValueError(f"Profile {profile_id} was not found.")

    job = get_job_details(job_id)
    if not job:
        raise ValueError(f"Job {job_id} was not found.")

    current_profile_id = job["profile_id"]
    if current_profile_id == profile_id:
        return get_job_details(job_id)

    current_name = job["profile_name"] or "Unassigned"
    target_name = target_profile["name"]
    previous_score = job["match_score"]
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET profile_id = ?,
                ai_analysis = NULL,
                match_score = NULL,
                analysis_signature = NULL,
                last_interaction_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (profile_id, job_id),
        )
        conn.execute(
            """
            INSERT INTO application_events (job_id, event_type, title, details)
            VALUES (?, 'profile', 'Moved to profile', ?)
            """,
            (
                job_id,
                f"{current_name} -> {target_name}"
                + (f"\nPrevious profile match score was {previous_score}%." if previous_score is not None else "")
                + "\nFit analysis cleared because profile evidence changed.",
            ),
        )
        conn.commit()
    return get_job_details(job_id)


def update_job_application(job_id, updates):
    if "additional_candidate_context" in updates:
        ensure_application_context_schema()
    allowed = {
        "pipeline_stage", "closing_date", "next_action", "next_action_date", "priority",
        "application_date", "application_url", "contact_person", "contact_email",
        "contact_phone", "resume_used", "resume_text", "cover_letter_path",
        "cover_letter_text", "position_description_path", "position_description_text",
        "additional_candidate_context",
        "interview_date", "interview_type", "interview_people",
        "feedback", "salary", "notes", "status", "advertiser_company", "actual_company",
        "employer_type", "company_confidence", "company_intelligence", "company_research_updated_at",
        "closing_date_source", "retired_reason",
    }
    values = {}
    for key, value in updates.items():
        if key in allowed:
            values[key] = value if value != "" else None

    if "pipeline_stage" in values:
        values["pipeline_stage"] = normalize_stage(values["pipeline_stage"])
        values["status"] = values["pipeline_stage"]
    elif "status" in values:
        values["status"] = normalize_stage(values["status"])
        values["pipeline_stage"] = values["status"]

    if values.get("pipeline_stage") == "new":
        values["next_action"] = None
        values["next_action_date"] = None

    if not values:
        return get_job_details(job_id)

    values["last_interaction_at"] = datetime.now().isoformat(timespec="seconds")
    values["updated_at"] = datetime.now().isoformat(timespec="seconds")
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [job_id]

    with get_db_connection() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)
        if "pipeline_stage" in values:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details, due_date) VALUES (?, ?, ?, ?, ?)",
                (
                    job_id,
                    "stage",
                    f"Moved to {values['pipeline_stage'].replace('_', ' ').title()}",
                    updates.get("notes"),
                    updates.get("next_action_date"),
                ),
            )
            # Mirror the transition into the immutable outcome snapshot. Kept in
            # the stage transaction so the snapshot and the stage move commit
            # atomically. Withdrawal is expressed by moving to 'archived'.
            try:
                _sync_outcome_for_stage(conn, job_id, values["pipeline_stage"], values.get("application_date"))
            except Exception as exc:
                print(f"Outcome snapshot sync failed for job {job_id} -> {values['pipeline_stage']}: {exc}")
        elif updates.get("notes") or updates.get("feedback"):
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, title, details, due_date) VALUES (?, ?, ?, ?, ?)",
                (
                    job_id,
                    "note",
                    updates.get("next_action") or "Application update",
                    updates.get("notes") or updates.get("feedback"),
                    updates.get("next_action_date"),
                ),
            )
        conn.commit()
    # Best-effort outcome propagation for pipeline transitions. Runs outside
    # the stage transaction so a failure never blocks the stage move;
    # recompute_fragment_outcome_scores reconciles later anyway.
    if "pipeline_stage" in values:
        try:
            record_fragment_outcomes(job_id, values["pipeline_stage"])
        except Exception as exc:
            print(f"Fragment outcome propagation failed for job {job_id} -> {values['pipeline_stage']}: {exc}")
    return get_job_details(job_id)


def get_interviews(job_id):
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM interviews WHERE job_id = ? ORDER BY round_number ASC, interview_date ASC, id ASC",
            (job_id,),
        ).fetchall()


def add_interview(job_id, data):
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT COALESCE(MAX(round_number), 0) FROM interviews WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        round_number = data.get("round_number") or (existing + 1)
        cursor = conn.execute(
            """
            INSERT INTO interviews (
                job_id, round_number, title, interview_date, interview_type,
                people_met, notes, outcome, next_action, next_action_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                round_number,
                data.get("title") or f"Interview {round_number}",
                data.get("interview_date"),
                data.get("interview_type"),
                data.get("people_met"),
                data.get("notes"),
                data.get("outcome"),
                data.get("next_action"),
                data.get("next_action_date"),
            ),
        )
        conn.execute(
            """
            UPDATE jobs
            SET pipeline_stage = 'interviewing',
                status = 'interviewing',
                interview_date = COALESCE(?, interview_date),
                interview_type = COALESCE(?, interview_type),
                interview_people = COALESCE(?, interview_people),
                notes = COALESCE(?, notes),
                next_action = COALESCE(?, next_action),
                next_action_date = COALESCE(?, next_action_date),
                last_interaction_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                data.get("interview_date"),
                data.get("interview_type"),
                data.get("people_met"),
                data.get("notes"),
                data.get("next_action"),
                data.get("next_action_date"),
                job_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO application_events (job_id, event_type, title, details, due_date)
            VALUES (?, 'interview', ?, ?, ?)
            """,
            (
                job_id,
                f"Interview {round_number} added",
                data.get("notes"),
                data.get("interview_date") or data.get("next_action_date"),
            ),
        )
        rounds = conn.execute(
            "SELECT COUNT(*) FROM interviews WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        try:
            set_application_outcome(conn, job_id, OUTCOME_INTERVIEW, interview_rounds=rounds)
        except Exception as exc:
            print(f"Outcome snapshot sync failed for interview on job {job_id}: {exc}")
        conn.commit()
    # An interview is our strongest positive signal: mine interview-validated
    # fragments from this job's JD + application docs (best-effort, off-thread).
    _schedule_interview_fragment_mining(job_id)
    return cursor.lastrowid


def update_interview(interview_id, data):
    allowed = {
        "title", "interview_date", "interview_type", "people_met",
        "notes", "outcome", "next_action", "next_action_date",
    }
    values = {key: (data.get(key) if data.get(key) != "" else None) for key in allowed if key in data}
    if not values:
        return None

    with get_db_connection() as conn:
        existing = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
        if not existing:
            raise ValueError(f"Interview {interview_id} was not found.")

        values["updated_at"] = datetime.now().isoformat(timespec="seconds")
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(
            f"UPDATE interviews SET {assignments} WHERE id = ?",
            list(values.values()) + [interview_id],
        )

        updated = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
        conn.execute(
            """
            UPDATE jobs
            SET pipeline_stage = 'interviewing',
                status = 'interviewing',
                interview_date = COALESCE(?, interview_date),
                interview_type = COALESCE(?, interview_type),
                interview_people = COALESCE(?, interview_people),
                next_action = COALESCE(?, next_action),
                next_action_date = COALESCE(?, next_action_date),
                last_interaction_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                updated["interview_date"],
                updated["interview_type"],
                updated["people_met"],
                updated["next_action"],
                updated["next_action_date"],
                updated["job_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO application_events (job_id, event_type, title, details, due_date)
            VALUES (?, 'interview', ?, ?, ?)
            """,
            (
                updated["job_id"],
                f"Interview {updated['round_number']} updated",
                updated["notes"],
                updated["interview_date"] or updated["next_action_date"],
            ),
        )
        rounds = conn.execute(
            "SELECT COUNT(*) FROM interviews WHERE job_id = ?", (updated["job_id"],)
        ).fetchone()[0]
        try:
            set_application_outcome(conn, updated["job_id"], OUTCOME_INTERVIEW, interview_rounds=rounds)
        except Exception as exc:
            print(f"Outcome snapshot sync failed for interview update on job {updated['job_id']}: {exc}")
        conn.commit()
        return updated


def get_job_events(job_id):
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM application_events WHERE job_id = ? ORDER BY COALESCE(event_date, created_at) DESC, id DESC",
            (job_id,),
        ).fetchall()


def add_application_event(job_id, event_type, title, details=None, event_date=None, due_date=None):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO application_events (job_id, event_type, title, details, event_date, due_date)
            VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')), ?)
            """,
            (job_id, event_type, title, details, event_date, due_date),
        )
        conn.execute(
            "UPDATE jobs SET last_interaction_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    return True


def mark_missing_new_jobs_after_sweep(profile_id, sources, sweep_started_at, threshold=3, log_callback=None):
    """Archive untouched new jobs that disappear from the same source repeatedly."""
    normalized_sources = [normalize_source(source) for source in (sources or []) if source]
    normalized_sources = list(dict.fromkeys(normalized_sources))
    if not normalized_sources or not sweep_started_at:
        return {"incremented": 0, "archived": 0}

    placeholders = ",".join("?" for _ in normalized_sources)
    params = [profile_id, *normalized_sources, sweep_started_at]
    new_stage_clause = """
        COALESCE(NULLIF(pipeline_stage, ''), NULLIF(status, ''), 'new') = 'new'
        AND COALESCE(NULLIF(status, ''), NULLIF(pipeline_stage, ''), 'new') = 'new'
    """

    with get_db_connection() as conn:
        candidates = conn.execute(
            f"""
            SELECT id, title, company, COALESCE(missing_sweeps, 0) AS missing_sweeps
            FROM jobs
            WHERE profile_id = ?
              AND source IN ({placeholders})
              AND {new_stage_clause}
              AND COALESCE(last_seen_at, date_scraped, '1970-01-01 00:00:00') < ?
            """,
            params,
        ).fetchall()
        if not candidates:
            return {"incremented": 0, "archived": 0}

        ids = [row["id"] for row in candidates]
        id_placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE jobs
            SET missing_sweeps = COALESCE(missing_sweeps, 0) + 1,
                updated_at = datetime('now')
            WHERE id IN ({id_placeholders})
            """,
            ids,
        )
        archive_rows = conn.execute(
            f"""
            SELECT id, title, company, source, missing_sweeps
            FROM jobs
            WHERE id IN ({id_placeholders})
              AND missing_sweeps >= ?
            """,
            [*ids, int(threshold)],
        ).fetchall()

        archived_ids = [row["id"] for row in archive_rows]
        if archived_ids:
            archived_placeholders = ",".join("?" for _ in archived_ids)
            reason = f"Not seen in {int(threshold)} consecutive successful scraper sweeps; listing appears unavailable."
            conn.execute(
                f"""
                UPDATE jobs
                SET status = 'stale',
                    pipeline_stage = 'archived',
                    retired_reason = ?,
                    next_action = NULL,
                    next_action_date = NULL,
                    updated_at = datetime('now')
                WHERE id IN ({archived_placeholders})
                """,
                [reason, *archived_ids],
            )
            conn.executemany(
                """
                INSERT INTO application_events (job_id, event_type, title, details)
                VALUES (?, 'stage', 'Archived unavailable listing', ?)
                """,
                [(job_id, reason) for job_id in archived_ids],
            )
            conn.execute(
                f"""
                UPDATE lane_opportunities
                SET status = 'stale',
                    pipeline_stage = 'archived',
                    retired_reason = ?,
                    next_action = NULL,
                    next_action_date = NULL,
                    updated_at = datetime('now')
                WHERE legacy_job_id IN ({archived_placeholders})
                """,
                [reason, *archived_ids],
            )

        conn.commit()

    if archive_rows and log_callback:
        preview = ", ".join(
            f"{row['title']} at {row['company'] or row['source']}" for row in archive_rows[:5]
        )
        suffix = "" if len(archive_rows) <= 5 else f", plus {len(archive_rows) - 5} more"
        log_callback(f"Archived {len(archive_rows)} unavailable new job(s): {preview}{suffix}.")
    return {"incremented": len(candidates), "archived": len(archive_rows)}


def dedupe_database(log_callback=None):
    """Removes exact duplicate jobs by normalized URL and identical description fingerprint."""
    count_before_query = "SELECT COUNT(*) FROM jobs"
    with get_db_connection() as conn:
        count_before = conn.execute(count_before_query).fetchone()[0]
        # Use the stored description_fingerprint instead of recomputing it from
        # the (large) description text for every row on every call. add_job()
        # populates it on insert and the backfill pass below fills any NULLs, so
        # this avoids reading essentially all descriptions on each dedupe.
        rows = conn.execute("SELECT id, profile_id, url, description_fingerprint FROM jobs ORDER BY id").fetchall()
        normalized_by_id = {row["id"]: normalize_job_url(row["url"]) for row in rows}
        fingerprint_by_id = {row["id"]: row["description_fingerprint"] for row in rows}
        duplicate_to_keep = {}
        seen_urls = {}
        seen_fingerprints = {}
        for row in rows:
            url_key = normalized_by_id[row["id"]]
            fingerprint_key = fingerprint_by_id[row["id"]]
            if url_key in seen_urls:
                duplicate_to_keep[row["id"]] = seen_urls[url_key]
                continue
            if fingerprint_key and fingerprint_key in seen_fingerprints:
                duplicate_to_keep[row["id"]] = seen_fingerprints[fingerprint_key]
                continue
            seen_urls[url_key] = row["id"]
            if fingerprint_key:
                seen_fingerprints[fingerprint_key] = row["id"]

        identity_rows = conn.execute(
            """
            SELECT id, profile_id, title, company, pipeline_stage, status,
                   COALESCE(application_date, interview_date, updated_at, last_interaction_at, date_scraped, id) AS recency
            FROM jobs
            ORDER BY id ASC
            """
        ).fetchall()
        identity_groups = {}
        for row in identity_rows:
            if row["id"] in duplicate_to_keep:
                continue
            if not _is_meaningful_job_identity(row["title"], row["company"]):
                continue
            key = _job_identity_key(row["title"], row["company"])
            identity_groups.setdefault(key, []).append(row)
        for group in identity_groups.values():
            if len(group) < 2:
                continue
            keep = max(group, key=lambda row: (_stage_dedupe_rank(row), str(row["recency"] or ""), row["id"]))
            for row in group:
                if row["id"] != keep["id"]:
                    duplicate_to_keep[row["id"]] = keep["id"]

        if duplicate_to_keep:
            for duplicate_id, keep_id in sorted(duplicate_to_keep.items()):
                while keep_id in duplicate_to_keep:
                    keep_id = duplicate_to_keep[keep_id]
                duplicate_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (duplicate_id,)).fetchone()
                keep_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (keep_id,)).fetchone()
                if not duplicate_row or not keep_row:
                    continue
                posting_id = _upsert_job_posting_from_row(conn, keep_row)
                _upsert_lane_opportunity_from_row(
                    conn,
                    keep_row,
                    posting_id,
                    keep_row["profile_id"] or 1,
                )
                _upsert_lane_opportunity_from_row(
                    conn,
                    duplicate_row,
                    posting_id,
                    duplicate_row["profile_id"] or keep_row["profile_id"] or 1,
                )
                conn.execute(
                    "UPDATE lane_opportunities SET legacy_job_id = NULL WHERE legacy_job_id = ?",
                    (duplicate_id,),
                )
            placeholders = ",".join("?" for _ in duplicate_to_keep)
            conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(duplicate_to_keep.keys()))

        # Normalize URLs and backfill any missing fingerprints. In steady state
        # both columns are already correct, so this touches nothing — and it only
        # reads description text for the (usually zero) rows missing a fingerprint.
        rows = conn.execute("SELECT id, url, description_fingerprint FROM jobs").fetchall()
        url_updates = []
        missing_fingerprint_ids = []
        for row in rows:
            normalized_url = normalize_job_url(row["url"])
            if normalized_url != row["url"]:
                url_updates.append((normalized_url, row["id"]))
            if not row["description_fingerprint"]:
                missing_fingerprint_ids.append(row["id"])
        if url_updates:
            conn.executemany("UPDATE jobs SET url = ? WHERE id = ?", url_updates)
        if missing_fingerprint_ids:
            placeholders = ",".join("?" for _ in missing_fingerprint_ids)
            fp_rows = conn.execute(
                f"SELECT id, description FROM jobs WHERE id IN ({placeholders})",
                tuple(missing_fingerprint_ids),
            ).fetchall()
            fp_updates = []
            for fp_row in fp_rows:
                fingerprint = description_fingerprint(fp_row["description"])
                if fingerprint:
                    fp_updates.append((fingerprint, fp_row["id"]))
            if fp_updates:
                conn.executemany(
                    "UPDATE jobs SET description_fingerprint = ? WHERE id = ?", fp_updates
                )
        conn.commit()
        count_after = conn.execute(count_before_query).fetchone()[0]
    deleted = count_before - count_after
    if log_callback:
        log_callback(f"Deduping complete. Removed {deleted} duplicates.")
    return deleted


# Verdict key -> column. A screening pass supplies every key; a salary-only
# backfill supplies a subset, and only the keys present are written, so
# re-parsing pay does not erase a commute result computed against a geocoder
# that may not be reachable right now.
_SCREENING_COLUMNS = (
    ("commute_km", "commute_km"),
    ("commute_sector", "commute_sector"),
    ("verdict", "commute_verdict"),
    ("reason", "commute_reason"),
    ("score_delta", "screen_score_delta"),
    ("salary_min", "salary_min"),
    ("salary_max", "salary_max"),
    ("salary_currency", "salary_currency"),
    ("salary_period", "salary_period"),
    ("salary_confidence", "salary_confidence"),
)


def save_job_screening(job_id, verdict):
    """Persist a deterministic screening result against a job.

    Written even when the verdict is "blocked": the row stays in the pipeline
    with a readable reason so the user can see why a role was set aside and
    overrule it. Nothing here deletes or hides a job.
    """
    pairs = [(column, verdict[key]) for key, column in _SCREENING_COLUMNS if key in verdict]
    if not pairs:
        return
    assignments = ", ".join(f"{column} = ?" for column, _ in pairs)
    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments}, screened_at = datetime('now') WHERE id = ?",
            [value for _, value in pairs] + [job_id],
        )
        conn.commit()
