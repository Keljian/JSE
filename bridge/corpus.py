"""Context-library and memory-fragment commands.

Split out of python_bridge.py, which re-exports everything here.
"""
import contextlib
import re
import sys

import database_manager as db
import concurrency
from .runtime import (
    _clean_text,
    _tokenize_for_match,
    emit,
    older_applications_dir,
)
from .documents import (
    _load_saved_application_payload,
    _saved_application_document_sources,
)
from .lanes import (
    _evolve_profile_terms_from_memory,
    _person_id_for,
)

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


def _corpus_conn():
    import context_library as clib
    import sqlite3 as _sql
    conn = _sql.connect(str(clib.DB_PATH)); conn.row_factory = _sql.Row
    clib.ensure_schema(conn)
    return conn, clib


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


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "memory:status": command_memory_status,
    "memory:scan": command_memory_scan,
    "memory:remineDue": command_memory_remine_due,
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
