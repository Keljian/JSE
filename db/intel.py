"""Hidden-market intelligence, leads, and the warm-contact book.

Split out of database_manager.py, which re-exports everything here.
"""
import re
import json
from collections import Counter
from datetime import datetime, timedelta
from .connection import (
    get_db_connection,
)
from .constants import (
    BROAD_RELEVANT_TITLES,
    KNOWN_RECRUITERS,
    RECRUITER_PHRASES,
)
from .text import (
    _clean,
    _company_key,
    _domain_from_value,
    _role_tokens,
    normalize_job_url,
)
from .companies import (
    _is_weak_company_candidate,
)
from .lanes import (
    _profile_filter_clause,
)
from .jobs import (
    add_job,
    update_job_application,
)
from .campaign import (
    CAMPAIGN_LEADERSHIP_TERMS,
    _matched_terms,
)

_HIDDEN_MARKET_JOB_BOARD_DOMAINS = {"seek.com", "linkedin.com", "indeed.com", "jora.com"}


# Whole words (and a few compound substrings) that mark a company NAME as an
# agency rather than an employer. Kept conservative: generic words like
# "people" or "resources" hit real employers too often.
_AGENCY_NAME_WORDS = {
    "recruit", "recruitment", "recruiting", "staffing", "personnel",
    "resourcing", "headhunters", "placement", "placements", "search",
}


_AGENCY_NAME_SUBSTRINGS = ("recruit", "talent", "staffing", "headhunt", "people2", "peoplebank")


def _agency_like_name(name):
    key = _company_key(name)
    if not key:
        return False
    if key in KNOWN_RECRUITERS or any(agency in key for agency in KNOWN_RECRUITERS):
        return True
    words = set(key.split())
    if words & _AGENCY_NAME_WORDS:
        return True
    return any(token in key for token in _AGENCY_NAME_SUBSTRINGS)


def _name_matches_domain(name, domain):
    """Does the employer name corroborate the ad's contact/application domain?

    'Monash University' vs monash.edu -> True; 'Agile' vs anzca.edu.au -> False
    (the ad's real organisation is ANZCA; the extracted name is noise)."""
    if not domain:
        return False
    core = domain.split(".")[0].lower()
    compact_name = _company_key(name).replace(" ", "")
    if len(core) >= 3 and core in compact_name:
        return True
    domain_compact = domain.replace(".", "").lower()
    return any(word in domain_compact for word in _company_key(name).split() if len(word) >= 4)


def _plausible_org_name(name):
    key = _company_key(name)
    return bool(key) and not _is_weak_company_candidate(name) and len(key.split()) <= 4


def _hidden_market_domain(row):
    for value in (row["contact_email"], row["application_url"], row["url"]):
        domain = _domain_from_value(value)
        if domain and not any(board in domain for board in _HIDDEN_MARKET_JOB_BOARD_DOMAINS):
            return domain
    return ""


def _market_job_intelligence(row):
    try:
        return json.loads(row["job_intelligence_json"] or "{}")
    except (TypeError, ValueError, KeyError):
        return {}


def _market_period(scraped, midpoint):
    return "current" if str(scraped or "") >= midpoint else "previous"


def _market_recency_points(last_seen):
    try:
        age = max(0, (datetime.now() - datetime.fromisoformat(str(last_seen).replace("Z", "+00:00").split("+")[0])).days)
    except (TypeError, ValueError):
        return 0
    if age <= 7:
        return 15
    if age <= 21:
        return 11
    if age <= 45:
        return 7
    return 3


def _finalise_market_target(entry, target_type, outcome_rates=None):
    roles = int(entry.get("roles") or entry.get("ic_count") or 0)
    best = int(entry.get("best_score") or 0)
    current = int(entry.pop("_current", 0))
    previous = int(entry.pop("_previous", 0))
    confidence = entry.get("confidence") or "low"
    confidence_points = {"high": 15, "medium": 10, "low": 5}.get(confidence, 5)
    contactable = bool(entry.get("contact_person") or entry.get("contact_email") or entry.get("contact_phone") or entry.get("domain"))
    outcome_rate = int((outcome_rates or {}).get(target_type, 0))
    score = round(
        min(35, best * 0.35)
        + min(20, roles * 5)
        + _market_recency_points(entry.get("last_seen"))
        + confidence_points
        + (10 if contactable else 0)
        + (5 if current > previous else 0)
        + min(10, outcome_rate * 0.1)
    )
    entry["opportunity_score"] = max(0, min(100, score))
    entry["momentum"] = {"current": current, "previous": previous, "delta": current - previous}
    entry["recommended_action"] = {
        "recruiter": "Contact the named consultant, reference the strongest recent role, and ask about adjacent unadvertised mandates.",
        "direct_employer": "Approach the relevant technology leader with evidence of recurring demand before the next role is advertised.",
        "leadership_gap": "Validate the reporting structure first, then test whether the growing team has an unadvertised leadership need.",
    }.get(target_type, "Review the evidence and choose a direct next step.")
    entry["score_reasons"] = [
        f"Best lane fit {best}%" if best else "No reliable fit score",
        f"{roles} supporting role{'s' if roles != 1 else ''}",
        f"{confidence} identity confidence",
        "Direct contact or domain available" if contactable else "No direct contact captured",
        f"Momentum {current - previous:+d} vs prior half-window",
    ]
    return entry


