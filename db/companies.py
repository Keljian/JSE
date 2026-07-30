"""Advertiser-versus-employer classification and the company profile cache.

Split out of database_manager.py, which re-exports everything here.
"""
import sqlite3
import re
import json
from datetime import datetime
from .connection import (
    get_db_connection,
)
from .constants import (
    COMPANY_CANDIDATE_STOPWORDS,
    DIRECT_EMPLOYER_PHRASES,
    KNOWN_RECRUITERS,
    RECRUITER_PHRASES,
)
from .text import (
    _clean,
    _company_key,
    _domain_from_value,
    _email_domains,
)

def _extract_named_company_from_text(text, advertiser):
    body = _clean(str(text or ""))
    patterns = [
        r"(?:at|with|join)\s+([A-Z][A-Za-z0-9&'.\-]+(?:\s+[A-Z][A-Za-z0-9&'.\-]+){0,4})",
        r"About\s+([A-Z][A-Za-z0-9&'.\-]+(?:\s+[A-Z][A-Za-z0-9&'.\-]+){0,4})",
        r"([A-Z][A-Za-z0-9&'.\-]+(?:\s+[A-Z][A-Za-z0-9&'.\-]+){0,4})\s+is\s+(?:a|an|one of|Australia)",
    ]
    advertiser_key = _company_key(advertiser)
    for pattern in patterns:
        for match in re.finditer(pattern, body):
            candidate = _clean(match.group(1)).strip(" .,-")
            while candidate and _company_key(candidate.split()[-1]) in COMPANY_CANDIDATE_STOPWORDS:
                candidate = " ".join(candidate.split()[:-1]).strip(" .,-")
            key = _company_key(candidate)
            if _is_weak_company_candidate(candidate):
                continue
            if advertiser_key and key == advertiser_key:
                continue
            if any(word in key.split() for word in {"role", "opportunity", "responsibilities", "skills"}):
                continue
            return candidate
    return ""


def _is_weak_company_candidate(candidate):
    value = _clean(str(candidate or "")).strip(" .,-:;")
    key = _company_key(value)
    if not key or key in COMPANY_CANDIDATE_STOPWORDS or len(value) < 3:
        return True
    words = key.split()
    if not words:
        return True
    if words[0] in COMPANY_CANDIDATE_STOPWORDS:
        return True
    if len(words) == 1:
        word = words[0]
        if word in COMPANY_CANDIDATE_STOPWORDS:
            return True
        if len(word) < 4 and not value.isupper():
            return True
    if len(words) <= 2 and all(word in COMPANY_CANDIDATE_STOPWORDS for word in words):
        return True
    role_like_words = {"analyst", "assistant", "consultant", "coordinator", "engineer", "manager", "officer", "specialist"}
    company_suffixes = {"group", "holdings", "limited", "ltd", "pty", "services", "solutions"}
    if any(word in role_like_words for word in words) and not any(word in company_suffixes for word in words):
        return True
    return False


