"""Campaign scoring, the daily plan, and the attack queue.

Split out of database_manager.py, which re-exports everything here.
"""
import re
from datetime import datetime, timedelta
from .connection import (
    get_db_connection,
)
from .constants import (
    PIPELINE_STAGES,
)
from .text import (
    _row_dict,
)
from .lanes import (
    _profile_filter_clause,
    _sync_lane_opportunity_for_job,
)
from .outcomes import (
    annotate_channel_warmth,
    warm_contact_index,
)
from .jobs import (
    normalize_stage,
)

CAMPAIGN_CORE_TERMS = [
    "it manager", "group it", "head of it", "technology delivery", "technology operations",
    "it operations", "infrastructure", "cloud", "azure", "microsoft 365", "enterprise systems",
    "business systems", "service delivery", "vendor management", "msp", "cybersecurity",
    "transformation", "digital", "systems manager", "platform", "release", "change",
]


CAMPAIGN_SERVICE_ENABLEMENT_ANCHORS = [
    "service delivery", "service enablement", "delivery enablement", "service improvement",
    "business improvement", "process improvement", "business process improvement",
    "service redesign", "process redesign", "service optimisation", "service optimization",
    "service design", "delivery governance", "education transformation", "learner experience",
]


CAMPAIGN_SERVICE_ENABLEMENT_TERMS = [
    "service delivery", "service enablement", "delivery enablement", "service improvement",
    "business improvement", "process improvement", "business process improvement",
    "service redesign", "process redesign", "service optimisation", "service optimization",
    "service design", "lean", "intake", "triage", "prioritisation", "prioritization",
    "roadmap", "delivery governance", "governance", "reporting rhythms", "status reporting",
    "dependency management", "decision logs", "escalation pathways", "improvement pipeline",
    "kpi", "service measures", "benefits realisation", "benefits realization", "raid",
    "executive-ready", "decision papers", "stakeholder engagement", "co-design",
    "change enablement", "cross-functional dependencies", "digital/it", "system enhancements",
    "workflow optimisation", "workflow optimization", "digitisation", "digitization",
    "user experience", "customer-focused services", "education transformation",
    "learner experience", "higher education", "university",
]


CAMPAIGN_OT_TERMS = [
    "operational technology", " ot ", "industrial systems", "industrial automation",
    "automation systems", "iot", "telemetry", "control systems", "manufacturing systems",
    "plant systems", "production systems", "scada", "bms", "facilities technology",
    "data centre operations", "data center operations", "warehouse systems",
    "supply chain systems", "connected devices", "intralogistics", "mechatronics",
    "robotics", "edge environment", "site migrations",
]


CAMPAIGN_DIRECT_EMPLOYER_TERMS = [
    "direct_employer", "our organisation", "our organization", "our business", "our company",
    "join our team", "we are seeking", "we're seeking",
]


CAMPAIGN_RECRUITER_TERMS = [
    "recruitment", "recruiting", "recruiter", "talent acquisition", "hays", "randstad",
    "michael page", "robert half", "peoplebank", "davidson", "fourquarters",
]


CAMPAIGN_PENALTY_TERMS = [
    "junior", "graduate program", "graduate role", "graduate position", "graduate engineer",
    "entry level", "helpdesk", "service desk analyst", "level 1",
    "sales executive", "commission", "presales", "pre-sales", "shift work", "night shift",
    "brisbane", "sydney", "perth", "adelaide", "canberra", "heavy travel", "field technician",
    "plc programmer", "electrical design", "controls engineer", "developer", "software engineer",
]


CAMPAIGN_LEADERSHIP_TERMS = [
    "manager", "lead", "leader", "leadership", "head", "director", "owner",
    "business partner", "service delivery", "delivery manager", "operations manager",
    "project manager", "program manager", "portfolio", "vendor management", "msp",
    "governance", "transformation", "strategy",
]


CAMPAIGN_HANDS_ON_ENGINEER_TERMS = [
    "systems engineer", "system engineer", "cloud engineer", "infrastructure engineer",
    "network engineer", "systems administrator", "system administrator", "cloud administrator",
    "m365 administrator", "microsoft 365 administrator", "endpoint administrator", "desktop engineer",
    "intune", "endpoint manager", "entra", "azure ad", "active directory", "exchange online",
    "sharepoint online", "teams administration", "powershell", "defender", "sentinel",
    "conditional access", "mfa", "iaas", "paas", "az-104", "ms-102", "hands-on",
    "hands on", "configuration", "administration",
]


