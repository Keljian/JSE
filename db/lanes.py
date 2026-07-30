"""Profiles/lanes, people, candidate memory fragments, and lane terms.

Split out of database_manager.py, which re-exports everything here.
"""
import sqlite3
import re
import hashlib
import json
import threading
from datetime import datetime, timedelta
from .connection import (
    DB_FILE,
    _execute_with_retry,
    get_db_connection,
)
from .text import (
    _clean,
    _company_key,
    _json_dumps_compact,
    _json_loads_maybe,
)
from .settings import (
    get_lane_settings,
    update_profile_settings,
)

_CONTACT_NON_NAMES = {
    "about", "apply", "application", "centre", "center", "company", "contact", "email",
    "enquiries", "further", "information", "job", "jobs", "please", "phone", "position",
    "recruitment", "role", "team", "technologies", "technology", "talent", "the", "via",
}


def _canonical_person_name(value):
    name = _clean(value)
    name = re.sub(r"(?i)\s+(?:on|via|at|email)\s*(?:[a-z][a-z0-9._-]*)?$", "", name).strip(" ,;:-")
    words = name.split()
    if not 2 <= len(words) <= 4:
        return ""
    if any(_company_key(word) in _CONTACT_NON_NAMES for word in words):
        return ""
    if not all(re.fullmatch(r"[A-Z][A-Za-z'’-]*|[A-Z]{2,}", word) for word in words):
        return ""
    return name


def _contact_email_name(email):
    local = str(email or "").split("@", 1)[0]
    parts = [part for part in re.split(r"[._-]+", local) if part]
    if len(parts) < 2 or any(not part.isalpha() or part.lower() in {"info", "jobs", "careers", "apply", "talent"} for part in parts):
        return ""
    return " ".join(part.capitalize() for part in parts[:3])


def _contact_names_overlap(left, right):
    a, b = set(_company_key(left).split()), set(_company_key(right).split())
    return bool(a and b and (len(a & b) >= 2 or (len(a & b) == 1 and min(len(a), len(b)) == 1)))


def get_all_profiles():
    """Returns all profiles ordered by created_at."""
    query = "SELECT * FROM profiles ORDER BY created_at ASC"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        return cursor.fetchall()


def get_all_lanes(include_inactive=True):
    """Returns all lanes. Physically backed by the legacy profiles table."""
    query = "SELECT * FROM profiles"
    params = []
    if not include_inactive:
        query += " WHERE COALESCE(active, 1) = 1"
    query += " ORDER BY created_at ASC"
    with get_db_connection() as conn:
        return conn.execute(query, params).fetchall()


def get_profile_by_id(profile_id):
    """Fetches a single profile by its ID."""
    query = "SELECT * FROM profiles WHERE id = ?"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, (profile_id,))
        return cursor.fetchone()


def get_lane_by_id(lane_id):
    return get_profile_by_id(lane_id)


def get_profile_by_name(name):
    """Fetches a single profile by its name."""
    query = "SELECT * FROM profiles WHERE name = ?"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, (name,))
        return cursor.fetchone()


def add_profile(name, resume_path):
    """Adds a new profile to the database."""
    query = "INSERT INTO profiles (name, resume_path) VALUES (?, ?)"
    try:
        with get_db_connection() as conn:
            _execute_with_retry(conn, query, (name, resume_path), is_commit=True)
            return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error:
        return False


def add_lane(name, resume_path, settings=None):
    if not add_profile(name, resume_path):
        return False
    lane = get_profile_by_name(name)
    if lane and settings:
        update_profile_settings(lane["id"], settings)
    return True


def update_profile(profile_id, name, resume_path):
    """Updates an existing profile's name and/or resume path."""
    query = "UPDATE profiles SET name = ?, resume_path = ? WHERE id = ?"
    try:
        with get_db_connection() as conn:
            _execute_with_retry(conn, query, (name, resume_path, profile_id), is_commit=True)
            return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error:
        return False


def update_lane(lane_id, name, resume_path, settings=None):
    if not update_profile(lane_id, name, resume_path):
        return False
    if settings:
        update_profile_settings(lane_id, settings)
    return True


def ensure_default_person(name="Candidate"):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM people ORDER BY id ASC LIMIT 1").fetchone()
        if row:
            return row
        conn.execute(
            "INSERT INTO people (id, name, contact_json) VALUES (1, ?, ?)",
            (name, json.dumps({"source": "database_manager"})),
        )
        conn.commit()
        return conn.execute("SELECT * FROM people WHERE id = 1").fetchone()


