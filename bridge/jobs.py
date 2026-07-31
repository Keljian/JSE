"""Job commands: listing, updates, flags, and shortlist export.

Split out of python_bridge.py, which re-exports everything here.
"""
import json
from pathlib import Path

import database_manager as db
from .runtime import (
    _ad_signals_cache,
    _clean_text,
    compact_job_dict,
    emit,
    import_app_logic,
    row_to_dict,
    rows_to_dicts,
    shortlists_dir,
)
from .documents import (
    read_resume_text,
)

def command_jobs_list(payload):
    import ad_signals
    rows = db.get_pipeline_jobs(payload)
    recurrence = db.recurrence_index(payload.get("profile_id"), bool(payload.get("include_all_profiles")))
    warm_index = db.warm_contact_index(payload.get("profile_id"), bool(payload.get("include_all_profiles")))
    compact = payload.get("compact")
    jobs = []
    for row in rows:
        data = compact_job_dict(row, extra_fields=("ad_text",)) if compact else row_to_dict(row)
        key = db.recurrence_key(data.get("company"), data.get("title"))
        data["ad_signals"] = ad_signals.derive(data, recurrence.get(key, 1), cache=_ad_signals_cache)
        data.pop("ad_text", None)
        jobs.append(data)
    db.annotate_channel_warmth(jobs, warm_index)
    # Warmth outranks the score but not the user's own priority or a due date:
    # an overdue action still has to come first on the board.
    jobs.sort(key=lambda job: (
        {"high": 0, "normal": 1, "low": 2}.get(job.get("priority") or "normal", 1),
        str(job.get("next_action_date") or "9999-12-31"),
        -int(job.get("warmth") or 0),
        -int(job.get("composite_score") or job.get("match_score") or 0),
        -int(job.get("id") or 0),
    ))
    return {"jobs": jobs}


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


def command_jobs_reset_rejected(payload):
    profile_id = payload.get("profile_id")
    count = db.reset_rejected_to_new(profile_id=profile_id)
    return {"count": count}


def command_jobs_move_profile(payload):
    job = db.move_job_to_profile(int(payload["job_id"]), int(payload["profile_id"]))
    return {
        "job": row_to_dict(job),
        "events": rows_to_dicts(db.get_job_events(payload["job_id"])),
        "interviews": rows_to_dicts(db.get_interviews(payload["job_id"])),
    }


def _job_has_researched_company_intel(job):
    if not job:
        return False
    try:
        intelligence = json.loads(job["company_intelligence"] or "{}")
    except (TypeError, json.JSONDecodeError):
        intelligence = {}
    return bool(intelligence.get("ai_research") or intelligence.get("cached_company_profile"))


def command_jobs_add_flag(payload):
    """Add a flag by hand. Marked manual, so re-analysis will not erase it."""
    job_id = payload["job_id"]
    record = db.add_job_flag(
        job_id,
        payload.get("type"),
        payload.get("requirement"),
        payload.get("detail") or "",
        payload.get("confidence") or "high",
    )
    db.add_application_event(
        job_id, "note", "Flag added",
        f"{payload.get('type')}: {payload.get('requirement')}",
    )
    return {"job_id": job_id, "flags": record}


def command_jobs_dismiss_flag(payload):
    """Remove a single flag. Nothing depended on it, so nothing else changes."""
    job_id = payload["job_id"]
    record = db.dismiss_job_flag(job_id, payload.get("requirement"))
    db.add_application_event(
        job_id, "note", "Flag dismissed", str(payload.get("requirement") or ""),
    )
    return {"job_id": job_id, "flags": record}


def command_jobs_clear_flags(payload):
    """Drop every flag on a job, manual ones included."""
    job_id = payload["job_id"]
    record = db.clear_job_flags(job_id)
    db.add_application_event(job_id, "note", "Flags cleared", "All flags removed.")
    return {"job_id": job_id, "flags": record}


SHORTLIST_DEFAULT_STAGES = ("new", "interested")