def _market_signal_rows(rows, midpoint):
    dimensions = {key: {} for key in ("title_families", "skills", "locations", "work_modes", "sources")}
    fallback_skills = (
        "stakeholder management", "vendor management", "cybersecurity", "cloud", "azure", "aws",
        "service delivery", "change management", "project management", "business analysis",
        "data governance", "erp", "sap", "leadership", "people management", "itil", "power bi",
    )

    def bump(dimension, label, period):
        label = _clean(str(label or ""))
        if not label or label.lower() in {"unknown", "other"}:
            return
        bucket = dimensions[dimension].setdefault(label, {"label": label, "current": 0, "previous": 0})
        bucket[period] += 1

    def fallback_family(title):
        value = str(title or "").lower()
        if _matched_terms(value, CAMPAIGN_LEADERSHIP_TERMS): return "IT leadership"
        if "business analyst" in value or re.search(r"\bba\b", value): return "business analysis"
        if any(term in value for term in ("program", "programme", "project", "delivery", "transformation")): return "delivery"
        if any(term in value for term in ("engineer", "embedded", "firmware", "mechatronic", "electronics")): return "engineering systems"
        if "product" in value: return "product"
        if any(term in value for term in ("support", "service desk", "helpdesk")): return "support"
        return ""

    for row in rows:
        period = _market_period(row["scraped_at"], midpoint)
        intel = _market_job_intelligence(row)
        family = intel.get("role_family") or fallback_family(row["title"])
        bump("title_families", family, period)
        skills = list(intel.get("core_skills") or [])[:10]
        if not skills:
            description = str(row["description_head"] or "").lower()
            skills = [skill for skill in fallback_skills if skill in description]
        for skill in skills:
            bump("skills", skill, period)
        bump("locations", row["location"], period)
        work_mode = intel.get("work_mode")
        if not work_mode:
            text = f"{row['location'] or ''} {row['description_head'] or ''}".lower()
            work_mode = "remote" if "remote" in text else "hybrid" if "hybrid" in text or "work from home" in text else "onsite" if "on-site" in text or "onsite" in text else ""
        bump("work_modes", work_mode, period)
        bump("sources", row["source"], period)

    result = {}
    for dimension, buckets in dimensions.items():
        items = []
        for item in buckets.values():
            item["count"] = item["current"] + item["previous"]
            item["delta"] = item["current"] - item["previous"]
            item["trend"] = "rising" if item["delta"] > 0 else "declining" if item["delta"] < 0 else "steady"
            items.append(item)
        result[dimension] = sorted(items, key=lambda item: (item["current"], item["count"]), reverse=True)[:12]
    return result


def _hidden_market_outcome_rates(profile_id=None, include_all_profiles=False):
    leads = list_hidden_market_leads(profile_id, include_all_profiles)
    totals, positive = Counter(), Counter()
    for lead in leads:
        target_type = lead.get("target_type") or "target"
        totals[target_type] += 1
        if lead.get("outcome") in {"replied", "meeting", "converted"}:
            positive[target_type] += 1
    return {key: round(positive[key] / total * 100) if total else 0 for key, total in totals.items()}


def _market_evidence(row, title, score, scraped):
    try:
        contacts = json.loads(row["contact_records_json"] or "[]")
    except (TypeError, ValueError, IndexError):
        contacts = []
    return {
        "job_id": row["id"], "title": title, "company": row["company"], "score": score,
        "seen": scraped, "url": row["url"], "application_url": row["application_url"],
        "location": row["location"], "source": row["source"],
        "contact_person": row["contact_person"], "contact_email": row["contact_email"],
        "contact_phone": row["contact_phone"],
        "contacts": contacts,
    }


def save_market_intelligence_snapshot(profile_id, include_all_profiles, window_days, payload):
    scope_key = "all" if include_all_profiles else f"profile:{int(profile_id or 1)}"
    summary = {
        "generated_at": payload.get("generated_at"),
        "target_counts": {key: len(payload.get(key) or []) for key in ("recruiters", "direct_employers", "leadership_gaps")},
        "signals": payload.get("signals") or {},
    }
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO market_intelligence_snapshots
                (scope_key, profile_id, window_days, snapshot_date, payload_json)
            VALUES (?, ?, ?, date('now'), ?)
            ON CONFLICT(scope_key, window_days, snapshot_date) DO UPDATE SET
                payload_json = excluded.payload_json, created_at = datetime('now')
            """,
            (scope_key, None if include_all_profiles else int(profile_id or 1), int(window_days), json.dumps(summary, ensure_ascii=False)),
        )
        conn.commit()


def get_market_intelligence_snapshot_history(profile_id, include_all_profiles, window_days, limit=30):
    scope_key = "all" if include_all_profiles else f"profile:{int(profile_id or 1)}"
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date, payload_json FROM market_intelligence_snapshots
            WHERE scope_key = ? AND window_days = ?
            ORDER BY snapshot_date DESC LIMIT ?
            """,
            (scope_key, int(window_days), int(limit)),
        ).fetchall()
    history = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        history.append({"date": row["snapshot_date"], **(payload.get("target_counts") or {})})
    return history


