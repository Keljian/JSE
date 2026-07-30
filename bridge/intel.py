"""Hidden-market, warm-contact, and company-research commands.

Split out of python_bridge.py, which re-exports everything here.
"""
import contextlib
import sys

import database_manager as db
import concurrency
from .runtime import (
    _clean_text,
    emit,
    row_to_dict,
    rows_to_dicts,
)
from .documents import (
    _load_application_kit_payload,
    _posting_to_payload,
)
from .jobs import (
    _job_has_researched_company_intel,
)

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


_HIDDEN_MARKET_SECTIONS = (
    ("recruiters", "recruiter"),
    ("direct_employers", "direct_employer"),
    ("leadership_gaps", "leadership_gap"),
)


def command_hidden_market_get(payload):
    """Full Hidden Market tab payload: the mined intel ledgers (with a 'tracked'
    flag per target), the outreach to-do leads, and an overview rollup."""
    from datetime import date

    profile_id = payload.get("profile_id")
    include_all = bool(payload.get("include_all_profiles"))
    intel = db.get_hidden_market_intel(profile_id, include_all, payload.get("days") or 60)
    leads = db.list_hidden_market_leads(profile_id, include_all)

    tracked_keys = {(lead["target_type"], lead["target_key"]) for lead in leads}
    saved_strategies = {
        (item["target_type"], item["target_key"]): item
        for item in db.list_hidden_market_strategies(profile_id or 1)
    }
    contact_research = {
        (item["target_type"], item["target_key"]): item["research"]
        for item in db.list_hidden_market_contact_research(profile_id or 1)
    }
    for section, target_type in _HIDDEN_MARKET_SECTIONS:
        for item in intel.get(section, []):
            item["target_type"] = target_type
            item["target_key"] = db.hidden_market_target_key(target_type, item["name"], item.get("entity_key"))
            legacy_key = db.hidden_market_target_key(target_type, item["name"])
            item["tracked"] = (target_type, item["target_key"]) in tracked_keys or (target_type, legacy_key) in tracked_keys
            item["saved_strategy"] = (saved_strategies.get((target_type, item["target_key"])) or {}).get("strategy") or {}
            item["contact_research"] = contact_research.get((target_type, item["target_key"])) or {}

    today = date.today().isoformat()
    status_counts = {status: 0 for status in db.HIDDEN_MARKET_STATUSES}
    due_followups = 0
    for lead in leads:
        status = lead.get("status") or "todo"
        status_counts[status] = status_counts.get(status, 0) + 1
        next_step = lead.get("next_step_date")
        if next_step and status != "done" and next_step <= today:
            due_followups += 1

    overview = {
        "window_days": intel.get("window_days"),
        "recruiters": len(intel.get("recruiters", [])),
        "direct_employers": len(intel.get("direct_employers", [])),
        "leadership_gaps": len(intel.get("leadership_gaps", [])),
        "targets_surfaced": sum(len(intel.get(section, [])) for section, _ in _HIDDEN_MARKET_SECTIONS),
        "tracked_total": len(leads),
        "open_total": len(leads) - status_counts.get("done", 0),
        "status_counts": status_counts,
        "due_followups": due_followups,
        "converted": sum(1 for lead in leads if lead.get("outcome") == "converted"),
    }
    try:
        performance = db.get_hidden_market_stats(profile_id, include_all, payload.get("performance_days") or 30)
    except Exception as exc:
        emit("log", message=f"Intelligence outcome learning unavailable: {exc}")
        performance = {}
    return {"intel": intel, "leads": leads, "overview": overview, "performance": performance}


def command_hidden_market_track(payload):
    lead = db.add_hidden_market_lead(
        payload.get("profile_id", 1),
        payload.get("target_type"),
        payload.get("target_name") or payload.get("name"),
        action=payload.get("action"),
        contact_person=payload.get("contact_person"),
        contact_email=payload.get("contact_email"),
        contact_phone=payload.get("contact_phone"),
        domain=payload.get("domain"),
        outreach_channel=payload.get("outreach_channel"),
        strategy=payload.get("strategy"),
        opportunity_score=payload.get("opportunity_score"),
        score_reasons=payload.get("score_reasons"),
        target_key_override=payload.get("target_key"),
    )
    return {"lead": lead}


def _hidden_market_lead_id(payload):
    lead_id = payload.get("id") or payload.get("lead_id")
    if not lead_id:
        raise ValueError("Missing hidden-market lead id.")
    return lead_id


def command_hidden_market_lead_update(payload):
    return {"lead": db.update_hidden_market_lead(_hidden_market_lead_id(payload), payload.get("updates") or {})}


def command_hidden_market_touch(payload):
    lead = db.add_hidden_market_touchpoint(
        _hidden_market_lead_id(payload),
        payload.get("note"),
        status=payload.get("status"),
        next_step_date=payload.get("next_step_date"),
    )
    return {"lead": lead}


def command_hidden_market_lead_delete(payload):
    db.delete_hidden_market_lead(_hidden_market_lead_id(payload))
    return {"ok": True}


def command_hidden_market_convert(payload):
    return db.convert_hidden_market_lead_to_job(_hidden_market_lead_id(payload))