def _campaign_haystack(job):
    return " ".join(
        str(job.get(key) or "")
        for key in (
            "title", "company", "location", "salary", "description", "source",
            "employer_type", "actual_company", "advertiser_company",
        )
    ).lower()


def _campaign_advertiser_haystack(job):
    return " ".join(
        str(job.get(key) or "")
        for key in ("company", "source", "employer_type", "actual_company", "advertiser_company")
    ).lower()


def _matched_terms(haystack, terms):
    matches = []
    padded = f" {haystack} "
    for term in terms:
        needle = term.lower().strip()
        if needle == "ot":
            if " ot " in padded or "it/ot" in padded or "ot/" in padded:
                matches.append("OT")
        elif re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
            matches.append(term)
    return matches


def _salary_numbers(value):
    text = str(value or "").lower().replace(",", "")
    numbers = [int(match) for match in re.findall(r"\b(\d{2,7})\b", text)]
    normalized = []
    for number in numbers:
        if number < 1000 and number >= 80:
            normalized.append(number * 1000)
        elif number >= 1000:
            normalized.append(number)
    return normalized


def campaign_salary_band(value):
    text = str(value or "").lower()
    numbers = _salary_numbers(value)
    if not numbers:
        return "unknown"
    high = max(numbers)
    low = min(numbers)
    day_rate_signal = any(term in text for term in ("/day", "per day", "daily", "day rate", "p/d", "pd"))
    if day_rate_signal or (500 <= high <= 2500 and "$" in text and "k" not in text):
        if high >= 1000:
            return "premium"
        if high >= 700:
            return "target"
        return "below_target"
    if high >= 170000:
        return "premium"
    if high >= 130000 or low >= 120000:
        return "target"
    return "below_target"


def campaign_role_family(job, haystack=None):
    haystack = haystack or _campaign_haystack(job)
    title = str(job.get("title") or "").lower()
    if _matched_terms(haystack, CAMPAIGN_SERVICE_ENABLEMENT_ANCHORS):
        return "Service delivery / enablement"
    if any(term in haystack for term in ("operational technology", "industrial systems", "industrial automation", "automation systems", "scada", "intralogistics", "control systems", "manufacturing systems")):
        return "OT / engineering-adjacent"
    if any(term in title for term in ("cyber", "security", "resilience")):
        return "Cyber / resilience"
    if any(term in haystack for term in ("cloud", "infrastructure", "azure", "network", "data centre", "data center")):
        return "Cloud / infrastructure"
    if any(term in haystack for term in ("business systems", "enterprise systems", "erp", "sap", "dynamics", "workday", "salesforce")):
        return "Business / enterprise systems"
    if any(term in haystack for term in ("service delivery", "change", "release", "implementation", "operations")):
        return "IT operations / service delivery"
    if any(term in haystack for term in ("project", "program", "delivery", "transformation", "portfolio", "epmo")):
        return "Project / transformation delivery"
    if any(term in haystack for term in ("vendor", "msp", "contracts", "supplier")):
        return "Vendor / MSP governance"
    return "General technology leadership"