def get_hidden_market_intel(profile_id=None, include_all_profiles=False, days=60, limit=12):
    days = max(14, int(days or 60))
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    midpoint = (datetime.now() - timedelta(days=max(7, days // 2))).isoformat(timespec="seconds")
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.advertiser_company, jobs.actual_company,
                   jobs.employer_type, jobs.match_score, jobs.composite_score, jobs.pipeline_stage,
                   jobs.url, jobs.application_url, jobs.contact_person, jobs.contact_email,
                   jobs.contact_phone, jobs.contact_records_json,
                   COALESCE(jobs.date_scraped, jobs.updated_at) AS scraped_at,
                   jobs.location, jobs.salary, jobs.source, postings.job_intelligence_json,
                   SUBSTR(LOWER(COALESCE(jobs.description, '')), 1, 4000) AS description_head
            FROM jobs
            LEFT JOIN job_postings AS postings ON postings.url = jobs.url
            WHERE COALESCE(jobs.date_scraped, jobs.updated_at) >= ?
            {profile_clause}
            """,
            [since] + params,
        ).fetchall()

    recruiters = {}
    employers = {}
    employer_roles = {}
    outcome_rates = _hidden_market_outcome_rates(profile_id, include_all_profiles)
    for row in rows:
        title = str(row["title"] or "")
        title_lower = title.lower()
        score = int(row["composite_score"] or row["match_score"] or 0)
        leadership_title = bool(_matched_terms(title_lower, CAMPAIGN_LEADERSHIP_TERMS))
        tech_title = bool(_role_tokens(title) & BROAD_RELEVANT_TITLES)
        relevant = score >= 50 or leadership_title
        scraped = str(row["scraped_at"] or "")
        period = _market_period(scraped, midpoint)

        # Cross-check identity against everything the advert offers, not just
        # the scrape-time classifier: agency-sounding names and recruiter
        # language in the ad text disqualify; a contact/application domain
        # that corroborates the name is the strongest confirmation.
        advertiser_name = _clean(row["advertiser_company"] or row["company"]) or "Unknown agency"
        employer_name = _clean(row["actual_company"] if not _is_weak_company_candidate(row["actual_company"]) else row["company"])
        description_head = str(row["description_head"] or "")
        recruiter_language = any(phrase in description_head for phrase in RECRUITER_PHRASES)
        domain = _hidden_market_domain(row)
        domain_confirms = _name_matches_domain(employer_name, domain)

        is_recruiter_row = (
            row["employer_type"] == "recruiter"
            or _agency_like_name(advertiser_name)
            or _agency_like_name(employer_name)
            or (recruiter_language and not domain_confirms)
        )
        verified_direct = (
            not is_recruiter_row
            and _plausible_org_name(employer_name)
            and (
                domain_confirms
                or (not domain and row["employer_type"] == "direct_employer" and not recruiter_language)
            )
        )

        # Recruiter ledger: agencies repeatedly carrying relevant roles. They
        # see the unadvertised mandates first — a warm consultant beats a
        # cold application every time.
        if is_recruiter_row and relevant:
            name = advertiser_name
            recruiter_key = f"domain:{domain}" if domain and _agency_like_name(name) else _company_key(name)
            entry = recruiters.setdefault(recruiter_key, {
                "name": name, "roles": 0, "best_score": 0, "last_seen": "",
                "entity_key": recruiter_key,
                "contact_person": "", "contact_email": "", "contact_phone": "", "domain": domain,
                "sample_titles": [], "evidence": [], "classification_reasons": [], "counter_evidence": [],
                "confidence": "medium", "aliases": [], "_current": 0, "_previous": 0,
            })
            entry["roles"] += 1
            entry[f"_{period}"] += 1
            entry["best_score"] = max(entry["best_score"], score)
            entry["last_seen"] = max(entry["last_seen"], scraped)
            if name not in entry["aliases"]:
                entry["aliases"].append(name)
            reasons = []
            if row["employer_type"] == "recruiter": reasons.append("scrape-time recruiter classification")
            if _agency_like_name(name): reasons.append("agency-like identity")
            if recruiter_language: reasons.append("recruiter language in advertisement")
            entry["classification_reasons"] = sorted(set(entry["classification_reasons"] + reasons))
            if domain_confirms:
                entry["counter_evidence"] = sorted(set(entry["counter_evidence"] + ["contact domain resembles the named organisation"]))
            entry["confidence"] = "high" if domain and reasons else "medium" if len(reasons) >= 2 else "low"
            for field in ("contact_person", "contact_email", "contact_phone"):
                if not entry[field] and row[field]:
                    entry[field] = _clean(str(row[field]))
            if title and title not in entry["sample_titles"]:
                entry["sample_titles"] = (entry["sample_titles"] + [title])[:3]
            entry["evidence"].append(_market_evidence(row, title, score, scraped))
            continue

        # Direct-employer watchlist: only identities the advert itself
        # corroborates. Organisations that have hired this role family hire
        # it again — and usually try the hidden channels first.
        employer_key = f"domain:{domain}" if domain_confirms else _company_key(employer_name)
        if verified_direct and employer_key and employer_key != "unknown":
            if relevant:
                entry = employers.setdefault(employer_key, {
                    "name": employer_name, "roles": 0, "best_score": 0, "last_seen": "",
                    "entity_key": employer_key,
                    "domain": "", "sample_titles": [], "locations": [], "verified": "ad signals",
                    "evidence": [], "classification_reasons": [], "counter_evidence": [], "aliases": [],
                    "confidence": "medium", "_current": 0, "_previous": 0,
                })
                entry["roles"] += 1
                entry[f"_{period}"] += 1
                entry["best_score"] = max(entry["best_score"], score)
                entry["last_seen"] = max(entry["last_seen"], scraped)
                entry["domain"] = entry["domain"] or domain
                if domain_confirms:
                    entry["verified"] = "contact domain"
                    entry["confidence"] = "high"
                    entry["classification_reasons"] = sorted(set(entry["classification_reasons"] + ["contact or application domain corroborates employer identity"]))
                else:
                    entry["classification_reasons"] = sorted(set(entry["classification_reasons"] + ["direct-employer advertisement signals"]))
                    entry["counter_evidence"] = sorted(set(entry["counter_evidence"] + ["no corroborating organisation domain captured"]))
                if employer_name not in entry["aliases"]:
                    entry["aliases"].append(employer_name)
                if title and title not in entry["sample_titles"]:
                    entry["sample_titles"] = (entry["sample_titles"] + [title])[:3]
                location = _clean(str(row["location"] or ""))
                if location and location not in entry["locations"]:
                    entry["locations"] = (entry["locations"] + [location])[:2]
                entry["evidence"].append(_market_evidence(row, title, score, scraped))

            # Leadership-gap detection input: track IC-vs-leadership postings
            # per verified direct employer regardless of personal fit score.
            if tech_title:
                bucket = employer_roles.setdefault(employer_key, {
                    "name": employer_name, "ic_titles": [], "lead_count": 0,
                    "entity_key": employer_key,
                    "last_seen": "", "domain": "", "evidence": [], "sources": [],
                    "_current": 0, "_previous": 0,
                })
                bucket["last_seen"] = max(bucket["last_seen"], scraped)
                bucket["domain"] = bucket["domain"] or domain
                if leadership_title:
                    bucket["lead_count"] += 1
                elif title not in bucket["ic_titles"]:
                    bucket["ic_titles"].append(title)
                    bucket[f"_{period}"] += 1
                    bucket["evidence"].append(_market_evidence(row, title, score, scraped))
                    if row["source"] and row["source"] not in bucket["sources"]:
                        bucket["sources"].append(row["source"])

    leadership_gaps = [
        {
            "name": bucket["name"],
            "entity_key": bucket["entity_key"],
            "ic_count": len(bucket["ic_titles"]),
            "sample_titles": bucket["ic_titles"][:4],
            "last_seen": bucket["last_seen"],
            "domain": bucket["domain"],
            "best_score": max((item.get("score") or 0 for item in bucket["evidence"]), default=0),
            "evidence": bucket["evidence"],
            "confidence": "high" if len(bucket["ic_titles"]) >= 4 and bucket["domain"] and len(bucket["sources"]) >= 2 else "medium" if bucket["domain"] else "low",
            "classification_reasons": [f"{len(bucket['ic_titles'])} technical individual-contributor roles observed", "no leadership title observed in the selected window"],
            "counter_evidence": (["Evidence comes from only one source"] if len(bucket["sources"]) < 2 else []) + ["Absence of an advertised leader does not prove an unadvertised vacancy"],
            "_current": bucket["_current"], "_previous": bucket["_previous"],
        }
        for bucket in employer_roles.values()
        if len(bucket["ic_titles"]) >= 2 and bucket["lead_count"] == 0
    ]

    recruiter_items = [_finalise_market_target(item, "recruiter", outcome_rates) for item in recruiters.values()]
    employer_items = [_finalise_market_target(item, "direct_employer", outcome_rates) for item in employers.values()]
    gap_items = [_finalise_market_target(item, "leadership_gap", outcome_rates) for item in leadership_gaps]

    # Tier 3 aggregates — single pass over the rows already in memory.
    momentum = {}
    sources = {}
    for row in rows:
        scraped = str(row["scraped_at"] or "")
        period = _market_period(scraped, midpoint)
        score = int(row["composite_score"] or row["match_score"] or 0)

        name = _clean(row["actual_company"] if not _is_weak_company_candidate(row["actual_company"]) else row["company"])
        key = _company_key(name)
        if name and key and key != "unknown":
            bucket = momentum.setdefault(key, {"employer": name, "roles": 0, "_current": 0, "_previous": 0, "best_score": 0, "last_seen": ""})
            bucket["roles"] += 1
            bucket[f"_{period}"] += 1
            bucket["best_score"] = max(bucket["best_score"], score)
            bucket["last_seen"] = max(bucket["last_seen"], scraped)

        source = _clean(row["source"]) or "unknown"
        srow = sources.setdefault(source, {"source": source, "roles": 0, "_score_sum": 0, "high_fit": 0})
        srow["roles"] += 1
        srow["_score_sum"] += score
        if score >= 70:
            srow["high_fit"] += 1

    employer_momentum = sorted(
        (
            {
                "employer": b["employer"], "roles": b["roles"],
                "current": b["_current"], "previous": b["_previous"],
                "delta": b["_current"] - b["_previous"],
                "best_score": b["best_score"], "last_seen": b["last_seen"],
            }
            for b in momentum.values() if b["roles"] >= 2
        ),
        key=lambda item: (item["delta"], item["current"], item["roles"]), reverse=True,
    )[:limit]

    source_roi = sorted(
        (
            {
                "source": s["source"], "roles": s["roles"],
                "avg_score": round(s["_score_sum"] / s["roles"]) if s["roles"] else 0,
                "high_fit": s["high_fit"],
                "high_fit_rate": round(s["high_fit"] / s["roles"] * 100) if s["roles"] else 0,
            }
            for s in sources.values() if s["roles"] >= 1
        ),
        key=lambda item: (item["avg_score"], item["high_fit"]), reverse=True,
    )

    row_count = len(rows)
    coverage = {
        "structured_role_data": round(sum(1 for row in rows if _market_job_intelligence(row)) / row_count * 100) if row_count else 0,
        "contact": round(sum(1 for row in rows if row["contact_person"] or row["contact_email"] or row["contact_phone"]) / row_count * 100) if row_count else 0,
    }
    payload = {
        "window_days": days,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "freshness": {"as_of": datetime.now().isoformat(timespec="seconds"), "jobs_considered": row_count, "window_start": since, "comparison_split": midpoint, "coverage": coverage},
        "signals": _market_signal_rows(rows, midpoint),
        "employer_momentum": employer_momentum,
        "source_roi": source_roi,
        "outcome_rates": outcome_rates,
        "recruiters": sorted(recruiter_items, key=lambda item: (item["opportunity_score"], item["last_seen"]), reverse=True)[:limit],
        "direct_employers": sorted(employer_items, key=lambda item: (item["opportunity_score"], item["last_seen"]), reverse=True)[:limit],
        "leadership_gaps": sorted(gap_items, key=lambda item: (item["opportunity_score"], item["last_seen"]), reverse=True)[:limit],
    }
    save_market_intelligence_snapshot(profile_id, include_all_profiles, days, payload)
    payload["snapshot_history"] = get_market_intelligence_snapshot_history(profile_id, include_all_profiles, days)
    return payload


HIDDEN_MARKET_STATUSES = ("todo", "contacted", "awaiting", "done")


def hidden_market_target_key(target_type, name, entity_key=None):
    identity = str(entity_key or "").strip().lower() or _company_key(name)
    return f"{target_type}:{identity}"


def _hidden_market_strategy_row(row):
    if not row:
        return None
    data = dict(row)
    try:
        data["strategy"] = json.loads(data.pop("strategy_json") or "{}")
    except (TypeError, ValueError):
        data["strategy"] = {}
    return data


def get_hidden_market_strategy(profile_id, target_type, target_key):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM hidden_market_strategies WHERE profile_id = ? AND target_type = ? AND target_key = ?",
            (int(profile_id or 1), target_type, target_key),
        ).fetchone()
    return _hidden_market_strategy_row(row)


def list_hidden_market_strategies(profile_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM hidden_market_strategies WHERE profile_id = ? ORDER BY updated_at DESC",
            (int(profile_id or 1),),
        ).fetchall()
    return [_hidden_market_strategy_row(row) for row in rows]


def _contact_research_row(row):
    if not row:
        return None
    data = dict(row)
    try:
        data["research"] = json.loads(data.pop("research_json") or "{}")
    except (TypeError, ValueError):
        data["research"] = {}
    data["research"]["selected_candidate_id"] = data.get("selected_candidate_id")
    if data.get("selected_candidate_id"):
        data["research"]["requires_selection"] = False
    return data


def get_hidden_market_contact_research(profile_id, target_type, target_key):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM hidden_market_contact_research WHERE profile_id = ? AND target_type = ? AND target_key = ?",
            (int(profile_id or 1), target_type, target_key),
        ).fetchone()
    return _contact_research_row(row)


def list_hidden_market_contact_research(profile_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM hidden_market_contact_research WHERE profile_id = ? ORDER BY researched_at DESC",
            (int(profile_id or 1),),
        ).fetchall()
    return [_contact_research_row(row) for row in rows]


def save_hidden_market_contact_research(profile_id, target_type, target_key, target_name, research):
    research = dict(research or {})
    selected = research.get("selected_candidate_id")
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO hidden_market_contact_research
                (profile_id, target_type, target_key, target_name, research_json, selected_candidate_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, target_type, target_key) DO UPDATE SET
                target_name = excluded.target_name,
                research_json = excluded.research_json,
                selected_candidate_id = excluded.selected_candidate_id,
                researched_at = datetime('now'), updated_at = datetime('now')
            """,
            (int(profile_id or 1), target_type, target_key, target_name, json.dumps(research, ensure_ascii=False), selected),
        )
        conn.commit()
    return get_hidden_market_contact_research(profile_id, target_type, target_key)


def select_hidden_market_contact(profile_id, target_type, target_key, candidate_id):
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE hidden_market_contact_research
            SET selected_candidate_id = ?, updated_at = datetime('now')
            WHERE profile_id = ? AND target_type = ? AND target_key = ?
            """,
            (candidate_id, int(profile_id or 1), target_type, target_key),
        )
        conn.commit()
    return get_hidden_market_contact_research(profile_id, target_type, target_key)


def save_hidden_market_strategy(profile_id, target_type, target_name, strategy, provider="local", target_key=None):
    target_key = target_key or hidden_market_target_key(target_type, target_name)
    payload = json.dumps(strategy or {}, ensure_ascii=False)
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO hidden_market_strategies
                (profile_id, target_type, target_key, target_name, strategy_json, provider)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, target_type, target_key) DO UPDATE SET
                target_name = excluded.target_name,
                strategy_json = excluded.strategy_json,
                provider = excluded.provider,
                updated_at = datetime('now')
            """,
            (int(profile_id or 1), target_type, target_key, target_name, payload, provider),
        )
        conn.execute(
            """
            UPDATE hidden_market_leads SET strategy_json = ?, updated_at = datetime('now')
            WHERE profile_id = ? AND target_type = ? AND target_key = ?
            """,
            (payload, int(profile_id or 1), target_type, target_key),
        )
        conn.commit()
    return get_hidden_market_strategy(profile_id, target_type, target_key)


def _hidden_market_lead_to_dict(row):
    lead = dict(row)
    try:
        lead["touchpoints"] = json.loads(row["touchpoints"]) if row["touchpoints"] else []
    except (TypeError, ValueError):
        lead["touchpoints"] = []
    try:
        lead["strategy"] = json.loads(row["strategy_json"] or "{}") if row["strategy_json"] else {}
    except (TypeError, ValueError, IndexError):
        lead["strategy"] = {}
    try:
        lead["score_reasons"] = json.loads(row["score_reasons_json"] or "[]") if row["score_reasons_json"] else []
    except (TypeError, ValueError, IndexError):
        lead["score_reasons"] = []
    return lead


def get_hidden_market_lead(lead_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM hidden_market_leads WHERE id = ?", (lead_id,)).fetchone()
        return _hidden_market_lead_to_dict(row) if row else None


def list_hidden_market_leads(profile_id=None, include_all_profiles=False):
    clause, params = _profile_filter_clause(profile_id, include_all_profiles, alias="hidden_market_leads")
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM hidden_market_leads
            WHERE 1=1 {clause}
            ORDER BY
                CASE status WHEN 'done' THEN 1 ELSE 0 END,
                CASE WHEN next_step_date IS NULL OR next_step_date = '' THEN 1 ELSE 0 END,
                next_step_date ASC,
                updated_at DESC
            """,
            params,
        ).fetchall()
        return [_hidden_market_lead_to_dict(row) for row in rows]


def add_hidden_market_lead(profile_id, target_type, target_name, action=None,
                           contact_person=None, contact_email=None, contact_phone=None, domain=None,
                           outreach_channel=None, strategy=None, opportunity_score=None, score_reasons=None,
                           target_key_override=None):
    """Start tracking a hidden-market target. Idempotent on (profile, type, key):
    re-tracking an existing target returns the existing lead untouched."""
    target_type = str(target_type or "").strip() or "target"
    target_name = _clean(str(target_name or "")) or "Unknown target"
    target_key = target_key_override or hidden_market_target_key(target_type, target_name)
    saved_strategy = get_hidden_market_strategy(profile_id, target_type, target_key)
    if not strategy and saved_strategy:
        strategy = saved_strategy.get("strategy") or {}
    if not outreach_channel and strategy:
        outreach_channel = strategy.get("recommended_channel")
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM hidden_market_leads WHERE profile_id = ? AND target_type = ? AND target_key = ?",
            (profile_id, target_type, target_key),
        ).fetchone()
        if existing:
            return _hidden_market_lead_to_dict(existing)
        cursor = conn.execute(
            """
            INSERT INTO hidden_market_leads
                (profile_id, target_type, target_key, target_name, action, status,
                 contact_person, contact_email, contact_phone, domain, outreach_channel,
                 strategy_json, opportunity_score, score_reasons_json, touchpoints, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (profile_id, target_type, target_key, target_name, _clean(str(action or "")) or None,
             _clean(str(contact_person or "")) or None, _clean(str(contact_email or "")) or None,
             _clean(str(contact_phone or "")) or None, _clean(str(domain or "")) or None,
             _clean(str(outreach_channel or "")) or None,
             json.dumps(strategy or {}, ensure_ascii=False) if strategy else None,
             int(opportunity_score or 0) or None,
             json.dumps(score_reasons or [], ensure_ascii=False), now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM hidden_market_leads WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _hidden_market_lead_to_dict(row)


def update_hidden_market_lead(lead_id, updates):
    allowed = {"action", "status", "outcome", "notes", "next_step_date",
               "contact_person", "contact_email", "contact_phone", "domain", "outreach_channel", "strategy_json"}
    fields = {key: value for key, value in (updates or {}).items() if key in allowed}
    if "status" in fields and fields["status"] not in HIDDEN_MARKET_STATUSES:
        raise ValueError(f"Invalid hidden-market status: {fields['status']}")
    if not fields:
        return get_hidden_market_lead(lead_id)
    assignments = ", ".join(f"{key} = ?" for key in fields)
    params = list(fields.values()) + [lead_id]
    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE hidden_market_leads SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            params,
        )
        conn.commit()
    return get_hidden_market_lead(lead_id)


def add_hidden_market_touchpoint(lead_id, note, status=None, next_step_date=None):
    """Append an interaction to the lead's log. Outreach is iterative, so this
    can be called many times (contact -> wait -> contact again) before 'done'."""
    lead = get_hidden_market_lead(lead_id)
    if not lead:
        raise ValueError("Hidden-market lead not found.")
    touchpoints = lead.get("touchpoints") or []
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": _clean(str(note or "")),
        "status": status if status in HIDDEN_MARKET_STATUSES else None,
        "next_step_date": str(next_step_date)[:10] if next_step_date else None,
    }
    touchpoints.append(entry)
    new_status = status if status in HIDDEN_MARKET_STATUSES else lead.get("status")
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE hidden_market_leads
            SET touchpoints = ?, status = ?, next_step_date = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (json.dumps(touchpoints), new_status, entry["next_step_date"], lead_id),
        )
        conn.commit()
    return get_hidden_market_lead(lead_id)


def delete_hidden_market_lead(lead_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM hidden_market_leads WHERE id = ?", (lead_id,))
        conn.commit()
    return True


def convert_hidden_market_lead_to_job(lead_id):
    """Turn a converted lead into a tracked job straight at the 'applied' stage
    (hidden-market outreach is post-engagement, not pre-triage), and mark the
    lead done/converted with a link to the new job."""
    lead = get_hidden_market_lead(lead_id)
    if not lead:
        raise ValueError("Hidden-market lead not found.")
    if lead.get("converted_job_id"):
        return {"job_id": lead["converted_job_id"], "lead": lead, "already": True}

    profile_id = lead["profile_id"]
    target_name = lead["target_name"]
    role_hint = lead.get("action") or "Hidden-market opportunity"
    title = f"{target_name} — hidden-market lead"
    note_bits = [b for b in [lead.get("action"), lead.get("notes")] if b]
    for tp in lead.get("touchpoints") or []:
        stamp = (tp.get("at") or "")[:10]
        if tp.get("note"):
            note_bits.append(f"[{stamp}] {tp['note']}")
    job_data = {
        "title": title,
        "company": target_name,
        "location": "",
        "url": f"hiddenmarket://{profile_id}/{lead['target_key']}",
        "description": f"Converted hidden-market outreach lead. {role_hint}",
        "pdf_text": "",
        "search_keyword": "hidden market",  # guarantees add_job storage gating passes
        "contact_person": lead.get("contact_person"),
        "contact_email": lead.get("contact_email"),
        "contact_phone": lead.get("contact_phone"),
    }
    add_job(job_data, "Hidden Market", profile_id=profile_id)
    normalized = normalize_job_url(job_data["url"])
    with get_db_connection() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (normalized,)).fetchone()
    job_id = row["id"] if row else None
    if not job_id:
        raise ValueError("Could not create the pipeline job for this lead.")

    update_job_application(job_id, {
        "pipeline_stage": "applied",
        "status": "applied",
        "application_date": datetime.now().date().isoformat(),
        "notes": "\n".join(note_bits) if note_bits else None,
    })
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE hidden_market_leads
            SET status = 'done', outcome = 'converted', converted_job_id = ?,
                converted_at = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (job_id, datetime.now().isoformat(timespec="seconds"), lead_id),
        )
        conn.commit()
    return {"job_id": job_id, "lead": get_hidden_market_lead(lead_id), "already": False}