def get_person_for_lane(lane_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT people.*
            FROM profiles
            LEFT JOIN people ON people.id = COALESCE(profiles.person_id, 1)
            WHERE profiles.id = ?
            """,
            (lane_id,),
        ).fetchone()
    return row or ensure_default_person()


def _candidate_fragment_fingerprint(fragment):
    return _memory_fragment_fingerprint(fragment)


def upsert_candidate_fragments(person_id, fragments, replace=False, interview_validated=False):
    """Persist typed fragments to the cross-lane `candidate_fragments` bank.

    Mirrors the field handling in `upsert_profile_memory_fragments` so the
    activation metadata the LLM produces (keywords, anti_keywords, status,
    etc.) survives the round-trip. See that function for the rationale.

    `interview_validated` marks fragments mined from a job that reached an
    interview; the flag is sticky (once validated, always validated) so a
    later resume-corpus re-mine of the same fragment can't downgrade it.
    """
    now = datetime.now().isoformat(timespec="seconds")
    validated_flag = 1 if interview_validated else 0
    count = 0
    with get_db_connection() as conn:
        if replace:
            conn.execute("DELETE FROM candidate_fragments WHERE person_id = ?", (person_id,))
        for fragment in fragments or []:
            claim = _clean(str(fragment.get("claim") or ""))
            theme = _clean(str(fragment.get("theme") or ""))
            if not claim or not theme:
                continue
            clean = {
                "fragment_type": _clean(str(fragment.get("fragment_type") or "evidence"))[:80],
                "theme": theme[:160],
                "claim": claim[:1200],
                "supporting_detail": _clean(str(fragment.get("supporting_detail") or fragment.get("evidence") or ""))[:1600],
                "skills_json": _json_dumps_compact(fragment.get("skills") or fragment.get("skills_json") or []),
                "domains_json": _json_dumps_compact(fragment.get("domains") or fragment.get("domains_json") or []),
                "seniority": _clean(str(fragment.get("seniority") or ""))[:80],
                "source_job_ids_json": _json_dumps_compact(fragment.get("source_job_ids") or []),
                "source_doc_paths_json": _json_dumps_compact(fragment.get("source_doc_paths") or []),
                "reuse_guidance": _clean(str(fragment.get("reuse_guidance") or ""))[:1200],
                "confidence": _clean(str(fragment.get("confidence") or "medium"))[:40],
                "keywords_json": _json_dumps_compact(fragment.get("keywords") or []),
                "anti_keywords_json": _json_dumps_compact(fragment.get("anti_keywords") or []),
                "job_families_json": _json_dumps_compact(fragment.get("job_families") or []),
                "status": _clean(str(fragment.get("status") or "established"))[:32] or "established",
                "confidence_reasoning": _clean(str(fragment.get("confidence_reasoning") or ""))[:800],
                "reinforces_themes_json": _json_dumps_compact(fragment.get("reinforces_fragment_themes") or []),
                "support_count": int(fragment.get("support_count") or 1),
            }
            fingerprint = fragment.get("fingerprint") or _candidate_fragment_fingerprint(clean)
            # Merge source-job attribution across mining runs. Fragments dedupe by
            # fingerprint, so a piece of evidence shared by several interviewed
            # roles would otherwise be re-attributed to only the last job mined —
            # making the other roles' "mined" state flip back and the funnel
            # under-count. Union old + new provenance instead.
            existing_sources = conn.execute(
                "SELECT source_job_ids_json FROM candidate_fragments WHERE person_id = ? AND fingerprint = ?",
                (person_id, fingerprint),
            ).fetchone()
            merged_sources = []
            seen_sources = set()
            for source in (_json_loads_maybe(existing_sources["source_job_ids_json"], []) if existing_sources else []) + (fragment.get("source_job_ids") or []):
                key = str(source)
                if key not in seen_sources:
                    seen_sources.add(key)
                    merged_sources.append(source)
            clean["source_job_ids_json"] = _json_dumps_compact(merged_sources)
            conn.execute(
                """
                INSERT INTO candidate_fragments (
                    person_id, fragment_type, theme, claim, supporting_detail,
                    skills_json, domains_json, seniority, source_job_ids_json,
                    source_doc_paths_json, reuse_guidance, confidence, fingerprint,
                    keywords_json, anti_keywords_json, job_families_json,
                    status, confidence_reasoning, reinforces_themes_json,
                    support_count, interview_validated, last_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id, fingerprint) DO UPDATE SET
                    supporting_detail = excluded.supporting_detail,
                    skills_json = excluded.skills_json,
                    domains_json = excluded.domains_json,
                    seniority = excluded.seniority,
                    source_job_ids_json = excluded.source_job_ids_json,
                    source_doc_paths_json = excluded.source_doc_paths_json,
                    reuse_guidance = excluded.reuse_guidance,
                    keywords_json = excluded.keywords_json,
                    anti_keywords_json = excluded.anti_keywords_json,
                    job_families_json = excluded.job_families_json,
                    status = CASE
                        WHEN candidate_fragments.status = 'established' THEN 'established'
                        ELSE excluded.status
                    END,
                    confidence_reasoning = excluded.confidence_reasoning,
                    confidence = CASE
                        WHEN excluded.confidence = 'high' OR candidate_fragments.confidence = 'high' THEN 'high'
                        WHEN excluded.confidence = 'medium' OR candidate_fragments.confidence = 'medium' THEN 'medium'
                        ELSE 'low'
                    END,
                    reinforces_themes_json = excluded.reinforces_themes_json,
                    support_count = candidate_fragments.support_count + 1,
                    interview_validated = MAX(
                        COALESCE(candidate_fragments.interview_validated, 0),
                        excluded.interview_validated
                    ),
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    person_id, clean["fragment_type"], clean["theme"], clean["claim"],
                    clean["supporting_detail"], clean["skills_json"], clean["domains_json"],
                    clean["seniority"], clean["source_job_ids_json"], clean["source_doc_paths_json"],
                    clean["reuse_guidance"], clean["confidence"], fingerprint,
                    clean["keywords_json"], clean["anti_keywords_json"], clean["job_families_json"],
                    clean["status"], clean["confidence_reasoning"], clean["reinforces_themes_json"],
                    clean["support_count"], validated_flag, now, now,
                ),
            )
            count += 1
        conn.commit()
    return count


def get_candidate_fragments(person_id=1, limit=500, query=None):
    clauses = ["person_id = ?"]
    params = [person_id]
    if query:
        clauses.append("(theme LIKE ? OR claim LIKE ? OR supporting_detail LIKE ? OR skills_json LIKE ? OR domains_json LIKE ?)")
        q = f"%{query}%"
        params.extend([q] * 5)
    params.append(limit)
    with get_db_connection() as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM candidate_fragments
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def get_lane_fragments(lane_id, limit=180):
    lane = get_lane_by_id(lane_id)
    person_id = lane["person_id"] if lane and "person_id" in lane.keys() and lane["person_id"] else 1
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT candidate_fragments.*, lane_fragment_affinity.weight,
                   lane_fragment_affinity.reason, lane_fragment_affinity.source AS affinity_source
            FROM candidate_fragments
            LEFT JOIN lane_fragment_affinity
              ON lane_fragment_affinity.fragment_id = candidate_fragments.id
             AND lane_fragment_affinity.lane_id = ?
            WHERE candidate_fragments.person_id = ?
              AND COALESCE(lane_fragment_affinity.weight, 0.35) > 0
            ORDER BY COALESCE(lane_fragment_affinity.weight, 0.35) DESC,
                     candidate_fragments.updated_at DESC,
                     candidate_fragments.id DESC
            LIMIT ?
            """,
            (lane_id, person_id, limit),
        ).fetchall()
    return rows


def upsert_lane_fragment_affinity(lane_id, affinities):
    count = 0
    with get_db_connection() as conn:
        for item in affinities or []:
            fragment_id = item.get("fragment_id") or item.get("id")
            if not fragment_id:
                continue
            weight = max(0.0, min(1.0, float(item.get("weight", 0.5))))
            conn.execute(
                """
                INSERT INTO lane_fragment_affinity (lane_id, fragment_id, weight, reason, source, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(lane_id, fragment_id) DO UPDATE SET
                    weight = excluded.weight,
                    reason = excluded.reason,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    lane_id,
                    fragment_id,
                    weight,
                    _clean(item.get("reason") or ""),
                    _clean(item.get("source") or "manual") or "manual",
                ),
            )
            count += 1
        conn.commit()
    return count