def _shortlist_entry(job, warm_index):
    """One survivor, flattened to what a go/no-go decision actually needs."""
    flag_record = db.get_job_flags(job["id"]) or {}
    warm_path = db.warm_path_for_job(job, warm_index)
    return {
        "id": job["id"],
        "title": job.get("title"),
        "company": job.get("actual_company") or job.get("company"),
        "advertiser": job.get("advertiser_company"),
        "employer_type": job.get("employer_type"),
        "location": job.get("location"),
        "salary": job.get("salary"),
        "source": job.get("source"),
        "url": job.get("url"),
        "closing_date": job.get("closing_date"),
        "pipeline_stage": job.get("pipeline_stage"),
        "match_score": job.get("match_score"),
        "fragment_score": job.get("fragment_score"),
        "composite_score": job.get("composite_score"),
        "channel": job.get("channel"),
        "channel_label": job.get("channel_label"),
        "warmth": job.get("warmth"),
        "warmth_label": job.get("warmth_label"),
        "warm_path": [
            {"name": c.get("name"), "role_title": c.get("role_title"), "relationship": c.get("relationship")}
            for c in warm_path[:5]
        ],
        "flags": flag_record.get("flags") or [],
        "flag_summary": flag_record.get("summary") or "",
        "seniority_direction": flag_record.get("seniority_direction") or "unknown",
        "analysis": job.get("ai_analysis") or "",
        "description": job.get("description") or "",
        "position_description_text": job.get("position_description_text") or "",
    }


def _shortlist_markdown(entries, generated_at, window_label):
    """The packet as one readable document.

    Deliberately one file rather than one per job: the handoff this replaces is
    a single review pass over the whole sweep, and splitting it would recreate
    the per-role copy-paste it exists to remove.
    """
    lines = [
        f"# JSE shortlist — {generated_at}",
        "",
        f"{len(entries)} role{'' if len(entries) == 1 else 's'} surviving triage{window_label}.",
        "",
        "## Index",
        "",
    ]
    for index, entry in enumerate(entries, start=1):
        chips = [entry["warmth_label"]]
        if entry["flags"]:
            chips.append(f"{len(entry['flags'])} flag{'' if len(entry['flags']) == 1 else 's'}")
        lines.append(
            f"{index}. **{entry['title']}** — {entry['company'] or 'Unknown company'} · "
            f"score {entry['composite_score'] if entry['composite_score'] is not None else entry['match_score']} · "
            f"{' · '.join(chips)}"
        )
    lines.append("")

    for index, entry in enumerate(entries, start=1):
        lines.extend([
            "---",
            "",
            f"## {index}. {entry['title']}",
            "",
            f"- **Employer:** {entry['company'] or 'Unknown'}"
            + (f" (advertised by {entry['advertiser']})" if entry.get("advertiser") else ""),
            f"- **Location:** {entry.get('location') or 'Not stated'}",
            f"- **Salary:** {entry.get('salary') or 'Not disclosed'}",
            f"- **Source:** {entry.get('source') or 'Unknown'} · **Stage:** {entry.get('pipeline_stage') or 'new'}",
            f"- **Closes:** {entry.get('closing_date') or 'Not stated'}",
            f"- **Scores:** match {entry.get('match_score')} · fragment {entry.get('fragment_score')} · composite {entry.get('composite_score')}",
            f"- **Channel:** {entry.get('channel_label') or 'Unattributed'} ({entry.get('warmth_label')})",
            f"- **URL:** {entry.get('url') or 'None'}",
            "",
        ])
        if entry["warm_path"]:
            described = [
                f"{c['name']}" + (f" ({c['role_title']})" if c.get("role_title") else "")
                for c in entry["warm_path"] if c.get("name")
            ]
            lines.extend([f"**Warm path:** {', '.join(described)}", ""])
        if entry["flag_summary"]:
            lines.extend([f"**Flags:** {entry['flag_summary']}", ""])
        if entry["flags"]:
            for item in entry["flags"]:
                detail = f" — {item.get('detail')}" if item.get("detail") else ""
                lines.append(
                    f"- **{item.get('label') or item.get('type')}** "
                    f"({item.get('confidence')} confidence): {item.get('requirement')}{detail}"
                )
            lines.append("")
        if entry["analysis"]:
            lines.extend(["**Analysis:**", "", "```", entry["analysis"].strip(), "```", ""])
        if entry["position_description_text"]:
            lines.extend(["<details><summary>Position description</summary>", "",
                          entry["position_description_text"].strip(), "", "</details>", ""])
        if entry["description"]:
            lines.extend(["<details><summary>Job advertisement</summary>", "",
                          entry["description"].strip(), "", "</details>", ""])
    return "\n".join(lines).rstrip() + "\n"