WARM_CONTACT_ORIGINS = ("contact_research", "company_profile", "manual", "lead")


def _warm_contact_key(name, organisation):
    org = _company_key(organisation or "")
    person = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return f"{org}:{person}" if org else person


def _warm_contact_to_dict(row):
    return dict(row) if row else None


def list_warm_contacts(profile_id=None, organisation=None, limit=500):
    clauses, params = ["1=1"], []
    if profile_id:
        clauses.append("(profile_id IS NULL OR profile_id = ?)")
        params.append(profile_id)
    if organisation:
        clauses.append("organisation_key = ?")
        params.append(_company_key(organisation))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM warm_contacts WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(last_contacted_at, '') DESC, organisation ASC, name ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [_warm_contact_to_dict(row) for row in rows]


def upsert_warm_contact(name, organisation=None, profile_id=None, role_title=None,
                        email=None, phone=None, linkedin_url=None, relationship=None,
                        origin="manual", notes=None):
    """Idempotent on (organisation, name). Later writes fill blanks but never
    overwrite a value already recorded — a scraped guess must not clobber
    something the user typed."""
    name = _clean(str(name or ""))
    if not name:
        raise ValueError("A warm contact needs a name.")
    organisation = _clean(str(organisation or "")) or None
    contact_key = _warm_contact_key(name, organisation)
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO warm_contacts
                (profile_id, contact_key, name, organisation, organisation_key, role_title,
                 email, phone, linkedin_url, relationship, origin, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contact_key) DO UPDATE SET
                profile_id = COALESCE(warm_contacts.profile_id, excluded.profile_id),
                role_title = COALESCE(NULLIF(warm_contacts.role_title, ''), excluded.role_title),
                email = COALESCE(NULLIF(warm_contacts.email, ''), excluded.email),
                phone = COALESCE(NULLIF(warm_contacts.phone, ''), excluded.phone),
                linkedin_url = COALESCE(NULLIF(warm_contacts.linkedin_url, ''), excluded.linkedin_url),
                relationship = COALESCE(NULLIF(warm_contacts.relationship, ''), excluded.relationship),
                notes = COALESCE(NULLIF(warm_contacts.notes, ''), excluded.notes),
                updated_at = excluded.updated_at
            """,
            (profile_id, contact_key, name, organisation, _company_key(organisation or "") or None,
             _clean(str(role_title or "")) or None, _clean(str(email or "")) or None,
             _clean(str(phone or "")) or None, _clean(str(linkedin_url or "")) or None,
             _clean(str(relationship or "")) or None,
             origin if origin in WARM_CONTACT_ORIGINS else "manual",
             _clean(str(notes or "")) or None, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM warm_contacts WHERE contact_key = ?", (contact_key,)).fetchone()
    return _warm_contact_to_dict(row)


def delete_warm_contact(contact_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM warm_contacts WHERE id = ?", (contact_id,))
        conn.commit()
    return True


def seed_warm_contacts(profile_id=1):
    """Populate the contact book from what the app already knows (item 6).

    Two existing sources: contact research already performed against
    hidden-market targets (real named people), and the employer set in
    `company_profiles` (organisations worth an approach, seeded without a named
    person so they surface as gaps to fill). Idempotent — safe to re-run.
    """
    seeded = {"contacts": 0, "organisations": 0}
    for research in list_hidden_market_contact_research(profile_id):
        payload = research.get("research") or {}
        selected_id = research.get("selected_candidate_id") or payload.get("selected_candidate_id")
        for candidate in payload.get("candidates") or []:
            # Only credible people are stored. contact_research scores every
            # candidate; anything below its own 45-point credibility floor is a
            # speculative web hit, not a contact. The selected candidate is
            # always kept regardless of score.
            is_selected = selected_id and candidate.get("candidate_id") == selected_id
            if not is_selected and int(candidate.get("confidence_score") or 0) < 45:
                continue
            name = _clean(str(candidate.get("name") or ""))
            if not name or "@" in name:
                continue
            upsert_warm_contact(
                name,
                organisation=candidate.get("organisation") or research.get("target_name"),
                profile_id=profile_id,
                role_title=candidate.get("role"),
                email=candidate.get("email"),
                phone=candidate.get("phone"),
                linkedin_url=candidate.get("profile_url"),
                relationship="selected contact" if is_selected else None,
                origin="contact_research",
            )
            seeded["contacts"] += 1

    with get_db_connection() as conn:
        employers = conn.execute(
            """
            SELECT display_name, website_domain FROM company_profiles
            WHERE employer_type = 'direct_employer' AND display_name IS NOT NULL
            ORDER BY updated_at DESC LIMIT 200
            """
        ).fetchall()
    for employer in employers:
        organisation = employer["display_name"]
        if not organisation:
            continue
        existing = list_warm_contacts(profile_id, organisation=organisation, limit=1)
        if existing:
            continue
        upsert_warm_contact(
            f"(no contact yet) {organisation}",
            organisation=organisation,
            profile_id=profile_id,
            origin="company_profile",
            notes=f"Seeded from company profile. Domain: {employer['website_domain'] or 'unknown'}.",
        )
        seeded["organisations"] += 1
    return seeded


def get_warm_channel_activity(profile_id=None, include_all_profiles=False, days=7):
    """Whether any warm-channel work happened in the trailing window (item 6).

    Counts lead creation, touchpoints, and conversions — the whole point is that
    zero of all three for a week is the condition worth nudging about, given the
    board channel is where the losses to better-matched candidates happen.
    """
    days = max(1, int(days or 7))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    leads = list_hidden_market_leads(profile_id, include_all_profiles)
    new_leads = sum(1 for lead in leads if str(lead.get("created_at") or "") >= cutoff)
    touchpoints = sum(
        1 for lead in leads for tp in (lead.get("touchpoints") or [])
        if str(tp.get("at") or "") >= cutoff
    )
    conversions = sum(1 for lead in leads if str(lead.get("converted_at") or "") >= cutoff)
    total = new_leads + touchpoints + conversions
    return {
        "window_days": days,
        "new_leads": new_leads,
        "touchpoints": touchpoints,
        "conversions": conversions,
        "total_activity": total,
        "open_leads": sum(1 for lead in leads if (lead.get("status") or "todo") != "done"),
        "tracked_leads": len(leads),
        "idle": total == 0,
    }


def get_hidden_market_stats(profile_id=None, include_all_profiles=False, days=7):
    """Outreach performance for the Stats tab: a snapshot funnel + effectiveness
    rates and market mix, plus period-over-period activity (new leads,
    touchpoints, conversions) for the metric-card deltas.

    All hidden-market timestamps (created_at, touchpoints[].at, converted_at) are
    written as local-time isoformat with seconds precision, so lexicographic
    string comparison against the window bounds is correct."""
    days = max(1, int(days or 7))
    leads = list_hidden_market_leads(profile_id, include_all_profiles)
    intel = get_hidden_market_intel(profile_id, include_all_profiles, days=60)

    now = datetime.now()
    cur_start = (now - timedelta(days=days)).isoformat(timespec="seconds")
    prev_start = (now - timedelta(days=days * 2)).isoformat(timespec="seconds")
    today = now.date().isoformat()

    replied_outcomes = {"replied", "meeting", "converted"}
    status_counts = {status: 0 for status in HIDDEN_MARKET_STATUSES}
    contacted_plus = 0
    replied_plus = 0
    converted_total = 0
    due_followups = 0
    current = {"new_leads": 0, "touchpoints": 0, "conversions": 0}
    previous = {"new_leads": 0, "touchpoints": 0, "conversions": 0}
    type_groups = {}
    channel_groups = {}
    score_groups = {}

    def outcome_group(bucket, key, lead, contacted, positive, converted):
        item = bucket.setdefault(key, {"label": key, "tracked": 0, "contacted": 0, "responses": 0, "meetings": 0, "converted": 0})
        item["tracked"] += 1
        item["contacted"] += 1 if contacted else 0
        item["responses"] += 1 if positive else 0
        item["meetings"] += 1 if lead.get("outcome") in {"meeting", "converted"} else 0
        item["converted"] += 1 if converted else 0

    def bump(bucket_current, bucket_previous, timestamp, key):
        if not timestamp:
            return
        if timestamp >= cur_start:
            bucket_current[key] += 1
        elif prev_start <= timestamp < cur_start:
            bucket_previous[key] += 1

    for lead in leads:
        status = lead.get("status") or "todo"
        status_counts[status] = status_counts.get(status, 0) + 1
        contacted = bool(status != "todo" or lead.get("touchpoints"))
        if contacted:
            contacted_plus += 1
        outcome = lead.get("outcome") or ""
        positive = outcome in replied_outcomes
        if positive:
            replied_plus += 1
        converted = outcome == "converted"
        if converted:
            converted_total += 1
        score = int(lead.get("opportunity_score") or 0)
        score_band = "70+" if score >= 70 else "50-69" if score >= 50 else "<50 / unscored"
        outcome_group(type_groups, (lead.get("target_type") or "target").replace("_", " ").title(), lead, contacted, positive, converted)
        outcome_group(channel_groups, (lead.get("outreach_channel") or "Unspecified").title(), lead, contacted, positive, converted)
        outcome_group(score_groups, score_band, lead, contacted, positive, converted)
        next_step = lead.get("next_step_date")
        if next_step and status != "done" and next_step <= today:
            due_followups += 1

        bump(current, previous, lead.get("created_at"), "new_leads")
        bump(current, previous, lead.get("converted_at"), "conversions")
        for touch in lead.get("touchpoints") or []:
            bump(current, previous, touch.get("at"), "touchpoints")

    tracked = len(leads)
    targets = sum(len(intel.get(section, [])) for section in ("recruiters", "direct_employers", "leadership_gaps"))
    market_mix = {
        "recruiter_carried": len(intel.get("recruiters", [])),
        "direct": len(intel.get("direct_employers", [])),
        "leadership_gaps": len(intel.get("leadership_gaps", [])),
        "targets": targets,
    }
    response_rate = round(replied_plus / contacted_plus * 100) if contacted_plus else 0
    conversion_rate = round(converted_total / tracked * 100) if tracked else 0

    reads = []
    if targets and tracked == 0:
        reads.append(f"{targets} hidden-market targets surfaced but none tracked — the hidden market is untouched.")
    elif targets and tracked / targets < 0.25:
        reads.append(f"Only {tracked} of {targets} surfaced targets are tracked — most of the hidden market is untouched.")
    if contacted_plus >= 5 and replied_plus == 0:
        reads.append(f"{contacted_plus} targets contacted with no replies yet — try a different angle or channel.")
    if due_followups:
        reads.append(f"{due_followups} outreach follow-up{'s' if due_followups != 1 else ''} due now.")
    if converted_total:
        reads.append(f"{converted_total} lead{'s' if converted_total != 1 else ''} converted to applications.")

    def final_groups(groups):
        items = []
        for item in groups.values():
            item["response_rate"] = round(item["responses"] / item["contacted"] * 100) if item["contacted"] else 0
            item["conversion_rate"] = round(item["converted"] / item["tracked"] * 100) if item["tracked"] else 0
            items.append(item)
        return sorted(items, key=lambda item: item["tracked"], reverse=True)

    type_performance = final_groups(type_groups)
    channel_performance = final_groups(channel_groups)
    score_calibration = final_groups(score_groups)
    calibrated = [item for item in score_calibration if item["contacted"] >= 3]
    if len(calibrated) >= 2:
        best_band = max(calibrated, key=lambda item: (item["response_rate"], item["conversion_rate"]))
        reads.append(f"The {best_band['label']} opportunity band currently produces the strongest response signal ({best_band['response_rate']}%).")

    return {
        "window_days": days,
        "funnel": {
            "surfaced": targets,
            "tracked": tracked,
            "contacted_plus": contacted_plus,
            "replied_plus": replied_plus,
            "converted": converted_total,
        },
        "status_counts": status_counts,
        "response_rate": response_rate,
        "conversion_rate": conversion_rate,
        "coverage": {"surfaced": targets, "tracked": tracked, "due_followups": due_followups},
        "market_mix": market_mix,
        "current": current,
        "previous": previous,
        "type_performance": type_performance,
        "channel_performance": channel_performance,
        "score_calibration": score_calibration,
        "reads": reads,
    }