def suggest_lane_fragment_affinity(lane_id, limit=80):
    lane = get_lane_by_id(lane_id)
    if not lane:
        return []
    settings = get_lane_settings(lane_id)
    haystack = " ".join(
        str(settings.get(key) or "")
        for key in ("lane_intent", "target_titles", "target_domains", "seniority", "must_have_terms", "boost_terms")
    ).lower()
    stop = {
        "and", "the", "for", "with", "role", "roles", "manager", "senior",
        "lead", "leadership", "technology", "systems", "delivery",
    }
    tokens = {token for token in re.findall(r"[a-z0-9]{3,}", haystack) if token not in stop}
    person_id = lane["person_id"] if "person_id" in lane.keys() and lane["person_id"] else 1
    suggestions = []
    for fragment in get_candidate_fragments(person_id, limit=500):
        text = " ".join(str(fragment[key] or "") for key in ("theme", "claim", "supporting_detail", "skills_json", "domains_json", "seniority")).lower()
        overlap = tokens & {token for token in re.findall(r"[a-z0-9]{3,}", text) if token not in stop}
        if overlap:
            weight = min(0.95, 0.45 + len(overlap) * 0.05)
            suggestions.append({
                "fragment_id": fragment["id"],
                "weight": weight,
                "reason": f"Matched lane terms: {', '.join(sorted(overlap)[:8])}",
                "source": "suggested",
            })
    suggestions.sort(key=lambda item: item["weight"], reverse=True)
    return suggestions[:limit]


def build_lane_context(lane_id, include_terms=True, include_fragments=True):
    lane = get_lane_by_id(lane_id)
    if not lane:
        raise ValueError(f"Lane {lane_id} was not found.")
    person = get_person_for_lane(lane_id)
    settings = get_lane_settings(lane_id)
    return {
        "person": {key: person[key] for key in person.keys()} if person else None,
        "lane": {key: lane[key] for key in lane.keys()},
        "settings": settings,
        "search_terms": get_lane_terms(lane_id) if include_terms else [],
        "fragments": [dict(row) for row in get_lane_fragments(lane_id)] if include_fragments else [],
    }


def _memory_fragment_fingerprint(fragment):
    base = "|".join(
        str(fragment.get(key) or "").strip().lower()
        for key in ("fragment_type", "theme", "claim", "supporting_detail")
    )
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _stronger_confidence(a, b):
    return a if _CONFIDENCE_RANK.get(a, 0) >= _CONFIDENCE_RANK.get(b, 0) else b


def _normalize_outcome(outcome):
    return str(outcome or "unknown").strip().lower()


_OUTCOME_WEIGHTS = {
    "interviewed": 1.0,
    "interviewing": 1.0,
    "offer": 1.5,
    "liked": 0.5,
    "applied": 0.1,
    "rejected": -0.3,
    "rejected_by_company": -0.3,
    "archived": -0.6,
    "unknown": 0.0,
    "new": 0.0,
    "interested": 0.0,
}