def command_jobs_export_shortlist(payload):
    """Write one triage packet for the survivors of a sweep.

    The daily loop is: JSE sweeps overnight, then the survivors get a human
    go/no-go and positioning pass elsewhere. That handoff was manual copying,
    so this emits the whole shortlist — ad text, metadata, scores with the
    stored analysis, gate verdict, warm-path hits — as one file in a folder
    that can be watched.
    """
    profile_id = payload.get("profile_id", 1)
    include_all = bool(payload.get("include_all_profiles"))
    limit = max(1, min(200, int(payload.get("limit") or 40)))
    min_score = payload.get("min_score")
    stages = [db.normalize_stage(stage) for stage in (payload.get("stages") or SHORTLIST_DEFAULT_STAGES)]
    fmt = str(payload.get("format") or "both").lower()
    # Nothing is excluded by default: the packet exists so a human can make the
    # call, and pre-filtering it would make that call on their behalf. Callers
    # may narrow it explicitly by flag type.
    exclude_flags = {str(value).strip().lower() for value in (payload.get("exclude_flags") or []) if value}

    filters = {
        "profile_id": profile_id,
        "include_all_profiles": include_all,
    }
    if min_score not in (None, ""):
        filters["min_score"] = int(min_score)
    rows = db.get_pipeline_jobs(filters)
    warm_index = db.warm_contact_index(profile_id, include_all)

    jobs = [row_to_dict(row) for row in rows]
    jobs = [job for job in jobs if db.normalize_stage(job.get("pipeline_stage") or job.get("status")) in stages]
    db.annotate_channel_warmth(jobs, warm_index)
    if exclude_flags:
        jobs = [
            job for job in jobs
            if not (exclude_flags & {
                part for part in str(job.get("job_flags_types") or "").split(",") if part
            })
        ]
    jobs.sort(key=lambda job: (
        -int(job.get("warmth") or 0),
        -int(job.get("composite_score") or job.get("match_score") or 0),
        str(job.get("closing_date") or "9999-12-31"),
    ))
    jobs = jobs[:limit]

    from datetime import datetime

    entries = [_shortlist_entry(job, warm_index) for job in jobs]
    now = datetime.now()
    generated_at = now.isoformat(timespec="seconds")
    stamp = now.strftime("%Y-%m-%d_%H%M")
    window_label = f" for {db.get_lane_settings(profile_id).get('name') or 'lane'}" if not include_all else ""

    folder = Path(payload["output_dir"]) if payload.get("output_dir") else shortlists_dir()
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    if fmt in ("markdown", "both"):
        markdown_path = folder / f"shortlist_{stamp}.md"
        markdown_path.write_text(_shortlist_markdown(entries, generated_at, window_label), encoding="utf-8")
        written.append(str(markdown_path))
    if fmt in ("json", "both"):
        json_path = folder / f"shortlist_{stamp}.json"
        json_path.write_text(
            json.dumps(
                {"generated_at": generated_at, "profile_id": profile_id,
                 "include_all_profiles": include_all, "count": len(entries), "jobs": entries},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        written.append(str(json_path))

    return {
        "generated_at": generated_at,
        "count": len(entries),
        "folder": str(folder),
        "files": written,
        "job_ids": [entry["id"] for entry in entries],
    }


def command_jobs_set_channel(payload):
    """Set or clear how this application reaches the employer."""
    job_id = payload["job_id"]
    channel = db.set_job_channel(job_id, payload.get("channel"))
    db.add_application_event(
        job_id, "note", "Application channel set",
        db.CHANNEL_LABELS.get(channel, "Cleared to derived"),
    )
    return {"job_id": job_id, "channel": channel, "job": row_to_dict(db.get_job_details(job_id))}


def command_jobs_detail(payload):
    import ad_signals
    job = db.get_job_details(payload["job_id"])
    if job and db.company_intelligence_needs_refresh(job):
        job = db.refresh_job_company_intelligence(payload["job_id"])
    job_dict = row_to_dict(job)
    if job_dict:
        job_dict["ad_signals"] = ad_signals.derive(job_dict, db.recurrence_count_for(job_dict))
        job_dict["job_flags"] = db.get_job_flags(payload["job_id"])
        job_dict["document_track_resolved"] = db.resolve_document_track(payload["job_id"])
        db.annotate_channel_warmth(
            [job_dict], db.warm_contact_index(job_dict.get("profile_id"))
        )
    return {
        "job": job_dict,
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


def report_job_flags(job):
    """Note any flags in the task log as documents are generated.

    Deliberately only a log line. Flags are observations, and an earlier design
    that let them block generation put the tool in the position of overruling
    the person using it — for a judgement it is not well placed to make. The
    flags are on the card and in the workspace before this point; if the
    decision is to apply anyway, that is the decision.
    """
    record = db.get_job_flags(job["id"]) if job else None
    flags = (record or {}).get("flags") or []
    if not flags:
        return
    emit("log", message=(
        f"{len(flags)} flag{'' if len(flags) == 1 else 's'} on this role: "
        + "; ".join(f"{item['label']} — {item['requirement']}" for item in flags)
    ))


def command_jobs_set_document_track(payload):
    """Pin the document track for one role, or clear it back to derived."""
    job_id = payload["job_id"]
    track = db.set_job_document_track(job_id, payload.get("track"))
    resolved = db.resolve_document_track(job_id)
    db.add_application_event(
        job_id, "note", "Document track set",
        f"{db.DOC_TRACK_LABELS.get(resolved['track'], resolved['track'])} ({resolved['source']}) — "
        f"{' '.join(resolved['reasons'])}",
    )
    return {"job_id": job_id, "track": track, "resolved": resolved}


def command_jobs_log_external(payload):
    """Log an application made outside the pipeline (item 2) — the Carlisle-BA
    blind spot. Reuses the manual-add pipeline, forced to the applied stage so
    its outcome snapshot is captured, and records the document used if supplied."""
    result = command_jobs_add_manual({**payload, "stage": "applied"})
    job_id = result.get("job_id")
    if job_id and _clean_text(payload.get("doc_used")):
        db.update_job_application(job_id, {"resume_used": _clean_text(payload.get("doc_used"))})
    return {**result, "external": True}


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "jobs:list": command_jobs_list,
    "jobs:counts": command_jobs_counts,
    "jobs:addManual": command_jobs_add_manual,
    "jobs:logExternal": command_jobs_log_external,
    "jobs:updateStatus": command_jobs_update_status,
    "jobs:update": command_jobs_update,
    "jobs:cleanupArchive": command_jobs_cleanup_archive,
    "jobs:resetRejected": command_jobs_reset_rejected,
    "jobs:moveProfile": command_jobs_move_profile,
    "jobs:detail": command_jobs_detail,
    "jobs:addFlag": command_jobs_add_flag,
    "jobs:dismissFlag": command_jobs_dismiss_flag,
    "jobs:clearFlags": command_jobs_clear_flags,
    "jobs:setChannel": command_jobs_set_channel,
    "jobs:exportShortlist": command_jobs_export_shortlist,
    "jobs:setDocumentTrack": command_jobs_set_document_track,
    "jobs:delete": command_jobs_delete,
    "interviews:add": command_interviews_add,
    "interviews:update": command_interviews_update,
    "events:add": command_events_add,
    "analysis:run": command_analysis_run,
    "analysis:job": command_analysis_job,
}
