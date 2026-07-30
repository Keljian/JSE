"""Read-only analytics: dashboard, funnel, targeting, campaign, calendar.

Split out of python_bridge.py, which re-exports everything here.
"""

import database_manager as db
from .runtime import (
    _housekeeping_due,
    _json_loads_maybe,
    compact_job_dicts,
    emit,
    row_to_dict,
    rows_to_dicts,
)
from .lanes import (
    _person_id_for,
)

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
        "interview_nudges": rows_to_dicts(data.get("interview_nudges") or []),
        "warm_channel": data.get("warm_channel") or {},
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
    # Hidden-market outreach performance (funnel, rates, market mix, activity).
    try:
        stats["hidden_market"] = db.get_hidden_market_stats(
            payload.get("profile_id"),
            bool(payload.get("include_all_profiles")),
            days,
        )
    except Exception as exc:
        emit("log", message=f"Hidden-market stats unavailable: {exc}")
    return stats


def command_funnel_insights(payload):
    """Outcome-driven conversion analytics (item 4). Cached in app_settings;
    recompute on demand. Also refreshes the composite-scoring conversion priors."""
    recompute = bool(payload.get("recompute"))
    insights = db.get_funnel_insights(recompute=recompute)
    return {"insights": insights}


def command_funnel_outcome_detail(payload):
    """Record how far an application actually got (item 4).

    The outcome-hygiene nudge and the job view both call this: a first-round
    screen-out and a runner-up finish used to collapse to the same two states,
    which hid the near misses that carry the strongest signal in the funnel.
    """
    job_id = payload.get("job_id")
    if not job_id:
        raise ValueError("Missing job id.")
    stage = payload.get("interview_stage_reached")
    outcome = db.record_application_outcome_detail(
        job_id,
        outcome=payload.get("outcome"),
        interview_stage_reached=int(stage) if stage not in (None, "") else None,
        loss_reason=payload.get("loss_reason"),
        channel=payload.get("channel"),
    )
    return {"outcome": outcome}


def command_funnel_outcome_vocabulary(_payload):
    """The outcome/channel vocabularies, so the renderer never hardcodes them."""
    return {
        "outcomes": [
            {"value": value, "label": db.OUTCOME_LABELS.get(value, value)}
            for value in db.APPLICATION_OUTCOMES
        ],
        "channels": [
            {"value": value, "label": db.CHANNEL_LABELS.get(value, value)}
            for value in db.APPLICATION_CHANNELS
        ],
    }


def command_targeting_summary(payload):
    """Allocation view for the Targeting card (item 7): applications by band,
    conversion by band and channel, and the share landing below baseline."""
    return {
        "summary": db.get_targeting_summary(
            payload.get("profile_id"),
            bool(payload.get("include_all_profiles")),
            payload.get("days") or 90,
        )
    }


def command_targeting_explain_score(payload):
    """Why a job's composite sits where it does, including any band demotion.
    Keeps the adjustment explainable rather than silent (item 3)."""
    job_id = payload.get("job_id")
    if not job_id:
        raise ValueError("Missing job id.")
    job = row_to_dict(db.get_job_details(job_id))
    if not job:
        raise ValueError("Job not found.")
    explanation = db.explain_composite_score(job)
    explanation["band_note"] = db.band_triage_note(job.get("title"))
    return {"explanation": explanation}


def command_funnel_mine_interview_fragments(payload):
    """Mine interview-validated fragments for a job (item 5). Long-running LLM
    work, so it runs as a cancellable task."""
    job_id = payload["job_id"]
    emit("status", message="Mining interview-validated fragments…")
    stored = db.mine_interview_validated_fragments(job_id, log=lambda m: emit("log", message=m))
    return {"job_id": job_id, "stored": stored}


def command_funnel_interview_learnings(payload):
    """Learnings tab payload: interview-validated fragments plus the interviewed
    jobs they were (or can be) mined from."""
    profile_id = payload.get("profile_id")
    include_all = bool(payload.get("include_all_profiles"))
    person_id = _person_id_for(profile_id or 1)

    fragments = []
    mined_job_ids = set()
    for row in db.get_interview_validated_fragments(person_id):
        data = row_to_dict(row)
        source_job_ids = _json_loads_maybe(data.get("source_job_ids_json"), [])
        for raw in source_job_ids:
            try:
                mined_job_ids.add(int(raw))
            except (TypeError, ValueError):
                pass
        fragments.append({
            "id": data["id"],
            "theme": data.get("theme"),
            "claim": data.get("claim"),
            "supporting_detail": data.get("supporting_detail"),
            "fragment_type": data.get("fragment_type"),
            "confidence": data.get("confidence"),
            "status": data.get("status"),
            "seniority": data.get("seniority"),
            "support_count": data.get("support_count"),
            "outcome_score": data.get("outcome_score"),
            "reuse_guidance": data.get("reuse_guidance"),
            "keywords": _json_loads_maybe(data.get("keywords_json"), []),
            "skills": _json_loads_maybe(data.get("skills_json"), []),
            "source_job_ids": source_job_ids,
            "updated_at": data.get("updated_at"),
        })

    interviewed_jobs = []
    for row in db.get_interviewed_jobs(profile_id, include_all):
        job = row_to_dict(row)
        job["mined"] = job["id"] in mined_job_ids
        interviewed_jobs.append(job)

    return {
        "fragments": fragments,
        "interviewed_jobs": interviewed_jobs,
        "person_id": person_id,
    }


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "dashboard:get": command_dashboard_get,
    "funnel:insights": command_funnel_insights,
    "funnel:interviewLearnings": command_funnel_interview_learnings,
    "funnel:mineInterviewFragments": command_funnel_mine_interview_fragments,
    "funnel:outcomeDetail": command_funnel_outcome_detail,
    "funnel:outcomeVocabulary": command_funnel_outcome_vocabulary,
    "targeting:summary": command_targeting_summary,
    "targeting:explainScore": command_targeting_explain_score,
    "calendar:get": command_calendar_get,
    "campaign:summary": command_campaign_summary,
    "campaign:plan": command_campaign_plan,
    "campaign:stageAttackQueue": command_campaign_stage_attack_queue,
    "campaign:refreshActions": command_campaign_refresh_actions,
    "campaign:weeklyReport": command_campaign_weekly_report,
    "stats:summary": command_stats_summary,
}