def score_campaign_job(row, warm_index=None):
    """Score one job for the campaign view.

    `warm_index` is the shared warm_contact_index for the batch; callers that
    rank a list should pass it so possible warm paths are found without an N+1
    lookup. Without it, warmth still reflects the stored channel and any named
    contact on the job itself.
    """
    job = _row_dict(row)
    annotate_channel_warmth([job], warm_index)
    haystack = _campaign_haystack(job)
    title = str(job.get("title") or "").lower()
    base_score = int(job.get("composite_score") or job.get("match_score") or 0)
    core_matches = _matched_terms(haystack, CAMPAIGN_CORE_TERMS)
    service_enablement_anchors = _matched_terms(haystack, CAMPAIGN_SERVICE_ENABLEMENT_ANCHORS)
    service_enablement_matches = _matched_terms(haystack, CAMPAIGN_SERVICE_ENABLEMENT_TERMS)
    ot_matches = _matched_terms(haystack, CAMPAIGN_OT_TERMS)
    penalty_matches = _matched_terms(haystack, CAMPAIGN_PENALTY_TERMS)
    direct_matches = _matched_terms(haystack, CAMPAIGN_DIRECT_EMPLOYER_TERMS)
    recruiter_matches = _matched_terms(_campaign_advertiser_haystack(job), CAMPAIGN_RECRUITER_TERMS)
    leadership_matches = _matched_terms(haystack, CAMPAIGN_LEADERSHIP_TERMS)
    hands_on_matches = _matched_terms(haystack, CAMPAIGN_HANDS_ON_ENGINEER_TERMS)
    salary_band = campaign_salary_band(job.get("salary"))

    role_family = campaign_role_family(job, haystack)
    core_bonus = min(18, len(core_matches) * 2)
    service_enablement_bonus = 0
    if service_enablement_anchors:
        service_enablement_bonus = min(20, len(service_enablement_anchors) * 4 + len(service_enablement_matches) * 2)
    ot_bonus = min(15, len(ot_matches) * 3)
    salary_bonus = {"premium": 10, "target": 6, "below_target": -8, "unknown": 0}[salary_band]
    explicit_direct = str(job.get("employer_type") or "").lower() == "direct_employer"
    direct_bonus = 5 if explicit_direct or (direct_matches and not recruiter_matches) else 0
    penalty = min(24, len(penalty_matches) * 4)

    if "developer" in penalty_matches or "software engineer" in penalty_matches:
        if any(term in haystack for term in ("manager", "lead", "head", "delivery", "operations")):
            penalty = max(0, penalty - 6)

    hands_on_ic_role = bool(hands_on_matches) and not any(term in title for term in CAMPAIGN_LEADERSHIP_TERMS)
    if hands_on_ic_role and base_score < 75:
        penalty += 10
        service_enablement_bonus = 0

    campaign_score = max(0, min(100, base_score + core_bonus + service_enablement_bonus + ot_bonus + salary_bonus + direct_bonus - penalty))
    if hands_on_ic_role and base_score < 70:
        campaign_score = min(campaign_score, 68)
    elif hands_on_ic_role and base_score < 75:
        campaign_score = min(campaign_score, 72)
    if hands_on_ic_role and base_score < 65 and not ot_matches:
        campaign_score = min(campaign_score, 60)

    if campaign_score >= 82:
        fit_type = "strong"
    elif campaign_score >= 70:
        fit_type = "good"
    elif campaign_score >= 58:
        fit_type = "watch"
    else:
        fit_type = "weak"

    reasons = []
    if core_matches:
        reasons.append(f"Core lane match: {', '.join(core_matches[:5])}")
    if service_enablement_bonus:
        reasons.append(f"Service delivery/enablement lift: {', '.join(service_enablement_matches[:6])}")
    if ot_matches:
        reasons.append(f"OT/engineering lift: {', '.join(ot_matches[:5])}")
    if salary_band in {"target", "premium"}:
        reasons.append(f"Salary band: {salary_band.replace('_', ' ')}")
    if direct_bonus:
        reasons.append("Direct-employer signal")
    if leadership_matches:
        reasons.append(f"Leadership/ownership signal: {', '.join(leadership_matches[:5])}")
    if not reasons:
        reasons.append("No strong campaign signal beyond base match score")

    risks = []
    if penalty_matches:
        risks.append(f"Penalty signals: {', '.join(penalty_matches[:5])}")
    if recruiter_matches:
        risks.append("Recruiter-listed role; employer identity and salary need research")
    if hands_on_ic_role and base_score < 75:
        risks.append("Hands-on engineer/admin role; keep out of Attack Queue unless the base match is strong")
    if salary_band == "below_target":
        risks.append("Salary appears below target")
    if role_family == "Cyber / resilience" and "cyber" not in str(job.get("title") or "").lower():
        risks.append("Cyber evidence may need careful positioning")
    if not risks:
        risks.append("No obvious campaign risk flagged")

    job.update(
        {
            "campaign_score": campaign_score,
            "fit_type": fit_type,
            "role_family": role_family,
            "salary_band": salary_band,
            "service_enablement_bonus": service_enablement_bonus,
            "service_enablement_terms": service_enablement_matches[:10],
            "ot_bonus": ot_bonus,
            "ot_terms": ot_matches[:8],
            "campaign_reasons": reasons,
            "campaign_risks": risks,
        }
    )
    return job


CAMPAIGN_PUBLIC_FIELDS = [
    "id", "title", "company", "location", "url", "application_url", "source", "profile_id",
    "profile_name", "pipeline_stage", "status", "match_score", "composite_score", "fragment_score",
    "campaign_score", "fit_type", "role_family", "salary_band", "market_pick", "service_enablement_bonus",
    "service_enablement_terms", "ot_bonus", "ot_terms",
    "campaign_reasons", "campaign_risks", "salary", "closing_date", "next_action",
    "next_action_date", "priority", "application_date", "contact_person", "contact_email",
    "interview_date", "interview_type", "interview_round", "interview_title",
    "interview_round_date", "interview_next_action", "interview_next_action_date",
    "channel", "channel_label", "warmth", "warmth_label", "warm_path",
]