def _hidden_market_lane_context(profile_id):
    try:
        settings = db.get_lane_settings(profile_id) or {}
    except Exception:
        return ""
    parts = []
    for label, key in (
        ("Intent", "lane_intent"), ("Target titles", "target_titles"),
        ("Target domains", "target_domains"), ("Seniority", "seniority"),
        ("Must-have", "must_have_terms"), ("Preferred location", "preferred_location"),
    ):
        value = settings.get(key)
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def command_hidden_market_strategy(payload):
    import llm_handler
    import contact_research

    profile_id = payload.get("profile_id", 1)
    target = payload.get("target") or {}
    research = contact_research.enrich_target_contacts(profile_id, target, force=bool(payload.get("force_contact_refresh")))
    if research.get("requires_selection"):
        return {"strategy": None, "contact_research": research, "requires_contact_selection": True}
    strategy = llm_handler.hidden_market_strategy(
        target,
        lane_context=_hidden_market_lane_context(profile_id),
        contact_research=research,
    )
    saved = db.save_hidden_market_strategy(
        profile_id,
        target.get("target_type") or "target",
        target.get("name") or target.get("target_name") or "Unknown target",
        strategy,
        provider="local",
        target_key=target.get("target_key"),
    )
    return {"strategy": strategy, "saved": saved, "contact_research": research, "requires_contact_selection": False}


def command_hidden_market_contact_select(payload):
    profile_id = payload.get("profile_id", 1)
    target = payload.get("target") or {}
    target_type = target.get("target_type") or payload.get("target_type") or "target"
    target_name = target.get("name") or target.get("target_name") or payload.get("target_name") or "Unknown target"
    target_key = target.get("target_key") or payload.get("target_key") or db.hidden_market_target_key(target_type, target_name, target.get("entity_key"))
    result = db.select_hidden_market_contact(profile_id, target_type, target_key, payload.get("candidate_id"))
    if not result:
        raise ValueError("No contact research exists for this target. Build strategy first.")
    return {"contact_research": result["research"]}


def command_warm_contacts_list(payload):
    return {
        "contacts": db.list_warm_contacts(
            payload.get("profile_id"),
            payload.get("organisation"),
            payload.get("limit") or 500,
        )
    }


def command_warm_contacts_save(payload):
    return {
        "contact": db.upsert_warm_contact(
            payload.get("name"),
            organisation=payload.get("organisation"),
            profile_id=payload.get("profile_id"),
            role_title=payload.get("role_title"),
            email=payload.get("email"),
            phone=payload.get("phone"),
            linkedin_url=payload.get("linkedin_url"),
            relationship=payload.get("relationship"),
            origin=payload.get("origin") or "manual",
            notes=payload.get("notes"),
        )
    }


def command_warm_contacts_delete(payload):
    contact_id = payload.get("id") or payload.get("contact_id")
    if not contact_id:
        raise ValueError("Missing warm contact id.")
    db.delete_warm_contact(contact_id)
    return {"ok": True}


def command_warm_contacts_seed(payload):
    """Populate the contact book from contact research and company profiles."""
    return {"seeded": db.seed_warm_contacts(payload.get("profile_id") or 1)}


def command_hidden_market_add_target(payload):
    """Create a warm-channel lead against a named employer, with no scraped job
    behind it (item 6). This is the entry point the hidden-market modules were
    missing: every existing path required a target surfaced from ad data, which
    is why the tables held zero rows."""
    profile_id = payload.get("profile_id", 1)
    target_name = _clean_text(payload.get("target_name") or payload.get("name"))
    if not target_name:
        raise ValueError("A warm lead needs a target employer name.")
    contact_name = _clean_text(payload.get("contact_person"))
    if contact_name:
        db.upsert_warm_contact(
            contact_name,
            organisation=target_name,
            profile_id=profile_id,
            role_title=payload.get("contact_role"),
            email=payload.get("contact_email"),
            phone=payload.get("contact_phone"),
            linkedin_url=payload.get("linkedin_url"),
            origin="lead",
        )
    lead = db.add_hidden_market_lead(
        profile_id,
        payload.get("target_type") or "employer",
        target_name,
        action=payload.get("action") or "Direct approach — no advertised role",
        contact_person=contact_name,
        contact_email=payload.get("contact_email"),
        contact_phone=payload.get("contact_phone"),
        domain=payload.get("domain"),
        outreach_channel=payload.get("outreach_channel") or db.CHANNEL_DIRECT_OUTREACH,
    )
    return {"lead": lead}


def command_warm_channel_activity(payload):
    return {
        "activity": db.get_warm_channel_activity(
            payload.get("profile_id"),
            bool(payload.get("include_all_profiles")),
            payload.get("days") or 7,
        )
    }


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "enrichment:jobExtract": command_enrichment_job_extract,
    "enrichment:applicationReview": command_enrichment_application_review,
    "enrichment:process": command_enrichment_process,
    "enrichment:status": command_enrichment_status,
    "company:classify": command_company_classify,
    "company:research": command_company_research,
    "company:researchBatch": command_company_research_batch,
    "warmContacts:list": command_warm_contacts_list,
    "warmContacts:save": command_warm_contacts_save,
    "warmContacts:delete": command_warm_contacts_delete,
    "warmContacts:seed": command_warm_contacts_seed,
    "warmChannel:activity": command_warm_channel_activity,
    "hiddenMarket:addTarget": command_hidden_market_add_target,
    "hiddenMarket:get": command_hidden_market_get,
    "hiddenMarket:track": command_hidden_market_track,
    "hiddenMarket:leadUpdate": command_hidden_market_lead_update,
    "hiddenMarket:touch": command_hidden_market_touch,
    "hiddenMarket:leadDelete": command_hidden_market_lead_delete,
    "hiddenMarket:convert": command_hidden_market_convert,
    "hiddenMarket:strategy": command_hidden_market_strategy,
    "hiddenMarket:contactSelect": command_hidden_market_contact_select,
}