def classify_company_intelligence(job_data):
    advertiser = _clean(job_data.get("company")) or "Unknown advertiser"
    company_source_text = "\n".join([
        str(job_data.get("description") or ""),
        str(job_data.get("pdf_text") or ""),
    ])
    description = "\n".join([
        str(job_data.get("title") or ""),
        company_source_text,
    ])
    lower_text = description.lower()
    advertiser_key = _company_key(advertiser)
    email_domains = _email_domains(description)
    contact_domain = _domain_from_value(job_data.get("contact_email"))
    if contact_domain and contact_domain not in email_domains:
        email_domains.append(contact_domain)
    url_domain = _domain_from_value(job_data.get("application_url") or job_data.get("url"))

    recruiter_hits = []
    if advertiser_key in KNOWN_RECRUITERS or any(name in advertiser_key for name in KNOWN_RECRUITERS):
        recruiter_hits.append(f"advertiser is a known recruiter/search firm ({advertiser})")
    recruiter_hits.extend([phrase for phrase in RECRUITER_PHRASES if phrase in lower_text])
    direct_hits = [phrase for phrase in DIRECT_EMPLOYER_PHRASES if phrase in lower_text]
    named_company = _extract_named_company_from_text(company_source_text, advertiser)

    actual_company = advertiser
    employer_type = "direct_employer"
    confidence = "medium"
    questions = []
    risks = []

    if recruiter_hits:
        employer_type = "recruiter"
        actual_company = named_company or "Unknown"
        confidence = "high" if advertiser_key in KNOWN_RECRUITERS or any(name in advertiser_key for name in KNOWN_RECRUITERS) else "medium"
        if actual_company == "Unknown":
            risks.append("Actual employer is not named in the advertisement.")
            questions.append("Ask the recruiter to confirm the end client before tailoring company-specific wording.")
    elif named_company:
        employer_type = "mixed"
        actual_company = named_company
        confidence = "medium"
        questions.append("Confirm whether the named organisation is the actual hiring employer.")

    if email_domains:
        recruiter_domain_signal = any(_company_key(domain.split(".")[0]) in KNOWN_RECRUITERS for domain in email_domains)
        if recruiter_domain_signal and employer_type == "direct_employer":
            employer_type = "mixed"
            confidence = "medium"
            risks.append("Contact email domain looks recruiter-related despite direct-employer style wording.")
        if actual_company == "Unknown" and not recruiter_domain_signal:
            actual_company = email_domains[0].split(".")[0].title()
            confidence = "low"

    if _is_weak_company_candidate(actual_company):
        actual_company = "Unknown"
        confidence = "low" if employer_type != "direct_employer" else confidence
        if "Actual employer is not named in the advertisement." not in risks and employer_type != "direct_employer":
            risks.append("Actual employer is not named in the advertisement.")
        if "Ask the recruiter to confirm the end client before tailoring company-specific wording." not in questions and employer_type != "direct_employer":
            questions.append("Ask the recruiter to confirm the end client before tailoring company-specific wording.")

    if employer_type == "direct_employer":
        angle = f"Use company-specific wording for {advertiser}; the ad appears to be directly from the employer."
    elif employer_type == "recruiter":
        angle = "Keep application wording role- and sector-specific until the end client is confirmed."
    else:
        angle = "Use cautious company wording and avoid assuming the advertiser is the end employer."

    intelligence = {
        "advertiser_company": advertiser,
        "actual_company": actual_company,
        "employer_type": employer_type,
        "confidence": confidence,
        "evidence": {
            "recruiter_signals": sorted(set(recruiter_hits)),
            "direct_employer_signals": sorted(set(direct_hits[:5])),
            "named_company_in_ad": named_company,
            "email_domains": email_domains,
            "application_domain": url_domain,
        },
        "application_angle": angle,
        "risks": risks,
        "questions_to_clarify": questions,
        "summary": (
            f"Advertiser is {advertiser}. "
            f"Classified as {employer_type.replace('_', ' ')} with {confidence} confidence. "
            + (
                "End client has not been identified yet."
                if actual_company == "Unknown" and employer_type != "direct_employer"
                else f"End client / employer: {actual_company}."
            )
        ),
    }
    return {
        "advertiser_company": advertiser,
        "actual_company": actual_company,
        "employer_type": employer_type,
        "company_confidence": confidence,
        "company_intelligence": json.dumps(intelligence, ensure_ascii=False),
        "company_research_updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _profile_row_to_company_intelligence(company, row):
    if not row:
        return company
    try:
        cached = json.loads(row["intelligence"] or "{}")
    except (TypeError, json.JSONDecodeError):
        cached = {}
    try:
        current = json.loads(company.get("company_intelligence") or "{}")
    except (TypeError, json.JSONDecodeError):
        current = {}

    cached_actual = row["display_name"] or cached.get("actual_company") or company.get("actual_company")
    cached_type = row["employer_type"] or cached.get("employer_type") or company.get("employer_type")
    cached_confidence = row["confidence"] or cached.get("confidence") or company.get("company_confidence")
    merged = {
        **current,
        "actual_company": cached_actual,
        "employer_type": cached_type,
        "confidence": cached_confidence,
        "cached_company_profile": {
            "display_name": row["display_name"],
            "updated_at": row["updated_at"],
            "website_domain": row["website_domain"],
        },
    }
    if cached:
        merged.setdefault("ai_research", cached.get("ai_research", cached))
        for key in ("company_summary", "business_context", "application_angle", "recruiter_warning",
                    "questions_to_clarify", "risks"):
            if key in cached and key not in merged:
                merged[key] = cached[key]
    return {
        **company,
        "actual_company": cached_actual,
        "employer_type": cached_type,
        "company_confidence": cached_confidence,
        "company_intelligence": json.dumps(merged, ensure_ascii=False),
    }


def apply_company_profile_cache(company, conn=None):
    """Overlay previously researched company intelligence when a known employer appears again."""
    candidates = []
    for value in (company.get("actual_company"), company.get("advertiser_company")):
        key = _company_key(value)
        if key and key != "unknown" and key not in candidates:
            candidates.append(key)
    if not candidates:
        return company

    placeholders = ",".join("?" for _ in candidates)
    query = f"SELECT * FROM company_profiles WHERE company_key IN ({placeholders}) ORDER BY updated_at DESC LIMIT 1"
    if conn is not None:
        row = conn.execute(query, candidates).fetchone()
        return _profile_row_to_company_intelligence(company, row)
    try:
        with get_db_connection() as lookup_conn:
            row = lookup_conn.execute(query, candidates).fetchone()
            return _profile_row_to_company_intelligence(company, row)
    except sqlite3.Error:
        return company


def company_intelligence_needs_refresh(row):
    if not row:
        return False
    keys = row.keys()
    actual = row["actual_company"] if "actual_company" in keys else ""
    intelligence = row["company_intelligence"] if "company_intelligence" in keys else ""
    if not intelligence:
        return True
    if actual and _is_weak_company_candidate(actual):
        return True
    try:
        data = json.loads(intelligence)
    except (TypeError, json.JSONDecodeError):
        return True
    return _is_weak_company_candidate(data.get("actual_company"))