def _campaign_public_job(job):
    return {key: job.get(key) for key in CAMPAIGN_PUBLIC_FIELDS if key in job}


def _campaign_stage_clause(include_all_profiles, profile_id):
    return _profile_filter_clause(profile_id, include_all_profiles, alias="jobs")


def _sort_campaign_candidates(jobs):
    """Rank campaign candidates with channel warmth as the primary dimension.

    Warmth outranks the score on purpose. Observed conversion is far higher
    through warm channels than cold portals, so a moderate-scoring role with a
    real contact behind it is worth more application effort than a
    higher-scoring cold board submission. The score still orders everything
    within a warmth tier.
    """
    def key(job):
        close = job.get("closing_date") or "9999-12-31"
        recent = job.get("date_scraped") or job.get("updated_at") or job.get("id") or ""
        return (-int(job.get("warmth") or 0), -int(job.get("campaign_score") or 0), close, str(recent))

    return sorted(jobs, key=key)


def get_campaign_summary(profile_id=None, include_all_profiles=False, limit=12, min_score=65):
    profile_clause, params = _campaign_stage_clause(include_all_profiles, profile_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            {profile_clause}
            ORDER BY jobs.id DESC
            """,
            params,
        ).fetchall()
        interview_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name,
                   interviews.round_number AS interview_round,
                   interviews.title AS interview_title,
                   interviews.interview_date AS interview_round_date,
                   interviews.next_action AS interview_next_action,
                   interviews.next_action_date AS interview_next_action_date
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            LEFT JOIN interviews ON interviews.job_id = jobs.id
            WHERE jobs.pipeline_stage = 'interviewing'
            {profile_clause}
            ORDER BY COALESCE(interviews.next_action_date, jobs.next_action_date, interviews.interview_date, jobs.interview_date, '9999-12-31') ASC
            """,
            params,
        ).fetchall()

    today = datetime.now().date().isoformat()
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()

    warm_index = warm_contact_index(profile_id, include_all_profiles)
    scored = [score_campaign_job(row, warm_index) for row in rows]
    new_jobs = [job for job in scored if normalize_stage(job.get("pipeline_stage") or job.get("status")) == "new"]
    interested = [job for job in scored if normalize_stage(job.get("pipeline_stage") or job.get("status")) == "interested"]
    applied = [job for job in scored if normalize_stage(job.get("pipeline_stage") or job.get("status")) == "applied"]
    interviewing = [score_campaign_job(row, warm_index) for row in interview_rows]

    high_value_new = [
        job for job in new_jobs
        if int(job.get("campaign_score") or 0) >= int(min_score or 65)
    ]
    # "Perfect fit" is relative to this week's market: when fewer roles clear
    # the absolute floor than the queue holds, backfill with the best of the
    # rest (down to a hard floor) and flag them, so a thin market surfaces
    # "best available" instead of an empty queue.
    market_floor = max(50, int(min_score or 65) - 15)
    market_picks = []
    if len(high_value_new) < int(limit or 12):
        backfill_pool = [
            job for job in new_jobs
            if market_floor <= int(job.get("campaign_score") or 0) < int(min_score or 65)
        ]
        market_picks = _sort_campaign_candidates(backfill_pool)[: int(limit or 12) - len(high_value_new)]
        for job in market_picks:
            job["market_pick"] = True
    attack_queue = _sort_campaign_candidates(high_value_new + market_picks)[: int(limit or 12)]
    attack_today = _sort_campaign_candidates(
        [
            job for job in interested
            if (job.get("next_action_date") or "9999-12-31") <= tomorrow
               or int(job.get("campaign_score") or 0) >= 82
        ]
    )[:20]
    follow_up = _sort_campaign_candidates(
        [
            job for job in applied
            if not job.get("next_action_date")
               or (job.get("next_action_date") or "9999-12-31") <= tomorrow
               or int(job.get("campaign_score") or 0) >= 82
        ]
    )[:20]
    ignore_fast = _sort_campaign_candidates(
        [
            job for job in new_jobs
            if int(job.get("campaign_score") or 0) < 55
               or job.get("fit_type") == "weak"
        ]
    )[-20:]

    role_family_counts = {}
    salary_band_counts = {}
    fit_counts = {}
    for job in scored:
        role_family_counts[job["role_family"]] = role_family_counts.get(job["role_family"], 0) + 1
        salary_band_counts[job["salary_band"]] = salary_band_counts.get(job["salary_band"], 0) + 1
        fit_counts[job["fit_type"]] = fit_counts.get(job["fit_type"], 0) + 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile_id": profile_id,
        "include_all_profiles": bool(include_all_profiles),
        "min_score": int(min_score or 65),
        "limit": int(limit or 12),
        "metrics": {
            "new": len(new_jobs),
            "high_value_new": len(high_value_new),
            "attack_today": len(attack_today),
            "follow_up": len(follow_up),
            "interviewing": len(interviewing),
            "ot_weighted_new": len([job for job in high_value_new if job.get("ot_bonus", 0) > 0]),
            "market_backfilled": len(market_picks),
            "role_family_counts": role_family_counts,
            "salary_band_counts": salary_band_counts,
            "fit_counts": fit_counts,
        },
        "attack_queue": [_campaign_public_job(job) for job in attack_queue],
        "attack_today": [_campaign_public_job(job) for job in attack_today],
        "follow_up": [_campaign_public_job(job) for job in follow_up],
        "interview_conversion": [_campaign_public_job(job) for job in interviewing],
        "ignore_fast": [_campaign_public_job(job) for job in ignore_fast],
        "today": today,
        "tomorrow": tomorrow,
    }