def upsert_profile_memory_fragments(profile_id, fragments, replace=False):
    """Persist typed fragments to `profile_memory_fragments`.

    Persists every field the LLM produces — keywords, anti_keywords,
    job_families, status, confidence_reasoning, support_count, outcomes_json,
    outcome_score, reinforces_themes_json — so the matcher actually has the
    activation metadata it needs. On conflict the support_count increments,
    outcomes merge by union, confidence keeps the strongest band, and the new
    `reinforces_themes_json` overwrites with the latest extraction's view.
    """
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    with get_db_connection() as conn:
        if replace:
            conn.execute("DELETE FROM profile_memory_fragments WHERE profile_id = ?", (profile_id,))
        for fragment in fragments or []:
            claim = str(fragment.get("claim") or "").strip()
            theme = str(fragment.get("theme") or "").strip()
            if not claim or not theme:
                continue
            clean = {
                "fragment_type": str(fragment.get("fragment_type") or "evidence").strip()[:80],
                "theme": theme[:160],
                "claim": claim[:1200],
                "supporting_detail": str(fragment.get("supporting_detail") or fragment.get("evidence") or "").strip()[:1600],
                "skills_json": _json_dumps_compact(fragment.get("skills") or fragment.get("skills_json") or []),
                "domains_json": _json_dumps_compact(fragment.get("domains") or fragment.get("domains_json") or []),
                "seniority": str(fragment.get("seniority") or "").strip()[:80],
                "source_job_ids_json": _json_dumps_compact(fragment.get("source_job_ids") or []),
                "source_doc_paths_json": _json_dumps_compact(fragment.get("source_doc_paths") or []),
                "reuse_guidance": str(fragment.get("reuse_guidance") or "").strip()[:1200],
                "confidence": str(fragment.get("confidence") or "medium").strip()[:40],
                "keywords_json": _json_dumps_compact(fragment.get("keywords") or []),
                "anti_keywords_json": _json_dumps_compact(fragment.get("anti_keywords") or []),
                "job_families_json": _json_dumps_compact(fragment.get("job_families") or []),
                "status": str(fragment.get("status") or "established").strip()[:32] or "established",
                "confidence_reasoning": str(fragment.get("confidence_reasoning") or "").strip()[:800],
                "reinforces_themes_json": _json_dumps_compact(fragment.get("reinforces_fragment_themes") or []),
                "support_count": int(fragment.get("support_count") or 1),
            }
            fingerprint = fragment.get("fingerprint") or _memory_fragment_fingerprint(clean)
            conn.execute(
                """
                INSERT INTO profile_memory_fragments (
                    profile_id, fragment_type, theme, claim, supporting_detail,
                    skills_json, domains_json, seniority, source_job_ids_json,
                    source_doc_paths_json, reuse_guidance, confidence, fingerprint,
                    keywords_json, anti_keywords_json, job_families_json,
                    status, confidence_reasoning, reinforces_themes_json,
                    support_count, last_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
                    supporting_detail = excluded.supporting_detail,
                    skills_json = excluded.skills_json,
                    domains_json = excluded.domains_json,
                    seniority = excluded.seniority,
                    source_job_ids_json = excluded.source_job_ids_json,
                    source_doc_paths_json = excluded.source_doc_paths_json,
                    reuse_guidance = excluded.reuse_guidance,
                    keywords_json = excluded.keywords_json,
                    anti_keywords_json = excluded.anti_keywords_json,
                    job_families_json = excluded.job_families_json,
                    status = CASE
                        WHEN profile_memory_fragments.status = 'established' THEN 'established'
                        ELSE excluded.status
                    END,
                    confidence_reasoning = excluded.confidence_reasoning,
                    confidence = CASE
                        WHEN excluded.confidence = 'high' OR profile_memory_fragments.confidence = 'high' THEN 'high'
                        WHEN excluded.confidence = 'medium' OR profile_memory_fragments.confidence = 'medium' THEN 'medium'
                        ELSE 'low'
                    END,
                    reinforces_themes_json = excluded.reinforces_themes_json,
                    support_count = profile_memory_fragments.support_count + 1,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    profile_id,
                    clean["fragment_type"],
                    clean["theme"],
                    clean["claim"],
                    clean["supporting_detail"],
                    clean["skills_json"],
                    clean["domains_json"],
                    clean["seniority"],
                    clean["source_job_ids_json"],
                    clean["source_doc_paths_json"],
                    clean["reuse_guidance"],
                    clean["confidence"],
                    fingerprint,
                    clean["keywords_json"],
                    clean["anti_keywords_json"],
                    clean["job_families_json"],
                    clean["status"],
                    clean["confidence_reasoning"],
                    clean["reinforces_themes_json"],
                    clean["support_count"],
                    now,
                    now,
                ),
            )
            count += 1
        conn.commit()
    return count


def record_profile_memory_scan(profile_id, applications_scanned_count, fragments_upserted_count, newest_application_date=None, summary=None):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO profile_memory_scans (
                profile_id, applications_scanned_count, fragments_upserted_count,
                newest_application_date, summary
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (profile_id, applications_scanned_count, fragments_upserted_count, newest_application_date, summary),
        )
        conn.commit()


def _outcome_weight(outcome):
    return _OUTCOME_WEIGHTS.get(_normalize_outcome(outcome), 0.0)


def record_fragment_outcomes(job_id, outcome):
    """Push an outcome onto every candidate/lane fragment used in a kit for this job.

    Called from stage transitions. Adds the outcome's signed weight to
    candidate_fragments.outcome_score, appends to outcomes_json, and stamps
    last_outcome_at. Profile-memory mirrors are updated via fingerprint match
    so both banks stay in sync.
    """
    outcome = _normalize_outcome(outcome)
    weight = _outcome_weight(outcome)
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT candidate_fragments.id, candidate_fragments.fingerprint,
                            candidate_fragments.outcomes_json, candidate_fragments.outcome_score,
                            candidate_fragments.person_id
            FROM candidate_fragments
            JOIN application_kit_fragments ON application_kit_fragments.fragment_id = candidate_fragments.id
            JOIN application_kits ON application_kits.id = application_kit_fragments.application_kit_id
            WHERE application_kits.legacy_job_id = ?
            """,
            (job_id,),
        ).fetchall()
        for row in rows:
            try:
                history = json.loads(row["outcomes_json"]) if row["outcomes_json"] else []
            except Exception:
                history = []
            history.append({"outcome": outcome, "job_id": job_id, "at": now})
            new_score = float(row["outcome_score"] or 0) + weight
            conn.execute(
                """
                UPDATE candidate_fragments
                SET outcome_score = ?,
                    outcomes_json = ?,
                    last_outcome_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_score, json.dumps(history, ensure_ascii=False, separators=(",", ":")), now, now, row["id"]),
            )
            conn.execute(
                """
                UPDATE profile_memory_fragments
                SET outcome_score = ?,
                    outcomes_json = ?,
                    last_outcome_at = ?,
                    updated_at = ?
                WHERE fingerprint = ?
                """,
                (new_score, json.dumps(history, ensure_ascii=False, separators=(",", ":")), now, now, row["fingerprint"]),
            )
        conn.commit()
    return len(rows)


def recompute_fragment_outcome_scores(profile_id=None):
    """Idempotent rebuild of fragment outcome_score from authoritative jobs.

    Walks `application_kit_fragments` joined to `jobs.pipeline_stage` and
    rebuilds each candidate fragment's outcome_score and outcomes_json from
    scratch. Use this when the schema changes or when stage hooks may have
    been missed. Safe to call repeatedly.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        lane_clause = "WHERE application_kits.lane_id = ?" if profile_id else ""
        params = (profile_id,) if profile_id else ()
        rows = conn.execute(
            f"""
            SELECT application_kit_fragments.fragment_id AS fragment_id,
                   COALESCE(jobs.pipeline_stage, 'unknown') AS stage,
                   jobs.id AS job_id,
                   jobs.last_interaction_at AS at
            FROM application_kit_fragments
            JOIN application_kits ON application_kits.id = application_kit_fragments.application_kit_id
            LEFT JOIN jobs ON jobs.id = application_kits.legacy_job_id
            {lane_clause}
            ORDER BY application_kit_fragments.fragment_id, jobs.last_interaction_at
            """,
            params,
        ).fetchall()
        by_fragment = {}
        for row in rows:
            entry = by_fragment.setdefault(row["fragment_id"], {"score": 0.0, "history": []})
            outcome = _normalize_outcome(row["stage"])
            entry["score"] += _outcome_weight(outcome)
            entry["history"].append({"outcome": outcome, "job_id": row["job_id"], "at": row["at"]})
        for fragment_id, agg in by_fragment.items():
            fp_row = conn.execute(
                "SELECT fingerprint FROM candidate_fragments WHERE id = ?",
                (fragment_id,),
            ).fetchone()
            fingerprint = fp_row["fingerprint"] if fp_row else None
            history_json = json.dumps(agg["history"], ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                UPDATE candidate_fragments
                SET outcome_score = ?, outcomes_json = ?, last_outcome_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (agg["score"], history_json, now, now, fragment_id),
            )
            if fingerprint:
                conn.execute(
                    """
                    UPDATE profile_memory_fragments
                    SET outcome_score = ?, outcomes_json = ?, last_outcome_at = ?, updated_at = ?
                    WHERE fingerprint = ?
                    """,
                    (agg["score"], history_json, now, now, fingerprint),
                )
        if profile_id:
            conn.execute(
                """
                INSERT INTO profile_memory_remine_schedule (profile_id, last_outcome_recompute_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    last_outcome_recompute_at = excluded.last_outcome_recompute_at,
                    updated_at = excluded.updated_at
                """,
                (profile_id, now, now),
            )
        conn.commit()
    return len(by_fragment)


def mark_memory_remine_complete(profile_id, cadence_days=None):
    """Stamp last_remine_at and compute next_due_at using the lane's cadence."""
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT cadence_days FROM profile_memory_remine_schedule WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        cadence = int(cadence_days or (row["cadence_days"] if row else 7) or 7)
        next_due = (now + timedelta(days=cadence)).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO profile_memory_remine_schedule (
                profile_id, cadence_days, last_remine_at, next_due_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                cadence_days = excluded.cadence_days,
                last_remine_at = excluded.last_remine_at,
                next_due_at = excluded.next_due_at,
                updated_at = excluded.updated_at
            """,
            (profile_id, cadence, now_iso, next_due, now_iso),
        )
        conn.commit()
    return next_due


def due_memory_remines(now=None):
    """Return profile_ids whose next_due_at has passed (or never been set)."""
    now_iso = (now or datetime.now()).isoformat(timespec="seconds")
    with get_db_connection() as conn:
        # Profiles with no schedule row are treated as due — first run.
        rows = conn.execute(
            """
            SELECT profiles.id AS profile_id
            FROM profiles
            LEFT JOIN profile_memory_remine_schedule
              ON profile_memory_remine_schedule.profile_id = profiles.id
            WHERE profile_memory_remine_schedule.profile_id IS NULL
               OR profile_memory_remine_schedule.next_due_at IS NULL
               OR profile_memory_remine_schedule.next_due_at <= ?
            """,
            (now_iso,),
        ).fetchall()
        return [row["profile_id"] for row in rows]


_interview_mining_lock = threading.Lock()


_interview_mining_inflight = set()


def _gather_interview_mining_documents(job_id):
    from .jobs import get_application_kits, get_job_details
    # Imported here rather than at module scope: _gather_interview_mining_documents needs a
    # module that imports this one back.
    job = get_job_details(job_id)
    if not job:
        return None, None, []
    job = dict(job)
    documents = []
    jd_parts = [job.get("title"), job.get("description"),
                job.get("position_description_text") or job.get("pdf_text")]
    jd_text = "\n\n".join(str(p) for p in jd_parts if p)
    if jd_text.strip():
        documents.append({"filename": f"job-{job_id}-description", "text": jd_text})
    have_candidate_docs = False
    for label, value in (("resume", job.get("resume_text")), ("cover-letter", job.get("cover_letter_text"))):
        if value and str(value).strip():
            documents.append({"filename": f"job-{job_id}-{label}", "text": str(value)})
            have_candidate_docs = True
    for kit in get_application_kits(job_id=job_id, limit=5) or []:
        kit = dict(kit)
        for label, value in (("kit-resume", kit.get("resume_text")), ("kit-cover", kit.get("cover_letter_text"))):
            if value and str(value).strip():
                documents.append({"filename": f"job-{job_id}-{label}-{kit.get('id')}", "text": str(value)})
                have_candidate_docs = True
    # Fallback for interviews logged without in-app document generation (the
    # common case for historical roles): there is no submitted resume/cover to
    # mine, only the job ad — which is not candidate evidence. Pull the most
    # JD-relevant documents from the candidate's evidence corpus so the miner
    # has real, role-relevant candidate material to extract interview-validated
    # fragments from. Best-effort: if the corpus is empty this is a no-op.
    if not have_candidate_docs and jd_text.strip():
        documents.extend(_corpus_evidence_for_jd(jd_text, job_id))
    return job, get_lane_settings(job.get("profile_id") or 1), documents


def _corpus_evidence_for_jd(jd_text, job_id, limit=6):
    """Retrieve the candidate's most JD-relevant corpus evidence (resumes, cover
    letters, KSC responses) as mining documents. Uses the same DB the rest of the
    process reads, so it works under test isolation and the packaged data dir."""
    try:
        import context_library as clib
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        try:
            picked, _family = clib.retrieve(conn, jd_text)
        finally:
            conn.close()
    except Exception as exc:
        print(f"Corpus evidence fallback failed for job {job_id}: {exc}")
        return []
    documents = []
    for _doc_type, items in (picked or {}).items():
        for _score, doc in items:
            text = str(doc.get("text") or "").strip()
            if text:
                documents.append({"filename": f"corpus-{doc.get('filename') or 'evidence'}", "text": text})
            if len(documents) >= limit:
                return documents
    return documents


def boost_interview_validated_affinity(lane_id, person_id):
    """Raise lane affinity for every interview-validated fragment so it sorts
    above ordinary evidence in get_lane_fragments / document generation."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM candidate_fragments WHERE person_id = ? AND COALESCE(interview_validated, 0) = 1",
            (person_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO lane_fragment_affinity (lane_id, fragment_id, weight, reason, source, updated_at)
                VALUES (?, ?, 0.97, 'Mined from a job that reached an interview.', 'interview_validated', datetime('now'))
                ON CONFLICT(lane_id, fragment_id) DO UPDATE SET
                    weight = MAX(lane_fragment_affinity.weight, 0.97),
                    reason = 'Mined from a job that reached an interview.',
                    source = 'interview_validated',
                    updated_at = datetime('now')
                """,
                (lane_id, row["id"]),
            )
        conn.commit()
        return len(rows)


def mine_interview_validated_fragments(job_id, log=print):
    """Mine interview-validated fragments from a job's JD + submitted documents.
    Best-effort: returns 0 (and logs) if no provider is available or no docs."""
    job, settings, documents = _gather_interview_mining_documents(job_id)
    if not job or not documents:
        log(f"No documents available to mine for interview-validated fragments (job {job_id}).")
        return 0
    lane_id = job.get("profile_id") or 1
    person_id = 1
    lane = get_lane_by_id(lane_id)
    if lane and "person_id" in lane.keys() and lane["person_id"]:
        person_id = lane["person_id"]
    import corpus_miner
    fragments, label = corpus_miner.mine_documents(documents, settings, log=log)
    for fragment in fragments:
        if isinstance(fragment, dict):
            fragment.setdefault("source_job_ids", [job_id])
    stored = upsert_candidate_fragments(person_id, fragments, interview_validated=True)
    boosted = boost_interview_validated_affinity(lane_id, person_id)
    keywords = []
    for fragment in fragments:
        keywords.extend(fragment.get("keywords") or [])
    if keywords:
        try:
            merge_lane_terms(lane_id, keywords, source="interview_validated", confidence=0.92)
        except Exception as exc:
            log(f"Interview-validated keyword merge failed: {exc}")
    log(f"Interview-validated mining ({label}): stored {stored} fragments, boosted {boosted} affinities for job {job_id}.")
    return stored


def _schedule_interview_fragment_mining(job_id):
    """Kick interview-validated mining onto a daemon thread so interview
    creation never blocks on the LLM. De-duped per job; failures are swallowed
    (recompute paths reconcile fragments anyway)."""
    with _interview_mining_lock:
        if job_id in _interview_mining_inflight:
            return
        _interview_mining_inflight.add(job_id)

    def _run():
        try:
            mine_interview_validated_fragments(job_id, log=lambda m: print(f"[interview-mining] {m}"))
        except Exception as exc:
            print(f"Interview-validated mining failed for job {job_id}: {exc}")
        finally:
            with _interview_mining_lock:
                _interview_mining_inflight.discard(job_id)

    threading.Thread(target=_run, name=f"interview-mine-{job_id}", daemon=True).start()


def merge_lane_terms(lane_id, keywords, source="memory_evolution", confidence=0.78, protected_sources=("manual", "interview_validated")):
    """Insert/update lane terms without clobbering manual or validated entries.

    The original save_lane_terms wipes provenance by setting ALL rows for the
    lane to the new source/confidence — that destroys signal from manual
    additions and interview-validated terms. This helper only touches rows
    we're actually writing.
    """
    if not keywords:
        return 0
    inserted = 0
    with get_db_connection() as conn:
        for term in keywords:
            term = str(term or "").strip()
            if not term:
                continue
            existing = conn.execute(
                "SELECT source FROM lane_terms WHERE lane_id = ? AND term = ?",
                (lane_id, term),
            ).fetchone()
            if existing and existing["source"] in protected_sources:
                continue
            conn.execute(
                """
                INSERT INTO lane_terms (lane_id, term, source, confidence, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(lane_id, term) DO UPDATE SET
                    source = excluded.source,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (lane_id, term, source, float(confidence)),
            )
            inserted += 1
        conn.commit()
    return inserted


def get_profile_memory_fragments(profile_id, limit=500):
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM profile_memory_fragments
            WHERE profile_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()


def get_profile_memory_status(profile_id, recent_days=7):
    with get_db_connection() as conn:
        last_scan = conn.execute(
            """
            SELECT *
            FROM profile_memory_scans
            WHERE profile_id = ?
            ORDER BY scanned_at DESC, id DESC
            LIMIT 1
            """,
            (profile_id,),
        ).fetchone()
        fragment_count = conn.execute(
            "SELECT COUNT(*) AS count FROM profile_memory_fragments WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()["count"]
        since = (datetime.now() - timedelta(days=recent_days)).isoformat(timespec="seconds")
        params = [profile_id, since]
        clause = """
            jobs.profile_id = ?
            AND application_events.event_type = 'documents'
            AND COALESCE(application_events.event_date, application_events.created_at) >= ?
        """
        if last_scan:
            clause += " AND COALESCE(application_events.event_date, application_events.created_at) > ?"
            params.append(last_scan["scanned_at"])
        recent_unscanned = conn.execute(
            f"""
            SELECT COUNT(DISTINCT jobs.id) AS count
            FROM jobs
            JOIN application_events ON application_events.job_id = jobs.id
            WHERE {clause}
            """,
            params,
        ).fetchone()["count"]
    return {
        "last_scan": {key: last_scan[key] for key in last_scan.keys()} if last_scan else None,
        "fragment_count": fragment_count,
        "recent_unscanned_count": recent_unscanned,
        "recent_days": recent_days,
        "reminder_threshold": 6,
    }


def get_generated_application_sources(profile_id, recent_days=None, limit=30):
    params = [profile_id]
    date_clause = ""
    if recent_days:
        date_clause = "AND COALESCE(application_events.event_date, application_events.created_at) >= ?"
        params.append((datetime.now() - timedelta(days=recent_days)).isoformat(timespec="seconds"))
    params.append(limit)
    query = f"""
        SELECT
            jobs.*,
            application_events.details AS document_details,
            COALESCE(application_events.event_date, application_events.created_at, jobs.updated_at, jobs.last_interaction_at) AS generated_at
        FROM jobs
        JOIN application_events ON application_events.job_id = jobs.id
        WHERE jobs.profile_id = ?
        AND application_events.event_type = 'documents'
        {date_clause}
        ORDER BY generated_at DESC, jobs.id DESC
        LIMIT ?
    """
    with get_db_connection() as conn:
        return conn.execute(query, params).fetchall()


def get_resume_triage_cache(profile_id, resume_hash):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT resume_triage_summary, resume_triage_hash FROM profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
    if row and row["resume_triage_hash"] == resume_hash and row["resume_triage_summary"]:
        return row["resume_triage_summary"]
    return None


def save_resume_triage_cache(profile_id, resume_hash, summary):
    with get_db_connection() as conn:
        _execute_with_retry(
            conn,
            "UPDATE profiles SET resume_triage_summary = ?, resume_triage_hash = ? WHERE id = ?",
            (summary, resume_hash, profile_id),
            is_commit=True,
        )


def delete_profile(profile_id):
    """Deletes a lane and everything scoped to it.

    Lane-scoped tables (profile_terms, lane_terms, lane_opportunities,
    application_kits, hidden_market_*, etc.) declare ON DELETE CASCADE (or
    SET NULL for run-history tables) against profiles(id), but SQLite only
    enforces that when foreign_keys is turned on for the connection. The
    legacy `jobs` table predates that constraint - its columns were added via
    ALTER over time - so it has no FK at all and is cleared explicitly.

    Jobs that carry real application history (an interview, a stage/interview
    event, or a post-applied status) are NOT deleted — that is how we lost
    jobs 12344/22508 to the old cascade. They are reassigned to a surviving
    fallback lane so their interviews and outcome snapshots stay reachable.
    `application_outcomes` has no FK to jobs, so those rows survive regardless.
    """
    from .jobs import sync_legacy_job_to_lane_model
    # Imported here rather than at module scope: delete_profile needs a
    # module that imports this one back.
    with get_db_connection() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        fallback = conn.execute(
            "SELECT id FROM profiles WHERE id != ? ORDER BY id ASC LIMIT 1",
            (profile_id,),
        ).fetchone()
        fallback_id = fallback["id"] if fallback else None
        reassigned = []
        if fallback_id:
            history_rows = conn.execute(
                """
                SELECT id FROM jobs
                WHERE profile_id = ?
                  AND (
                        pipeline_stage IN ('applied','interviewing','offer','rejected_by_company')
                     OR status IN ('applied','interviewing','offer','rejected_by_company')
                     OR EXISTS (SELECT 1 FROM interviews WHERE interviews.job_id = jobs.id)
                     OR EXISTS (
                            SELECT 1 FROM application_events
                            WHERE application_events.job_id = jobs.id
                              AND application_events.event_type IN ('stage','interview')
                        )
                  )
                """,
                (profile_id,),
            ).fetchall()
            reassigned = [row["id"] for row in history_rows]
            if reassigned:
                placeholders = ",".join("?" for _ in reassigned)
                conn.execute(
                    f"""
                    UPDATE jobs
                    SET profile_id = ?, updated_at = datetime('now')
                    WHERE id IN ({placeholders})
                    """,
                    [fallback_id, *reassigned],
                )
        conn.execute("DELETE FROM jobs WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
    # Re-home the lane-model rows for reassigned jobs so they show under the
    # fallback lane's pipeline (the deleted lane's lane_opportunities cascaded).
    for job_id in reassigned:
        try:
            sync_legacy_job_to_lane_model(job_id, fallback_id)
        except Exception as exc:
            print(f"Lane re-home sync failed for job {job_id}: {exc}")


def get_profile_terms(profile_id):
    """Returns search terms for a specific profile."""
    query = "SELECT keyword FROM profile_terms WHERE profile_id = ? ORDER BY created_at ASC"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, (profile_id,))
        return [row['keyword'] for row in cursor.fetchall()]


def get_lane_terms(lane_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT term FROM lane_terms
            WHERE lane_id = ?
            ORDER BY performance_score DESC, confidence DESC, created_at ASC
            """,
            (lane_id,),
        ).fetchall()
        terms = [row["term"] for row in rows]
    return terms or get_profile_terms(lane_id)


def save_profile_terms(profile_id, keywords):
    """Replaces all existing terms for a profile with a new list."""
    deduped = []
    seen = set()
    for keyword in keywords or []:
        clean = _clean(keyword)
        key = clean.casefold()
        if clean and key not in seen:
            deduped.append(clean)
            seen.add(key)
    with get_db_connection() as conn:
        conn.execute("DELETE FROM profile_terms WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM lane_terms WHERE lane_id = ?", (profile_id,))
        if deduped:
            conn.executemany(
                "INSERT INTO profile_terms (profile_id, keyword) VALUES (?, ?)",
                [(profile_id, kw) for kw in deduped]
            )
            conn.executemany(
                "INSERT OR IGNORE INTO lane_terms (lane_id, term, source, confidence) VALUES (?, ?, ?, ?)",
                [(profile_id, kw, "generated", 0.75) for kw in deduped]
            )
        conn.commit()


def save_lane_terms(lane_id, keywords, source="generated", confidence=0.75):
    save_profile_terms(lane_id, keywords)
    with get_db_connection() as conn:
        conn.execute("UPDATE lane_terms SET source = ?, confidence = ? WHERE lane_id = ?", (source, confidence, lane_id))
        conn.commit()


def _upsert_lane_opportunity_from_row(conn, row, posting_id, lane_id=None):
    from .jobs import normalize_stage
    # Imported here rather than at module scope: _upsert_lane_opportunity_from_row needs a
    # module that imports this one back.
    lane_id = lane_id or row["profile_id"] or 1
    same_legacy_lane = int(lane_id) == int(row["profile_id"] or 0)
    # legacy_job_id points back to the single-lane jobs row and is UNIQUE for
    # backwards compatibility.  A deduped posting may legitimately appear in
    # several lanes, so only its original lane can own that legacy pointer;
    # every lane is still linked through the shared job_posting_id.
    if same_legacy_lane:
        existing_legacy = conn.execute(
            "SELECT id FROM lane_opportunities WHERE legacy_job_id = ? AND legacy_job_id IS NOT NULL",
            (row["id"],),
        ).fetchone()
        legacy_job_id = None if existing_legacy else row["id"]
    else:
        legacy_job_id = None
    conn.execute(
        """
        INSERT INTO lane_opportunities (
            legacy_job_id, lane_id, job_posting_id, pipeline_stage, status, match_score,
            ai_analysis, analysis_signature, priority, notes, next_action, next_action_date,
            application_date, feedback, retired_reason, discovered_at, last_interaction_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), COALESCE(?, datetime('now')), COALESCE(?, datetime('now')))
        ON CONFLICT(lane_id, job_posting_id) DO UPDATE SET
            legacy_job_id = COALESCE(lane_opportunities.legacy_job_id, excluded.legacy_job_id),
            pipeline_stage = excluded.pipeline_stage,
            status = excluded.status,
            match_score = excluded.match_score,
            ai_analysis = excluded.ai_analysis,
            analysis_signature = excluded.analysis_signature,
            priority = excluded.priority,
            notes = excluded.notes,
            next_action = excluded.next_action,
            next_action_date = excluded.next_action_date,
            application_date = excluded.application_date,
            feedback = excluded.feedback,
            retired_reason = excluded.retired_reason,
            last_interaction_at = excluded.last_interaction_at,
            updated_at = excluded.updated_at
        """,
        (
            legacy_job_id, lane_id, posting_id, normalize_stage(row["pipeline_stage"] or row["status"]),
            normalize_stage(row["status"] or row["pipeline_stage"]),
            row["match_score"] if same_legacy_lane else None,
            row["ai_analysis"] if same_legacy_lane else None,
            row["analysis_signature"] if same_legacy_lane else None,
            row["priority"] or "normal",
            row["notes"], row["next_action"], row["next_action_date"], row["application_date"],
            row["feedback"], row["retired_reason"], row["date_scraped"],
            row["last_interaction_at"], row["updated_at"],
        ),
    )
    return conn.execute(
        "SELECT id FROM lane_opportunities WHERE lane_id = ? AND job_posting_id = ?",
        (lane_id, posting_id),
    ).fetchone()["id"]


def refresh_lane_learning_metrics(lane_id=None):
    """Refresh simple term and fragment performance signals from stored outcomes."""
    params = []
    lane_clause = ""
    if lane_id:
        lane_clause = "AND search_hits.lane_id = ?"
        params.append(lane_id)
    with get_db_connection() as conn:
        term_rows = conn.execute(
            f"""
            SELECT
                search_hits.lane_id,
                search_hits.keyword AS term,
                SUM(CASE lane_opportunities.pipeline_stage
                    WHEN 'interested' THEN 3
                    WHEN 'applied' THEN 5
                    WHEN 'interviewing' THEN 8
                    WHEN 'offer' THEN 13
                    WHEN 'rejected' THEN -1
                    WHEN 'archived' THEN -1
                    ELSE 0
                END) AS score
            FROM search_hits
            JOIN lane_opportunities
              ON lane_opportunities.lane_id = search_hits.lane_id
             AND lane_opportunities.job_posting_id = search_hits.job_posting_id
            WHERE NULLIF(search_hits.keyword, '') IS NOT NULL
            {lane_clause}
            GROUP BY search_hits.lane_id, search_hits.keyword
            """,
            params,
        ).fetchall()
        for row in term_rows:
            conn.execute(
                """
                INSERT INTO lane_terms (lane_id, term, source, confidence, performance_score, updated_at)
                VALUES (?, ?, 'learned', 0.7, ?, datetime('now'))
                ON CONFLICT(lane_id, term) DO UPDATE SET
                    performance_score = excluded.performance_score,
                    updated_at = excluded.updated_at
                """,
                (row["lane_id"], row["term"], row["score"] or 0),
            )

        affinity_params = []
        affinity_lane_clause = ""
        if lane_id:
            affinity_lane_clause = "AND application_kits.lane_id = ?"
            affinity_params.append(lane_id)
        fragment_rows = conn.execute(
            f"""
            SELECT
                application_kits.lane_id,
                application_kit_fragments.fragment_id,
                COUNT(*) AS uses,
                SUM(CASE COALESCE(application_kits.outcome, '')
                    WHEN 'interviewing' THEN 3
                    WHEN 'offer' THEN 5
                    WHEN 'applied' THEN 2
                    ELSE 1
                END) AS signal
            FROM application_kit_fragments
            JOIN application_kits ON application_kits.id = application_kit_fragments.application_kit_id
            WHERE 1 = 1
            {affinity_lane_clause}
            GROUP BY application_kits.lane_id, application_kit_fragments.fragment_id
            """,
            affinity_params,
        ).fetchall()
        for row in fragment_rows:
            weight = min(1.0, 0.55 + float(row["signal"] or 0) * 0.04)
            conn.execute(
                """
                INSERT INTO lane_fragment_affinity (lane_id, fragment_id, weight, reason, source, updated_at)
                VALUES (?, ?, ?, ?, 'learned', datetime('now'))
                ON CONFLICT(lane_id, fragment_id) DO UPDATE SET
                    weight = MAX(lane_fragment_affinity.weight, excluded.weight),
                    reason = excluded.reason,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    row["lane_id"],
                    row["fragment_id"],
                    weight,
                    f"Used in {row['uses']} application kit(s); signal score {row['signal'] or 0}.",
                ),
            )
        conn.commit()
    return {"terms_updated": len(term_rows), "fragment_affinities_updated": len(fragment_rows)}


def _profile_filter_clause(profile_id=None, include_all_profiles=False, alias="jobs"):
    if include_all_profiles or not profile_id:
        return "", []
    return f" AND {alias}.profile_id = ?", [profile_id]


def get_interview_validated_fragments(person_id=1, limit=300):
    """Candidate fragments mined from jobs that reached an interview (item 5).

    Ordered strongest-first (outcome-weighted, then repeatedly-reinforced) so the
    Learnings tab leads with the evidence most correlated with getting interviews.
    """
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM candidate_fragments
            WHERE person_id = ? AND COALESCE(interview_validated, 0) = 1
            ORDER BY outcome_score DESC, support_count DESC, updated_at DESC
            LIMIT ?
            """,
            (person_id, limit),
        ).fetchall()


def _sync_lane_opportunity_for_job(conn, job_id, updates):
    allowed = {"pipeline_stage", "status", "priority", "next_action", "next_action_date", "application_date", "feedback", "notes"}
    values = {key: value for key, value in updates.items() if key in allowed}
    if not values:
        return
    values["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if "pipeline_stage" in values and "status" not in values:
        values["status"] = values["pipeline_stage"]
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE lane_opportunities SET {assignments} WHERE legacy_job_id = ?",
        list(values.values()) + [job_id],
    )