_PLAN_JOB_FIELDS = (
    "id", "title", "company", "profile_id", "profile_name", "pipeline_stage", "url",
    "match_score", "composite_score", "campaign_score", "fit_type", "market_pick",
    "closing_date", "closing_date_source", "next_action", "next_action_date",
    "application_date", "application_url", "salary", "priority",
    "contact_person", "contact_email", "contact_phone",
    "interview_date", "interview_type", "interview_people", "feedback", "notes",
    "channel", "channel_label", "warmth", "warmth_label", "warm_path",
)


def _plan_job_ref(job):
    return {key: job.get(key) for key in _PLAN_JOB_FIELDS if key in job}


def get_campaign_plan(profile_id=None, include_all_profiles=False, limit=10):
    """One finite, ordered plan for today.

    The kanban is the database; this is mission control. Items are merged
    across urgency tiers — interviews/offers, imminent closes, overdue
    actions, stale applications, then the best new roles to stage — and each
    job appears once, at its most urgent. Progress counters let the UI show
    cadence against the weekly application goal.
    """
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    now = datetime.now()
    today = now.date().isoformat()
    close_horizon = (now + timedelta(days=3)).date().isoformat()
    interview_horizon = (now + timedelta(days=7)).date().isoformat()
    weekly_goal = 6

    with get_db_connection() as conn:
        active_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            AND (jobs.match_score IS NULL OR jobs.match_score >= 45)
            {profile_clause}
            """,
            params,
        ).fetchall()
        interview_rows = conn.execute(
            f"""
            SELECT interviews.round_number, interviews.interview_date, interviews.interview_type,
                   interviews.next_action AS interview_next_action,
                   jobs.id AS job_id, jobs.title, jobs.company, jobs.profile_id, jobs.url,
                   jobs.pipeline_stage, profiles.name AS profile_name
            FROM interviews
            JOIN jobs ON jobs.id = interviews.job_id
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            AND interviews.interview_date IS NOT NULL
            AND date(interviews.interview_date) >= date('now', 'localtime')
            AND date(interviews.interview_date) <= date(?)
            {profile_clause}
            ORDER BY interviews.interview_date ASC
            """,
            [interview_horizon] + params,
        ).fetchall()
        applied_week = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE date(application_date) >= date('now', 'localtime', '-6 days') {profile_clause}",
            params,
        ).fetchone()[0]
        actions_today = conn.execute(
            f"""
            SELECT COUNT(*) FROM application_events
            JOIN jobs ON jobs.id = application_events.job_id
            WHERE date(application_events.created_at, 'localtime') = date('now', 'localtime')
            AND application_events.event_type IN ('stage', 'documents', 'prompt', 'note', 'applied', 'interview')
            {profile_clause}
            """,
            params,
        ).fetchone()[0]

    items = []
    seen_jobs = set()

    def add(kind, urgency, title, detail, due, job=None):
        job_id = job.get("id") if job else None
        if job_id and job_id in seen_jobs:
            return
        if job_id:
            seen_jobs.add(job_id)
        items.append({
            "kind": kind,
            "urgency": urgency,
            "title": title,
            "detail": detail,
            "due": str(due or "")[:10],
            "job": _plan_job_ref(job) if job else None,
        })

    # Tier 0 — interviews on the calendar beat everything.
    for row in interview_rows:
        job = {key: row[key] for key in row.keys()}
        job["id"] = row["job_id"]
        round_label = f"Round {row['round_number']}" + (f" · {row['interview_type']}" if row["interview_type"] else "")
        add(
            "interview", 0,
            f"Prepare: interview at {row['company'] or row['title']}",
            f"{row['title']} — {round_label}",
            row["interview_date"], job,
        )

    warm_index = warm_contact_index(profile_id, include_all_profiles)
    scored = [score_campaign_job(row, warm_index) for row in active_rows]
    by_urgency_pool = {stage: [] for stage in PIPELINE_STAGES}
    for job in scored:
        by_urgency_pool.setdefault(normalize_stage(job.get("pipeline_stage") or job.get("status")), []).append(job)

    # Tier 0 — an offer on the table is the highest-value work in the system.
    for job in by_urgency_pool.get("offer", []):
        add("offer", 0, f"Review offer: {job.get('company') or job.get('title')}", job.get("title") or "", job.get("next_action_date") or today, job)

    # Tier 1 — decent roles closing within 3 days: apply before the door shuts.
    closing_pool = [
        job for job in by_urgency_pool.get("new", []) + by_urgency_pool.get("interested", [])
        if job.get("closing_date") and today <= str(job["closing_date"])[:10] <= close_horizon
        and int(job.get("campaign_score") or 0) >= 55
    ]
    for job in sorted(closing_pool, key=lambda item: str(item.get("closing_date"))):
        add(
            "closing", 1,
            f"Apply before close: {job.get('title')}",
            f"{job.get('company') or 'Unknown company'} — closes {str(job['closing_date'])[:10]}, campaign {job.get('campaign_score')}",
            job.get("closing_date"), job,
        )

    # Tier 2 — actions you already promised yourself, now due or overdue.
    overdue_pool = [
        job for stage in ("interviewing", "applied", "interested")
        for job in by_urgency_pool.get(stage, [])
        if job.get("next_action_date") and str(job["next_action_date"])[:10] <= today
    ]
    for job in sorted(overdue_pool, key=lambda item: str(item.get("next_action_date"))):
        add(
            "overdue", 2,
            job.get("next_action") or f"Action due: {job.get('title')}",
            f"{job.get('title')} at {job.get('company') or 'Unknown company'} — due {str(job['next_action_date'])[:10]}",
            job.get("next_action_date"), job,
        )

    # Tier 3 — applications going quiet: 5+ days, nothing scheduled.
    stale_cutoff = (now - timedelta(days=5)).date().isoformat()
    stale_pool = [
        job for job in by_urgency_pool.get("applied", [])
        if not job.get("next_action_date")
        and job.get("application_date") and str(job["application_date"])[:10] <= stale_cutoff
    ]
    for job in sorted(stale_pool, key=lambda item: str(item.get("application_date"))):
        add(
            "followup", 3,
            f"Follow up: {job.get('company') or job.get('title')}",
            f"{job.get('title')} — applied {str(job['application_date'])[:10]}, no response logged",
            today, job,
        )

    # Tier 4 — fill remaining slots with the best new roles to stage.
    if len(items) < limit:
        stage_pool = _sort_campaign_candidates([
            job for job in by_urgency_pool.get("new", [])
            if int(job.get("campaign_score") or 0) >= 50 and job.get("id") not in seen_jobs
        ])
        for job in stage_pool[: limit - len(items)]:
            pick_note = " · best available this window" if job.get("market_pick") else ""
            add(
                "stage", 4,
                f"Review and stage: {job.get('title')}",
                f"{job.get('company') or 'Unknown company'} — campaign {job.get('campaign_score')} ({job.get('fit_type')}){pick_note}",
                job.get("closing_date") or "", job,
            )

    items.sort(key=lambda item: (item["urgency"], item["due"] or "9999-12-31"))
    plan = items[:limit]
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "today": today,
        "plan": plan,
        "progress": {
            "applied_week": applied_week,
            "weekly_goal": weekly_goal,
            "actions_today": actions_today,
            "interviews_upcoming": len(interview_rows),
            "due_now": len([item for item in plan if item["urgency"] <= 2]),
            "queue_depth": len(by_urgency_pool.get("new", [])),
        },
    }


def stage_campaign_attack_queue(profile_id=None, include_all_profiles=False, limit=12, min_score=65, due_date=None):
    summary = get_campaign_summary(profile_id, include_all_profiles, limit, min_score)
    due_date = due_date or summary["tomorrow"]
    moved = []
    skipped = []
    now = datetime.now().isoformat(timespec="seconds")
    today = datetime.now().date().isoformat()

    with get_db_connection() as conn:
        for candidate in summary["attack_queue"]:
            job_id = candidate["id"]
            current = conn.execute("SELECT id, pipeline_stage, status, title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not current:
                skipped.append({"id": job_id, "reason": "missing"})
                continue
            if normalize_stage(current["pipeline_stage"] or current["status"]) != "new":
                skipped.append({"id": job_id, "title": current["title"], "company": current["company"], "reason": current["pipeline_stage"] or current["status"]})
                continue
            updates = {
                "pipeline_stage": "interested",
                "status": "interested",
                "priority": "high" if int(candidate.get("campaign_score") or 0) >= 70 else "normal",
                "next_action": "Prepare targeted application and outreach",
                "next_action_date": due_date,
                "last_interaction_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                UPDATE jobs
                SET pipeline_stage = ?,
                    status = ?,
                    priority = ?,
                    next_action = ?,
                    next_action_date = ?,
                    last_interaction_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    updates["pipeline_stage"],
                    updates["status"],
                    updates["priority"],
                    updates["next_action"],
                    updates["next_action_date"],
                    updates["last_interaction_at"],
                    updates["updated_at"],
                    job_id,
                ),
            )
            _sync_lane_opportunity_for_job(conn, job_id, updates)
            conn.execute(
                """
                INSERT INTO application_events (job_id, event_type, title, details, event_date, due_date, created_at)
                VALUES (?, 'stage', 'Moved to Interested', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "Campaign staged. "
                    + f"Score {candidate['campaign_score']} ({candidate['fit_type']}). "
                    + " | ".join(candidate.get("campaign_reasons") or []),
                    today,
                    due_date,
                    now,
                ),
            )
            moved.append(_campaign_public_job(candidate))
        conn.commit()
    return {"moved": moved, "skipped": skipped, "due_date": due_date}


def refresh_campaign_actions(profile_id=None, include_all_profiles=False):
    profile_clause, params = _campaign_stage_clause(include_all_profiles, profile_id)
    today = datetime.now().date()
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    soon = (datetime.now() + timedelta(days=2)).date().isoformat()
    weekend = (datetime.now() + timedelta(days=3)).date().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    changed = []

    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage IN ('interested', 'applied', 'interviewing')
            {profile_clause}
            """,
            params,
        ).fetchall()
        for row in rows:
            job = score_campaign_job(row)
            stage = normalize_stage(job.get("pipeline_stage") or job.get("status"))
            score = int(job.get("campaign_score") or 0)
            closing = job.get("closing_date") or ""
            updates = {}
            if stage == "interviewing":
                updates = {
                    "priority": "high",
                    "next_action": "Follow up on interview outcome and prepare next round",
                    "next_action_date": tomorrow,
                }
            elif stage == "applied":
                high_value = score >= 78 or job.get("fit_type") == "strong"
                updates = {
                    "priority": "high" if high_value else "normal",
                    "next_action": "Follow up / ask for status",
                    "next_action_date": tomorrow if high_value else soon,
                }
            elif stage == "interested":
                if closing:
                    try:
                        closing_date = datetime.fromisoformat(closing[:10]).date()
                    except ValueError:
                        closing_date = None
                else:
                    closing_date = None
                if closing_date and closing_date < today:
                    updates = {
                        "priority": "normal",
                        "next_action": "Check if role is still open; close out if unavailable",
                        "next_action_date": tomorrow,
                    }
                elif closing_date and closing_date <= today + timedelta(days=4):
                    updates = {
                        "priority": "high",
                        "next_action": "Prepare application before close",
                        "next_action_date": tomorrow,
                    }
                else:
                    high_value = score >= 78 or job.get("fit_type") == "strong" or job.get("ot_bonus", 0) >= 6
                    updates = {
                        "priority": "high" if high_value else "normal",
                        "next_action": "Prepare targeted application and outreach" if high_value else "Prepare application",
                        "next_action_date": tomorrow if high_value else weekend,
                    }
            if not updates:
                continue
            updates["updated_at"] = now
            conn.execute(
                """
                UPDATE jobs
                SET priority = ?, next_action = ?, next_action_date = ?, updated_at = ?
                WHERE id = ?
                """,
                (updates["priority"], updates["next_action"], updates["next_action_date"], updates["updated_at"], job["id"]),
            )
            _sync_lane_opportunity_for_job(conn, job["id"], updates)
            changed.append(
                {
                    "id": job["id"],
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "pipeline_stage": stage,
                    "campaign_score": score,
                    "fit_type": job.get("fit_type"),
                    **updates,
                }
            )
        conn.commit()
    return {"changed": changed}


def get_campaign_weekly_report(profile_id=None, include_all_profiles=False, days=7):
    profile_clause, params = _campaign_stage_clause(include_all_profiles, profile_id)
    since = (datetime.now() - timedelta(days=int(days or 7))).date().isoformat()
    with get_db_connection() as conn:
        applied_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.application_date IS NOT NULL
              AND date(jobs.application_date) >= date(?)
              {profile_clause}
            ORDER BY jobs.application_date DESC, jobs.id DESC
            """,
            [since] + params,
        ).fetchall()
        interview_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name, interviews.outcome, interviews.notes AS interview_notes
            FROM interviews
            JOIN jobs ON jobs.id = interviews.job_id
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE date(COALESCE(interviews.interview_date, interviews.created_at)) >= date(?)
              {profile_clause}
            ORDER BY COALESCE(interviews.interview_date, interviews.created_at) DESC
            """,
            [since] + params,
        ).fetchall()
        event_rows = conn.execute(
            f"""
            SELECT event_type, COUNT(*) AS count
            FROM application_events
            JOIN jobs ON jobs.id = application_events.job_id
            WHERE date(COALESCE(application_events.event_date, application_events.created_at)) >= date(?)
              {profile_clause}
            GROUP BY event_type
            """,
            [since] + params,
        ).fetchall()

    applied = [score_campaign_job(row) for row in applied_rows]
    interviews = [score_campaign_job(row) for row in interview_rows]
    role_family_counts = {}
    for job in applied + interviews:
        role_family_counts[job["role_family"]] = role_family_counts.get(job["role_family"], 0) + 1
    best_families = sorted(role_family_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    event_counts = {row["event_type"]: row["count"] for row in event_rows}

    # Conversion by score band: the calibration readout for the scoring chain.
    # If 70-77 converts to interviews as well as 78+, the gatekeeper is
    # over-strict; if <60 never converts, the relaxed floor can come back up.
    def _score_band(job):
        score = int(job.get("match_score") or 0)
        if score >= 78:
            return "78+"
        if score >= 70:
            return "70-77"
        if score >= 60:
            return "60-69"
        return "<60"

    band_funnel = []
    for band in ("78+", "70-77", "60-69", "<60"):
        band_applied = len([job for job in applied if _score_band(job) == band])
        band_interviews = len([job for job in interviews if _score_band(job) == band])
        band_funnel.append({"band": band, "applied": band_applied, "interviews": band_interviews})

    return {
        "band_funnel": band_funnel,
        "since": since,
        "days": int(days or 7),
        "applied_count": len(applied),
        "interview_count": len(interviews),
        "event_counts": event_counts,
        "best_role_families": [{"role_family": family, "count": count} for family, count in best_families],
        "recent_applications": [_campaign_public_job(job) for job in applied[:12]],
        "recent_interviews": [_campaign_public_job(job) for job in interviews[:12]],
        "recommendations": _campaign_recommendations(applied, interviews, event_counts),
    }


def _campaign_recommendations(applied, interviews, event_counts):
    recommendations = []
    if len(applied) < 5:
        recommendations.append("Application volume is low; aim for 5-8 high-quality targeted applications this week.")
    if not interviews and len(applied) >= 8:
        recommendations.append("Applications are not converting to interviews yet; tighten the resume headline and role-specific proof points.")
    if interviews:
        recommendations.append("Interview signal exists; prioritise postmortems and sharpen risk answers for final-stage conversion.")
    if event_counts.get("stage", 0) > event_counts.get("prompt", 0) + event_counts.get("documents", 0):
        recommendations.append("Some staged roles may not have full attack packs yet; generate prompts or documents before applying.")
    if not recommendations:
        recommendations.append("Campaign cadence looks healthy; keep staging selectively and following up every serious application.")
    return recommendations
