"""SQLite persistence and query helpers for the desktop assistant.

This module owns schema-safe database access, profile/lane settings, scraper
plugin metadata, application memory, campaign planning, and job pipeline CRUD.
It deliberately exposes plain functions because the Python bridge dispatches
one command at a time and serialises the results to JSON for the Electron UI.
"""
import sqlite3
import time
import re
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlparse

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("JSE_DATA_DIR") or APP_ROOT / "settings")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = str(DATA_DIR / "job_applications.db")
LOCAL_LLM_SETTINGS_FILE = DATA_DIR / "local_llm_settings.json"
DEFAULT_APP_SETTINGS = {
    "settings_dir": str(DATA_DIR),
    "applications_dir": str(APP_ROOT / "applications"),
    "older_applications_dir": str(APP_ROOT / "older_applications"),
    "onboarding_completed": False,
    "onboarding_version": 0,
}

PIPELINE_STAGES = [
    "new",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "rejected_by_company",
    "archived",
]

ACTIVE_PRE_APPLICATION_STAGES = ["interested"]
APPLIED_EMPLOYER_DECLINE_DAYS = 50
# Jobs analysed below this score are auto-rejected out of the active pipeline.
# Relaxed June 2026 (was 50) — keep aligned with llm_handler.TRIAGE_KEEP_THRESHOLD.
AUTO_REJECT_THRESHOLD = 45

WORK_MODE_OPTIONS = ["hybrid", "remote", "wfh", "onsite"]
DEFAULT_PROFILE_SETTINGS = {
    "preferred_location": "Melbourne VIC",
    "seek_location": "Melbourne VIC",
    "linkedin_location": "Melbourne VIC",
    "work_modes": ["hybrid", "remote", "wfh", "onsite"],
    "max_pages": 30,
    "default_min_score": 60,
    "boost_terms": "",
    "penalty_terms": "",
    "doc_ai_provider": "local",
    "doc_ai_model": "",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "claude_api_key": "",
    "claude_model": "claude-sonnet-4-6",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.1-pro-preview",
    "local_base_url": "http://localhost:1234/v1",
    "local_api_key": "",
    "local_model": "",
    "resume_template_path": "",
    "cover_letter_template_path": "",
    "lane_intent": "",
    "target_titles": "",
    "target_domains": "",
    "seniority": "",
    "must_have_terms": "",
    "avoid_terms": "",
    "document_strategy": "",
    "active": 1,
}
# API keys are account-level credentials, not per-lane preferences. A key entered
# on any lane is shared by every lane (see _get_global_credentials / propagation
# in update_profile_settings) so document generation works regardless of which
# lane is active.
GLOBAL_CREDENTIAL_FIELDS = ("openai_api_key", "claude_api_key", "gemini_api_key", "local_api_key")
DEFAULT_APP_SETTINGS.update({
    "doc_ai_provider": DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
    # Blank document/research values inherit the legacy provider until the user
    # makes an explicit per-workflow selection. Memory remains local by default.
    "document_ai_provider": "",
    "research_ai_provider": "",
    "memory_ai_provider": DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
    "doc_ai_model": DEFAULT_PROFILE_SETTINGS["doc_ai_model"],
    "openai_api_key": "",
    "openai_base_url": DEFAULT_PROFILE_SETTINGS["openai_base_url"],
    "claude_api_key": "",
    "claude_model": DEFAULT_PROFILE_SETTINGS["claude_model"],
    "gemini_api_key": "",
    "gemini_model": DEFAULT_PROFILE_SETTINGS["gemini_model"],
    "local_base_url": DEFAULT_PROFILE_SETTINGS["local_base_url"],
    "local_api_key": "",
    "local_model": DEFAULT_PROFILE_SETTINGS["local_model"],
    # Job-matching (triage/scoring/analysis) provider. Defaults to local so
    # behaviour is unchanged; scoring_model is an independent model override for
    # this workflow. Free / OpenAI-compatible endpoint credentials (Groq,
    # Cerebras, OpenRouter, OpenCode Zen, custom) live under compat_*.
    "scoring_ai_provider": "local",
    "scoring_model": "",
    "compat_base_url": "",
    "compat_api_key": "",
    "compat_model": "",
})
SOURCE_ALIASES = {
    "seek": "Seek",
    "seek.com.au": "Seek",
    "linkedin": "LinkedIn",
    "deakin": "Deakin University",
    "deakin university": "Deakin University",
    "monash": "Monash University",
    "monash university": "Monash University",
    "latrobe": "LaTrobe University",
    "latrobe university": "LaTrobe University",
    "la trobe": "LaTrobe University",
    "la trobe university": "LaTrobe University",
    "swinburne": "Swinburne University",
    "swinburne university": "Swinburne University",
    "knox": "Knox City Council",
    "knox city council": "Knox City Council",
    "maroondah": "Maroondah City Council",
    "maroondah city council": "Maroondah City Council",
}
# Sources whose jobs skip the broad-feed plausibility pre-filter: keyword
# searches are already targeted, and manual adds are intentional by definition.
KEYWORD_FILTERED_SOURCES = {"manual"}
ROLE_STOPWORDS = {
    "and", "or", "the", "for", "with", "role", "jobs", "job", "position", "senior", "junior",
    "lead", "head", "chief", "principal", "officer", "advisor", "adviser", "specialist",
    "consultant", "manager", "coordinator", "administrator", "assistant", "executive",
    "melbourne", "victoria", "australia", "vic",
}
BROAD_RELEVANT_TITLES = {
    "application", "applications", "analyst", "architecture", "automation", "business",
    "change", "cloud", "commercial", "compliance", "continuous", "customer", "cyber",
    "data", "delivery", "digital", "enablement", "enterprise", "governance", "ict",
    "implementation", "information", "innovation", "integration", "it", "leadership",
    "operations", "portfolio", "process", "product", "program", "programme", "project",
    "quality", "risk", "service", "software", "solution", "solutions", "stakeholder",
    "strategy", "systems", "technical", "technology", "transformation", "vendor",
}
BROAD_UNRELATED_TITLES = {
    "apprentice", "barista", "bartender", "carer", "chef", "childcare", "cleaner",
    "cook", "dentist", "doctor", "driver", "educator", "electrician", "gardener",
    "hospitality", "labourer", "lifeguard", "mechanic", "nurse", "pharmacist",
    "plumber", "receptionist", "retail", "security", "surgeon", "teacher", "vet",
    "waiter", "waitress", "warehouse",
}

KNOWN_RECRUITERS = {
    "accent group recruitment", "adecco", "ambition", "ashdown people", "bluefin resources",
    "charterhouse", "circuit recruitment", "davidson", "deloitte recruitment", "finite",
    "halcyon knights", "hays", "hudson", "ignite", "korn ferry", "michael page",
    "page executive", "paxus", "peoplebank", "randstad", "robert half", "sharp & carter",
    "six degrees executive", "talent", "talent international", "talent – specialists in tech, transformation & beyond",
    "the network", "u&u", "underwood executive", "vertical talent", "west recruitment",
    "zone IT solutions", "horizontal talent", "hamilton barnes", "pacific search",
}

RECRUITER_PHRASES = [
    "our client", "we are partnering", "we're partnering", "on behalf of", "confidential client",
    "client is seeking", "client are seeking", "recruitment consultant", "advising consultant",
    "specialists in tech", "staffing", "recruiting", "recruitment agency", "executive search",
]

DIRECT_EMPLOYER_PHRASES = [
    "about us", "about the company", "our organisation", "our organization", "our team",
    "we are seeking", "we're seeking", "join us", "life at", "our values",
]

COMPANY_CANDIDATE_STOPWORDS = {
    "a", "about", "about us", "about you", "all", "and", "are", "as", "at", "business",
    "candidate", "client", "company", "confidential", "department", "employer", "for",
    "group", "here", "hiring", "if", "in", "it", "its", "join", "key responsibilities",
    "new", "our", "people", "position", "responsibilities", "role", "team", "the",
    "the company", "the opportunity", "the role", "their", "this", "this role", "we",
    "what you", "who", "with", "work", "you", "your",
}

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def make_analysis_signature(resume_text, description, pdf_text="", position_description_text=""):
    payload = "\n\n".join([
        str(resume_text or ""),
        str(description or ""),
        str(pdf_text or ""),
        str(position_description_text or ""),
    ])
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def normalize_job_url(url):
    value = _clean(str(url or ""))
    if not value:
        return value
    value = value.split("#", 1)[0]
    if "?" not in value:
        return value.rstrip("/")
    base, query = value.split("?", 1)
    # Keep query parameters that identify the vacancy itself while discarding
    # marketing/referral parameters.  A number of ATS platforms use a generic
    # path (for example ``/OpportunityDetail``) and put the only job identity
    # in the query string; stripping those values both breaks the application
    # URL and makes unrelated vacancies look like duplicates.
    identity_params = {
        "id", "job", "jobid", "job_id", "jobkey", "job_key", "jobno",
        "job_no", "jobnumber", "job_number", "jid", "rid", "reqid",
        "req_id", "requisitionid", "requisition_id", "requisitionnumber",
        "requisition_number", "opportunityid", "opportunity_id",
        "postingid", "posting_id", "jobpostingid", "jobposting_id",
        "positionid", "position_id", "vacancyid", "vacancy_id",
        "openingid", "opening_id", "reference", "refno", "ref_no",
        "gh_jid", "career_job_req_id",
    }
    # SuccessFactors uses a shared /career endpoint.  The company tenant and
    # route selector are required for the direct application link to work.
    host = (urlparse(value).hostname or "").lower()
    if host == "successfactors.com" or host.endswith(".successfactors.com"):
        identity_params.update({"company", "career_ns"})
    keep_params = []
    for part in query.split("&"):
        key = part.split("=", 1)[0].lower()
        if key in identity_params:
            keep_params.append(part)
    return (base + ("?" + "&".join(sorted(keep_params)) if keep_params else "")).rstrip("/")


def description_fingerprint(description):
    text = str(description or "")
    normalized = re.sub(r"https?://\S+", " ", text.lower())
    normalized = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if len(normalized) < 120:
        return None
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def _split_csv(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _company_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _dedupe_text_key(value):
    text = str(value or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(pty|ltd|limited|australia|australian|vic|v ic)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _job_identity_key(title, company):
    return (_dedupe_text_key(title), _dedupe_text_key(company))


def _is_meaningful_job_identity(title, company):
    title_key, company_key = _job_identity_key(title, company)
    return len(title_key) >= 5 and len(company_key) >= 3


def _find_existing_equivalent_job(conn, profile_id, title, company):
    if not _is_meaningful_job_identity(title, company):
        return None
    title_key, company_key = _job_identity_key(title, company)
    rows = conn.execute(
        """
        SELECT id, title, company, pipeline_stage, status
        FROM jobs
        WHERE profile_id = ?
        AND pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
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


def _domain_from_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text and not text.lower().startswith(("http://", "https://")):
        return text.rsplit("@", 1)[-1].lower().strip(" .,;)")
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.netloc or parsed.path).lower()
    return re.sub(r"^www\.", "", host).split("/", 1)[0]


def _email_domains(text):
    return sorted({
        match.rsplit("@", 1)[-1].lower()
        for match in re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", str(text or ""))
    })


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


def normalize_source(source):
    value = _clean(str(source or ""))
    return SOURCE_ALIASES.get(value.lower(), value)


def source_aliases(source):
    canonical = normalize_source(source)
    aliases = {canonical}
    for alias, target in SOURCE_ALIASES.items():
        if target == canonical:
            aliases.add(alias)
            aliases.add(alias.upper())
            aliases.add(alias.title())
    return sorted(aliases)


def location_aliases(location):
    value = _clean(str(location or ""))
    if not value:
        return []
    lower = value.lower()
    aliases = {value}
    if "melbourne" in lower or lower in {"vic", "victoria"} or " vic" in lower or "victoria" in lower:
        aliases.update(["Melbourne", "Melbourne VIC", "Melbourne, Victoria", "Victoria", "VIC"])
    if "sydney" in lower or lower in {"nsw", "new south wales"} or " nsw" in lower or "new south wales" in lower:
        aliases.update(["Sydney", "Sydney NSW", "Sydney, New South Wales", "New South Wales", "NSW"])
    if "brisbane" in lower or lower in {"qld", "queensland"} or " qld" in lower or "queensland" in lower:
        aliases.update(["Brisbane", "Brisbane QLD", "Brisbane, Queensland", "Queensland", "QLD"])
    if "adelaide" in lower or lower in {"sa", "south australia"} or " south australia" in lower:
        aliases.update(["Adelaide", "Adelaide SA", "Adelaide, South Australia", "South Australia"])
    if "perth" in lower or lower in {"wa", "western australia"} or " western australia" in lower:
        aliases.update(["Perth", "Perth WA", "Perth, Western Australia", "Western Australia"])
    if "canberra" in lower or lower in {"act", "australian capital territory"} or "capital territory" in lower:
        aliases.update(["Canberra", "ACT", "Australian Capital Territory"])
    return sorted(aliases)


def _role_tokens(value):
    tokens = re.findall(r"[a-zA-Z][a-zA-Z+.#-]{2,}", str(value or "").lower())
    return {token.replace("-", "") for token in tokens if token not in ROLE_STOPWORDS}


# Google retired the Gemini 1.x/2.0 families; calling them 404s. The db_setup
# migration rewrites stored names at launch, and this sanitiser is the runtime
# belt-and-braces so a stale name can never reach an API call.
RETIRED_GEMINI_MODELS = {"gemini-pro", "gemini-pro-vision"}
RETIRED_GEMINI_MODEL_PREFIXES = ("gemini-1.0", "gemini-1.5", "gemini-2.0")
RETIRED_CLAUDE_MODELS = {"claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022"}


def sanitize_gemini_model(value):
    model = _clean(str(value or ""))
    lowered = model.lower()
    if not model or lowered in RETIRED_GEMINI_MODELS or lowered.startswith(RETIRED_GEMINI_MODEL_PREFIXES):
        return DEFAULT_PROFILE_SETTINGS["gemini_model"]
    return model


def sanitize_claude_model(value):
    model = _clean(str(value or ""))
    if not model or model.lower() in RETIRED_CLAUDE_MODELS:
        return DEFAULT_PROFILE_SETTINGS["claude_model"]
    return model


def _settings_from_profile(row):
    if not row:
        return dict(DEFAULT_PROFILE_SETTINGS)
    settings = dict(DEFAULT_PROFILE_SETTINGS)
    for key in ("preferred_location", "seek_location", "linkedin_location", "doc_ai_provider",
                "doc_ai_model", "openai_api_key", "openai_base_url", "claude_api_key",
                "claude_model", "gemini_api_key", "gemini_model", "local_model",
                "resume_template_path", "cover_letter_template_path", "lane_intent", "target_titles",
                "target_domains", "seniority", "must_have_terms", "avoid_terms", "document_strategy"):
        if key in row.keys():
            settings[key] = row[key] or settings[key]
    if "active" in row.keys():
        settings["active"] = 1 if row["active"] is None else int(row["active"])
    settings["work_modes"] = _split_csv(row["work_modes"]) or list(DEFAULT_PROFILE_SETTINGS["work_modes"])
    settings["max_pages"] = int(row["max_pages"] or DEFAULT_PROFILE_SETTINGS["max_pages"])
    settings["default_min_score"] = int(row["default_min_score"] or DEFAULT_PROFILE_SETTINGS["default_min_score"])
    settings["boost_terms"] = row["boost_terms"] or ""
    settings["penalty_terms"] = row["penalty_terms"] or ""
    settings["gemini_model"] = sanitize_gemini_model(settings.get("gemini_model"))
    settings["claude_model"] = sanitize_claude_model(settings.get("claude_model"))
    return settings


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


def _default_closing_date():
    return (datetime.now() + timedelta(days=14)).date().isoformat()


def _parse_date_parts(day, month, year=None):
    day = int(day)
    month = MONTHS[str(month).lower()[:3]] if str(month).isalpha() else int(month)
    year = int(year or datetime.now().year)
    if year < 100:
        year += 2000
    return datetime(year, month, day).date().isoformat()


def _extract_explicit_closing_date(text):
    value = _clean(str(text or ""))
    keyword = r"(?:applications?(?:\s+(?:will\s+)?)?(?:close|closing)|closing\s+date|closes|apply\s+by|submitted\s+by)"
    month_names = "|".join(MONTHS)
    patterns = [
        rf"{keyword}.{{0,90}}?(?:\w+day,?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_names})\s*,?\s*(\d{{4}})?",
        rf"{keyword}.{{0,90}}?(?:\w+day,?\s+)?({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?",
        rf"{keyword}.{{0,90}}?(\d{{1,2}})[/-](\d{{1,2}})[/-](\d{{2,4}})",
    ]
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            try:
                if index == 1:
                    return _parse_date_parts(match.group(2), match.group(1), match.group(3))
                return _parse_date_parts(match.group(1), match.group(2), match.group(3))
            except Exception:
                pass
    return ""


def _date_is_past(value):
    if not value:
        return False
    try:
        return datetime.fromisoformat(str(value)[:10]).date() < datetime.now().date()
    except ValueError:
        return False


def _closing_date_is_expired(metadata):
    return metadata.get("closing_date_source") in {"advertisement", "provided"} and _date_is_past(metadata.get("closing_date"))


def extract_job_metadata(job_data):
    """Best-effort extraction of closing date, contact details, and salary from ad text."""
    text = _clean("\n".join([
        str(job_data.get("title") or ""),
        str(job_data.get("company") or ""),
        str(job_data.get("description") or ""),
        str(job_data.get("pdf_text") or ""),
    ]))
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

    phone_match = re.search(r"(\+?61\s?\d[\d\s]{7,12}|0\d[\d\s]{8,12})", text)
    if phone_match:
        metadata.setdefault("contact_phone", _clean(phone_match.group(1)))

    email_match = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", text)
    if email_match:
        metadata.setdefault("contact_email", email_match.group(0))

    contact_patterns = [
        r"(?:contact|enquiries|for further information).*?(?:contact\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        r"please contact\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
    ]
    for pattern in contact_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            name = _clean(match.group(1))
            if not any(word.lower() in {"apply", "email", "phone", "please"} for word in name.split()):
                metadata.setdefault("contact_person", name)
                break

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
    if not assignments:
        assignments = []
    assignments.append("last_seen_at = datetime('now')")
    assignments.append("missing_sweeps = 0")
    assignments.append("updated_at = datetime('now')")
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)


# journal_mode=WAL is persisted in the database header, so it only needs to be
# set once per process rather than on every connection open.
_wal_enabled = False


@contextmanager
def get_db_connection():
    """Context manager for SQLite connections tuned for a large local database.

    WAL is enabled once per process (it persists in the DB header). The other
    PRAGMAs are per-connection and are applied on every open:
      - busy_timeout lets SQLite wait on locks instead of raising immediately
        (the 6-worker scrape path hits contention).
      - synchronous=NORMAL is safe under WAL and is the biggest write speedup;
        only a power loss (not an app crash) risks the last commit.
      - temp_store=MEMORY keeps sorts/temp tables off disk.
      - cache_size (~64MB) and mmap_size (256MB) cut I/O against the ~200MB DB.
    """
    global _wal_enabled
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        if not _wal_enabled:
            conn.execute("PRAGMA journal_mode=WAL")
            _wal_enabled = True
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("PRAGMA mmap_size=268435456")
        yield conn
    finally:
        conn.close()


def ensure_application_context_schema():
    """Add optional application-evidence columns for hot-reloaded workers.

    Normal startup runs db_setup, but development UI reloads and fresh long-task
    processes can briefly run newer code against a still-open older database.
    Keep this targeted migration cheap and idempotent so an optional blank field
    can never block saving or document generation.
    """
    required = {
        "jobs": "additional_candidate_context TEXT",
        "application_kits": "additional_candidate_context TEXT",
    }
    with get_db_connection() as conn:
        for table, definition in required.items():
            columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns or "additional_candidate_context" in columns:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            except sqlite3.OperationalError as exc:
                # Another bridge/task process may have won the same migration.
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()


def compact_database():
    """Checkpoint WAL and VACUUM the SQLite database, returning size stats."""
    db_path = Path(DB_FILE)

    def file_size(path):
        return path.stat().st_size if path.exists() else 0

    def sizes():
        wal_path = db_path.with_name(db_path.name + "-wal")
        shm_path = db_path.with_name(db_path.name + "-shm")
        main = file_size(db_path)
        wal = file_size(wal_path)
        shm = file_size(shm_path)
        return {"main_bytes": main, "wal_bytes": wal, "shm_bytes": shm, "total_bytes": main + wal + shm}

    before = sizes()
    with sqlite3.connect(DB_FILE, timeout=30) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
    after = sizes()
    reclaimed = before["total_bytes"] - after["total_bytes"]
    return {
        "before_bytes": before["total_bytes"],
        "after_bytes": after["total_bytes"],
        "before_main_bytes": before["main_bytes"],
        "after_main_bytes": after["main_bytes"],
        "before_wal_bytes": before["wal_bytes"],
        "after_wal_bytes": after["wal_bytes"],
        "before_shm_bytes": before["shm_bytes"],
        "after_shm_bytes": after["shm_bytes"],
        "reclaimed_bytes": max(0, reclaimed),
        "delta_bytes": after["total_bytes"] - before["total_bytes"],
    }

def _execute_with_retry(conn, query, params, is_commit=False):
    """Executes a query with a retry mechanism for locked databases."""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if is_commit:
                conn.commit()
            return cursor
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 0.1  # Incremental backoff
                    time.sleep(wait_time)
                else:
                    raise e # Re-raise the exception after the last attempt
            else:
                raise e # Re-raise other operational errors immediately


# --- Profile CRUD ---

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


def _get_global_credentials():
    """Returns account-level API keys from app_settings."""
    settings = get_app_settings()
    return {field: str(settings.get(field) or "").strip() for field in GLOBAL_CREDENTIAL_FIELDS}


GLOBAL_AI_SETTING_FIELDS = (
    "doc_ai_provider",
    "document_ai_provider",
    "research_ai_provider",
    "memory_ai_provider",
    "doc_ai_model",
    "openai_api_key",
    "openai_base_url",
    "claude_api_key",
    "claude_model",
    "gemini_api_key",
    "gemini_model",
    "local_base_url",
    "local_api_key",
    "local_model",
    "scoring_ai_provider",
    "scoring_model",
    "compat_base_url",
    "compat_api_key",
    "compat_model",
)
LOCAL_LLM_SETTING_FIELDS = ("local_base_url", "local_api_key", "local_model")


def _normalize_local_base_url(value):
    text = str(value or "").strip().rstrip("/")
    if text.lower() in {"http://localhost:8888/api", "http://127.0.0.1:8888/api"}:
        return f"{text[:-4]}/v1"
    return text


def _get_global_ai_settings():
    settings = get_app_settings()
    return {field: settings.get(field, DEFAULT_APP_SETTINGS.get(field, "")) for field in GLOBAL_AI_SETTING_FIELDS}


def _load_local_llm_settings():
    try:
        if not LOCAL_LLM_SETTINGS_FILE.exists():
            return {}
        data = json.loads(LOCAL_LLM_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: _normalize_local_base_url(data.get(key)) if key == "local_base_url" else str(data.get(key) or "").strip()
        for key in LOCAL_LLM_SETTING_FIELDS
        if key in data
    }


def _save_local_llm_settings(updates):
    current = {
        "local_base_url": DEFAULT_APP_SETTINGS.get("local_base_url", ""),
        "local_api_key": "",
        "local_model": "",
        **_load_local_llm_settings(),
    }
    for key in LOCAL_LLM_SETTING_FIELDS:
        if key not in (updates or {}):
            continue
        value = str(updates.get(key) or "").strip()
        if key == "local_base_url" and not value:
            value = DEFAULT_APP_SETTINGS.get("local_base_url", "")
        if key == "local_base_url":
            value = _normalize_local_base_url(value)
        current[key] = value
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_LLM_SETTINGS_FILE.write_text(
        json.dumps(current, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return current


def get_profile_settings(profile_id):
    """Returns search and filtering preferences for a profile."""
    settings = _settings_from_profile(get_profile_by_id(profile_id))
    # Overlay account-level AI settings so document generation is independent of
    # the active lane and credentials are not duplicated across profile rows.
    settings.update(_get_global_ai_settings())
    settings["gemini_model"] = sanitize_gemini_model(settings.get("gemini_model"))
    settings["claude_model"] = sanitize_claude_model(settings.get("claude_model"))
    return settings


def get_lane_settings(lane_id):
    return get_profile_settings(lane_id)


def update_profile_settings(profile_id, settings):
    """Updates profile-level search preferences."""
    ai_updates = {
        field: settings[field]
        for field in GLOBAL_AI_SETTING_FIELDS
        if isinstance(settings, dict) and field in settings
    }
    if ai_updates:
        update_app_settings(ai_updates)
    current = get_profile_settings(profile_id)
    merged = {**current, **(settings or {})}
    work_modes = [mode for mode in _split_csv(merged.get("work_modes")) if mode in WORK_MODE_OPTIONS]
    if not work_modes:
        work_modes = list(DEFAULT_PROFILE_SETTINGS["work_modes"])
    max_pages = max(1, min(100, int(merged.get("max_pages") or DEFAULT_PROFILE_SETTINGS["max_pages"])))
    default_min_score = max(0, min(100, int(merged.get("default_min_score") or 0)))
    with get_db_connection() as conn:
        _execute_with_retry(
            conn,
            """
            UPDATE profiles
            SET preferred_location = ?,
                seek_location = ?,
                linkedin_location = ?,
                work_modes = ?,
                max_pages = ?,
                default_min_score = ?,
                boost_terms = ?,
                penalty_terms = ?,
                doc_ai_provider = ?,
                doc_ai_model = ?,
                openai_api_key = ?,
                openai_base_url = ?,
                claude_api_key = ?,
                claude_model = ?,
                gemini_api_key = ?,
                gemini_model = ?,
                local_model = ?,
                resume_template_path = ?,
                cover_letter_template_path = ?,
                lane_intent = ?,
                target_titles = ?,
                target_domains = ?,
                seniority = ?,
                must_have_terms = ?,
                avoid_terms = ?,
                document_strategy = ?,
                active = ?
            WHERE id = ?
            """,
            (
                _clean(merged.get("preferred_location")) or DEFAULT_PROFILE_SETTINGS["preferred_location"],
                _clean(merged.get("seek_location")) or DEFAULT_PROFILE_SETTINGS["seek_location"],
                _clean(merged.get("linkedin_location")) or DEFAULT_PROFILE_SETTINGS["linkedin_location"],
                ",".join(work_modes),
                max_pages,
                default_min_score,
                _clean(merged.get("boost_terms")),
                _clean(merged.get("penalty_terms")),
                _clean(merged.get("doc_ai_provider")) or DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
                _clean(merged.get("doc_ai_model")),
                "",
                _clean(merged.get("openai_base_url")) or DEFAULT_PROFILE_SETTINGS["openai_base_url"],
                "",
                _clean(merged.get("claude_model")) or DEFAULT_PROFILE_SETTINGS["claude_model"],
                "",
                _clean(merged.get("gemini_model")) or DEFAULT_PROFILE_SETTINGS["gemini_model"],
                _clean(merged.get("local_model")),
                _clean(merged.get("resume_template_path")) or DEFAULT_PROFILE_SETTINGS["resume_template_path"],
                _clean(merged.get("cover_letter_template_path")) or DEFAULT_PROFILE_SETTINGS["cover_letter_template_path"],
                _clean(merged.get("lane_intent")),
                _clean(merged.get("target_titles")),
                _clean(merged.get("target_domains")),
                _clean(merged.get("seniority")),
                _clean(merged.get("must_have_terms")),
                _clean(merged.get("avoid_terms")),
                _clean(merged.get("document_strategy")),
                1 if merged.get("active", 1) else 0,
                profile_id,
            ),
            is_commit=True,
        )
    return get_profile_settings(profile_id)


def update_lane_settings(lane_id, settings):
    return update_profile_settings(lane_id, settings)


def _app_setting_defaults():
    runtime_root = Path(os.environ.get("JSE_RUNTIME_ROOT") or os.environ.get("JSE_APP_ROOT") or APP_ROOT)
    return {
        **DEFAULT_APP_SETTINGS,
        "settings_dir": str(DATA_DIR),
        "applications_dir": str(runtime_root / "applications"),
        "older_applications_dir": str(runtime_root / "older_applications"),
    }


def _persistent_runtime_path(key, value):
    """Remap only JSE's old default install-tree folders, never custom paths."""
    legacy_root = os.environ.get("JSE_LEGACY_RUNTIME_ROOT")
    runtime_root = os.environ.get("JSE_RUNTIME_ROOT")
    if not legacy_root or not runtime_root or key not in {"applications_dir", "older_applications_dir"}:
        return value
    try:
        if Path(str(value)).resolve() == (Path(legacy_root) / key.removesuffix("_dir")).resolve():
            return str(Path(runtime_root) / key.removesuffix("_dir"))
    except (OSError, TypeError, ValueError):
        pass
    return value


def get_app_settings():
    settings = _app_setting_defaults()
    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
    except sqlite3.OperationalError:
        return settings
    for row in rows:
        try:
            settings[row["key"]] = json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            settings[row["key"]] = row["value_json"]
    for key in ("applications_dir", "older_applications_dir"):
        settings[key] = _persistent_runtime_path(key, settings.get(key))
    local_file_settings = _load_local_llm_settings()
    if local_file_settings:
        settings.update(local_file_settings)
    elif (
        settings.get("local_model")
        or settings.get("local_api_key")
        or settings.get("local_base_url") != DEFAULT_APP_SETTINGS.get("local_base_url")
    ):
        settings.update(_save_local_llm_settings({key: settings.get(key, "") for key in LOCAL_LLM_SETTING_FIELDS}))
    settings["claude_model"] = sanitize_claude_model(settings.get("claude_model"))
    return settings


def get_app_setting(key, default=None):
    return get_app_settings().get(key, default)


def update_app_settings(settings):
    allowed = {
        "applications_dir", "older_applications_dir",
        "onboarding_completed", "onboarding_version",
        *GLOBAL_AI_SETTING_FIELDS,
    }
    defaults = _app_setting_defaults()
    sanitized = {}
    local_llm_updates = {}
    for key, value in (settings or {}).items():
        if key not in allowed:
            continue
        if key == "onboarding_completed":
            sanitized[key] = bool(value)
            continue
        if key == "onboarding_version":
            try:
                sanitized[key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                sanitized[key] = 0
            continue
        text = str(value or "").strip()
        if not text:
            text = defaults.get(key, "")
        if key == "local_base_url":
            text = _normalize_local_base_url(text)
        if key in LOCAL_LLM_SETTING_FIELDS:
            local_llm_updates[key] = text
        elif key in {"applications_dir", "older_applications_dir"}:
            try:
                path = Path(text).expanduser()
                if not path.is_absolute():
                    runtime_root = Path(os.environ.get("JSE_RUNTIME_ROOT") or os.environ.get("JSE_APP_ROOT") or APP_ROOT)
                    path = (runtime_root / path).resolve()
                sanitized[key] = str(path)
            except Exception:
                sanitized[key] = text
        elif key == "gemini_model":
            sanitized[key] = sanitize_gemini_model(text)
        elif key == "claude_model":
            sanitized[key] = sanitize_claude_model(text)
        else:
            sanitized[key] = text
    if local_llm_updates:
        _save_local_llm_settings(local_llm_updates)
    with get_db_connection() as conn:
        for key, value in sanitized.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value_json, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value)),
            )
        conn.commit()
    return get_app_settings()


def migrate_profile_credentials_to_app_settings():
    """Move legacy per-lane API keys into app_settings and clear profile copies."""
    migrated = {}
    try:
        current_app = get_app_settings()
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT doc_ai_provider, doc_ai_model, openai_api_key, openai_base_url,
                       claude_api_key, claude_model, gemini_api_key, gemini_model, local_model
                FROM profiles
                """
            ).fetchall()
        for row in rows:
            for field in GLOBAL_AI_SETTING_FIELDS:
                value = str(row[field] or "").strip() if field in row.keys() else ""
                if value and not str(current_app.get(field) or "").strip() and field not in migrated:
                    migrated[field] = value
        if migrated:
            update_app_settings(migrated)
        with get_db_connection() as conn:
            conn.execute("UPDATE profiles SET openai_api_key = '', claude_api_key = '', gemini_api_key = ''")
            conn.commit()
    except sqlite3.OperationalError:
        return {"migrated_fields": [], "cleared_profile_credentials": False}
    return {
        "migrated_fields": sorted(migrated.keys()),
        "cleared_profile_credentials": True,
    }


def _json_loads_maybe(value, default=None):
    if value in (None, ""):
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else {}


def _scraper_plugin_row(row, lane_row=None):
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["manifest"] = _json_loads_maybe(data.pop("manifest_json", None), {})
    data["config"] = _json_loads_maybe(data.pop("config_json", None), {})
    if lane_row:
        data["lane_enabled"] = bool(lane_row["enabled"])
        data["lane_config"] = _json_loads_maybe(lane_row["config_json"], {})
    else:
        data["lane_enabled"] = True
        data["lane_config"] = {}
    return data


def ensure_builtin_scraper_plugins(plugins):
    with get_db_connection() as conn:
        for plugin in plugins:
            existing = conn.execute("SELECT id FROM scraper_plugins WHERE id = ?", (plugin["id"],)).fetchone()
            manifest = dict(plugin)
            config = {}
            for item in manifest.get("config_schema") or []:
                if "key" in item and "default" in item:
                    config[item["key"]] = item["default"]
            if existing:
                conn.execute(
                    """
                    UPDATE scraper_plugins
                    SET name = ?,
                        source_name = ?,
                        version = ?,
                        install_type = 'bundled',
                        install_path = NULL,
                        manifest_json = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        plugin.get("name") or plugin["id"],
                        plugin.get("source_name") or plugin.get("name") or plugin["id"],
                        plugin.get("version") or "",
                        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                        plugin["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO scraper_plugins
                        (id, name, source_name, version, enabled, install_type, install_path, manifest_json, config_json)
                    VALUES (?, ?, ?, ?, 1, 'bundled', NULL, ?, ?)
                    """,
                    (
                        plugin["id"],
                        plugin.get("name") or plugin["id"],
                        plugin.get("source_name") or plugin.get("name") or plugin["id"],
                        plugin.get("version") or "",
                        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                        json.dumps(config, separators=(",", ":"), sort_keys=True),
                    ),
                )
        conn.commit()


def disable_removed_builtin_scraper_plugins(active_builtin_ids):
    active = set(active_builtin_ids or [])
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM scraper_plugins WHERE install_type = 'bundled'"
        ).fetchall()
        removed = [row["id"] for row in rows if row["id"] not in active]
        if removed:
            placeholders = ",".join("?" for _ in removed)
            conn.execute(
                f"""
                UPDATE scraper_plugins
                SET enabled = 0,
                    updated_at = datetime('now')
                WHERE install_type = 'bundled'
                  AND id IN ({placeholders})
                """,
                removed,
            )
            conn.commit()


def disable_missing_user_scraper_plugins(valid_install_paths):
    valid = {str(Path(path).resolve()).casefold() for path in (valid_install_paths or []) if path}
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, install_path FROM scraper_plugins WHERE install_type = 'user'"
        ).fetchall()
        missing = []
        for row in rows:
            path = row["install_path"] or ""
            try:
                resolved = str(Path(path).resolve()).casefold()
            except Exception:
                resolved = path.casefold()
            if resolved not in valid or not Path(path).exists():
                missing.append(row["id"])
        if missing:
            placeholders = ",".join("?" for _ in missing)
            conn.execute(
                f"""
                UPDATE scraper_plugins
                SET enabled = 0,
                    updated_at = datetime('now')
                WHERE install_type = 'user'
                  AND id IN ({placeholders})
                """,
                missing,
            )
            conn.commit()


def get_scraper_plugin(plugin_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM scraper_plugins WHERE id = ?", (plugin_id,)).fetchone()
    return _scraper_plugin_row(row) if row else None


def get_scraper_plugins(include_disabled=True, profile_id=None):
    with get_db_connection() as conn:
        query = "SELECT * FROM scraper_plugins"
        params = []
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY install_type, name"
        rows = conn.execute(query, params).fetchall()
        lane_rows = {}
        if profile_id:
            lane_rows = {
                row["scraper_id"]: row
                for row in conn.execute(
                    "SELECT * FROM lane_scraper_settings WHERE lane_id = ?",
                    (profile_id,),
                ).fetchall()
            }
    return [_scraper_plugin_row(row, lane_rows.get(row["id"])) for row in rows]


def upsert_scraper_plugin(plugin, preserve_existing=True):
    existing = get_scraper_plugin(plugin["id"]) if preserve_existing else None
    enabled = int(existing["enabled"]) if existing and "enabled" in existing else int(plugin.get("enabled", 1))
    config_json = (
        json.dumps(existing.get("config") or {}, separators=(",", ":"), sort_keys=True)
        if existing
        else plugin.get("config_json") or "{}"
    )
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO scraper_plugins
                (id, name, source_name, version, enabled, install_type, install_path, manifest_json, config_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                source_name = excluded.source_name,
                version = excluded.version,
                enabled = excluded.enabled,
                install_type = excluded.install_type,
                install_path = excluded.install_path,
                manifest_json = excluded.manifest_json,
                config_json = excluded.config_json,
                updated_at = datetime('now')
            """,
            (
                plugin["id"],
                plugin["name"],
                plugin["source_name"],
                plugin.get("version") or "",
                enabled,
                plugin.get("install_type") or "user",
                plugin.get("install_path"),
                plugin["manifest_json"],
                config_json,
            ),
        )
        conn.commit()


def update_scraper_plugin(plugin_id, updates):
    allowed = {"enabled", "config_json", "name", "source_name", "version"}
    assignments = []
    params = []
    for key, value in (updates or {}).items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        params.append(value)
    if not assignments:
        return get_scraper_plugin(plugin_id)
    params.append(plugin_id)
    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE scraper_plugins SET {', '.join(assignments)}, updated_at = datetime('now') WHERE id = ?",
            params,
        )
        conn.commit()
    return get_scraper_plugin(plugin_id)


def delete_scraper_plugin(plugin_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM lane_scraper_settings WHERE scraper_id = ?", (plugin_id,))
        conn.execute("DELETE FROM scraper_plugins WHERE id = ?", (plugin_id,))
        conn.commit()


def update_lane_scraper_settings(lane_id, scraper_id, enabled=None, config=None):
    current = {}
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM lane_scraper_settings WHERE lane_id = ? AND scraper_id = ?",
            (lane_id, scraper_id),
        ).fetchone()
        if row:
            current = _json_loads_maybe(row["config_json"], {})
            if enabled is None:
                enabled = row["enabled"]
        if config is not None:
            current = {**current, **config}
        if enabled is None:
            enabled = 1
        conn.execute(
            """
            INSERT INTO lane_scraper_settings (lane_id, scraper_id, enabled, config_json, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(lane_id, scraper_id) DO UPDATE SET
                enabled = excluded.enabled,
                config_json = excluded.config_json,
                updated_at = datetime('now')
            """,
            (lane_id, scraper_id, 1 if enabled else 0, json.dumps(current, separators=(",", ":"), sort_keys=True)),
        )
        conn.commit()
    return True


def get_lane_scraper_setting(lane_id, scraper_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM lane_scraper_settings WHERE lane_id = ? AND scraper_id = ?",
            (lane_id, scraper_id),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["config"] = _json_loads_maybe(data.pop("config_json", None), {})
    return data


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


def upsert_candidate_fragments(person_id, fragments, replace=False):
    """Persist typed fragments to the cross-lane `candidate_fragments` bank.

    Mirrors the field handling in `upsert_profile_memory_fragments` so the
    activation metadata the LLM produces (keywords, anti_keywords, status,
    etc.) survives the round-trip. See that function for the rationale.
    """
    now = datetime.now().isoformat(timespec="seconds")
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
            conn.execute(
                """
                INSERT INTO candidate_fragments (
                    person_id, fragment_type, theme, claim, supporting_detail,
                    skills_json, domains_json, seniority, source_job_ids_json,
                    source_doc_paths_json, reuse_guidance, confidence, fingerprint,
                    keywords_json, anti_keywords_json, job_families_json,
                    status, confidence_reasoning, reinforces_themes_json,
                    support_count, last_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    clean["support_count"], now, now,
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


def _json_dumps_compact(value):
    return json.dumps(value or [], ensure_ascii=False, separators=(",", ":"))


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


# ---------------------------------------------------------------------------
# Outcome-weighted fragment confidence + re-mine scheduling.
#
# The fragment bank only earns its keep if it learns from actual outcomes.
# When a job moves through pipeline stages the fragments the kit USED should
# accumulate signal: interviewed/offer => positive, rejected/archived =>
# negative, applied => mildly positive (the human chose to spend a slot).
# `record_fragment_outcomes` is the event-time push; `recompute_fragment_
# outcome_scores` is the idempotent pull from the authoritative
# jobs.pipeline_stage column.
# ---------------------------------------------------------------------------


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


def update_job_fragment_alignment(job_id, fragment_score, composite_score, alignment_json):
    """Persist the fragment-bank alignment score and composite UI score on a job."""
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
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


def calculate_composite_score(match_score, fragment_score):
    """Canonical score formula: 80% final match + 20% fragment alignment."""
    if match_score is None:
        return None
    if fragment_score is None:
        return int(round(float(match_score)))
    return int(round(0.80 * float(match_score) + 0.20 * float(fragment_score)))


def recalculate_composite_scores():
    """Repair stale composites left by older analysis/gatekeeper write ordering."""
    changed = 0
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, match_score, fragment_score, composite_score FROM jobs WHERE match_score IS NOT NULL"
        ).fetchall()
        updates = []
        for row in rows:
            expected = calculate_composite_score(row["match_score"], row["fragment_score"])
            if row["composite_score"] != expected:
                updates.append((expected, row["id"]))
        if updates:
            conn.executemany("UPDATE jobs SET composite_score = ? WHERE id = ?", updates)
            conn.commit()
            changed = len(updates)
    return changed


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
    """Deletes a profile and its associated jobs and terms."""
    # Delete jobs for this profile
    with get_db_connection() as conn:
        conn.execute("DELETE FROM jobs WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profile_terms WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()

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


def _upsert_job_posting_from_row(conn, row):
    normalized_url = normalize_job_url(row["url"])
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
            row["id"], row["title"], row["company"], row["location"], normalized_url,
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


def _upsert_lane_opportunity_from_row(conn, row, posting_id, lane_id=None):
    lane_id = lane_id or row["profile_id"] or 1
    same_legacy_lane = int(lane_id) == int(row["profile_id"] or 0)
    # legacy_job_id points back to the single-lane jobs row and is UNIQUE for
    # backwards compatibility.  A deduped posting may legitimately appear in
    # several lanes, so only its original lane can own that legacy pointer;
    # every lane is still linked through the shared job_posting_id.
    legacy_job_id = row["id"] if same_legacy_lane else None
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


# --- Job CRUD ---

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
        (title, company, location, url, description, source, pdf_text, profile_id,
         date_scraped, last_interaction_at, updated_at, closing_date, contact_person,
         contact_email, contact_phone, salary, closing_date_source, description_fingerprint, advertiser_company,
         actual_company, employer_type, company_confidence, company_intelligence,
         company_research_updated_at, last_seen_at, missing_sweeps) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 0)
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
                    WHERE profile_id = ?
                    AND description_fingerprint = ?
                    LIMIT 1
                    """,
                    (profile_id, fingerprint),
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
                job_data.get('pdf_text'), profile_id, metadata.get("closing_date"),
                metadata.get("contact_person"), metadata.get("contact_email"),
                metadata.get("contact_phone"), metadata.get("salary"),
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
            "SELECT pipeline_stage, status, fragment_score FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        composite_score = calculate_composite_score(score, row["fragment_score"] if row else None)
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

def get_jobs_to_analyze(status_filter, re_analyze, profile_id=None, resume_text=""):
    """Fetches jobs that need AI analysis."""
    base_query = "SELECT id, title, description, pdf_text, position_description_text, analysis_signature, ai_analysis FROM jobs WHERE status = ? AND description IS NOT NULL"
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
    query = f"SELECT id, title, description, pdf_text, position_description_text, analysis_signature, ai_analysis FROM jobs WHERE id IN ({placeholders})"
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


# --- ATS / Pipeline helpers ---

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


def _profile_filter_clause(profile_id=None, include_all_profiles=False, alias="jobs"):
    if include_all_profiles or not profile_id:
        return "", []
    return f" AND {alias}.profile_id = ?", [profile_id]


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
)


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


def get_dashboard(profile_id=None, include_all_profiles=False):
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        stage_rows = conn.execute(
            f"""
            SELECT pipeline_stage, COUNT(*) AS count
            FROM jobs
            WHERE 1 = 1 {profile_clause}
            GROUP BY pipeline_stage
            """,
            params,
        ).fetchall()
        due_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.next_action_date IS NOT NULL
            AND jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            {profile_clause}
            ORDER BY jobs.next_action_date ASC
            LIMIT 12
            """,
            params,
        ).fetchall()
        top_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.match_score IS NOT NULL
            AND jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            {profile_clause}
            ORDER BY jobs.match_score DESC, jobs.id DESC
            LIMIT 8
            """,
            params,
        ).fetchall()
        feedback_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage IN ('applied', 'interviewing')
            AND (jobs.feedback IS NULL OR jobs.feedback = '')
            {profile_clause}
            ORDER BY COALESCE(jobs.application_date, jobs.last_interaction_at, jobs.id) DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
        cleanup_rows = conn.execute(
            f"""
            SELECT
                jobs.*,
                profiles.name AS profile_name,
                CAST(julianday('now') - julianday(jobs.application_date) AS INTEGER) AS days_since_application
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage = 'applied'
            AND jobs.application_date IS NOT NULL
            AND date(jobs.application_date) <= date('now', '-30 days')
            AND (jobs.feedback IS NULL OR jobs.feedback = '')
            AND NOT EXISTS (
                SELECT 1 FROM interviews
                WHERE interviews.job_id = jobs.id
            )
            {profile_clause}
            ORDER BY date(jobs.application_date) ASC, jobs.id ASC
            """,
            params,
        ).fetchall()
        last_scrape = conn.execute(
            f"""
            SELECT *
            FROM scraper_runs
            WHERE (? OR profile_id = ? OR profile_id IS NULL)
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (1 if include_all_profiles else 0, profile_id),
        ).fetchone()
    return {
        "stage_counts": {row["pipeline_stage"] or "new": row["count"] for row in stage_rows},
        "due_actions": due_rows,
        "top_matches": top_rows,
        "awaiting_feedback": feedback_rows,
        "cleanup_due": cleanup_rows,
        "last_scrape": last_scrape,
    }


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


def _row_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


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
    numbers = [int(match) for match in re.findall(r"\b(\d{2,4})\b", text)]
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


def score_campaign_job(row):
    job = _row_dict(row)
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
]


def _campaign_public_job(job):
    return {key: job.get(key) for key in CAMPAIGN_PUBLIC_FIELDS if key in job}


def _campaign_stage_clause(include_all_profiles, profile_id):
    return _profile_filter_clause(profile_id, include_all_profiles, alias="jobs")


def _sort_campaign_candidates(jobs):
    def key(job):
        close = job.get("closing_date") or "9999-12-31"
        recent = job.get("date_scraped") or job.get("updated_at") or job.get("id") or ""
        return (-int(job.get("campaign_score") or 0), close, str(recent))

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

    scored = [score_campaign_job(row) for row in rows]
    new_jobs = [job for job in scored if normalize_stage(job.get("pipeline_stage") or job.get("status")) == "new"]
    interested = [job for job in scored if normalize_stage(job.get("pipeline_stage") or job.get("status")) == "interested"]
    applied = [job for job in scored if normalize_stage(job.get("pipeline_stage") or job.get("status")) == "applied"]
    interviewing = [score_campaign_job(row) for row in interview_rows]

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

    scored = [score_campaign_job(row) for row in active_rows]
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


# ---------------------------------------------------------------------------
# Hidden-market intelligence. The reject/archive pile is not waste — it is a
# map of the market: which recruiters repeatedly carry this role family (with
# contacts the scrapers already harvested), which direct employers hire it,
# and which employers are stacking junior tech hires with no leadership
# posting — the visible footprint of an unadvertised leadership need.
# ---------------------------------------------------------------------------

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


def get_hidden_market_intel(profile_id=None, include_all_profiles=False, days=60, limit=12):
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    since = (datetime.now() - timedelta(days=int(days or 60))).isoformat(timespec="seconds")
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.advertiser_company, jobs.actual_company,
                   jobs.employer_type, jobs.match_score, jobs.composite_score, jobs.pipeline_stage,
                   jobs.url, jobs.application_url, jobs.contact_person, jobs.contact_email,
                   jobs.contact_phone, jobs.date_scraped, jobs.location,
                   SUBSTR(LOWER(COALESCE(jobs.description, '')), 1, 4000) AS description_head
            FROM jobs
            WHERE COALESCE(jobs.date_scraped, jobs.updated_at) >= ?
            {profile_clause}
            """,
            [since] + params,
        ).fetchall()

    recruiters = {}
    employers = {}
    employer_roles = {}
    for row in rows:
        title = str(row["title"] or "")
        title_lower = title.lower()
        score = int(row["composite_score"] or row["match_score"] or 0)
        leadership_title = bool(_matched_terms(title_lower, CAMPAIGN_LEADERSHIP_TERMS))
        tech_title = bool(_role_tokens(title) & BROAD_RELEVANT_TITLES)
        relevant = score >= 50 or leadership_title
        scraped = str(row["date_scraped"] or "")

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
            entry = recruiters.setdefault(_company_key(name), {
                "name": name, "roles": 0, "best_score": 0, "last_seen": "",
                "contact_person": "", "contact_email": "", "contact_phone": "", "sample_titles": [],
            })
            entry["roles"] += 1
            entry["best_score"] = max(entry["best_score"], score)
            entry["last_seen"] = max(entry["last_seen"], scraped)
            for field in ("contact_person", "contact_email", "contact_phone"):
                if not entry[field] and row[field]:
                    entry[field] = _clean(str(row[field]))
            if title and title not in entry["sample_titles"]:
                entry["sample_titles"] = (entry["sample_titles"] + [title])[:3]
            continue

        # Direct-employer watchlist: only identities the advert itself
        # corroborates. Organisations that have hired this role family hire
        # it again — and usually try the hidden channels first.
        employer_key = _company_key(employer_name)
        if verified_direct and employer_key and employer_key != "unknown":
            if relevant:
                entry = employers.setdefault(employer_key, {
                    "name": employer_name, "roles": 0, "best_score": 0, "last_seen": "",
                    "domain": "", "sample_titles": [], "locations": [], "verified": "ad signals",
                })
                entry["roles"] += 1
                entry["best_score"] = max(entry["best_score"], score)
                entry["last_seen"] = max(entry["last_seen"], scraped)
                entry["domain"] = entry["domain"] or domain
                if domain_confirms:
                    entry["verified"] = "contact domain"
                if title and title not in entry["sample_titles"]:
                    entry["sample_titles"] = (entry["sample_titles"] + [title])[:3]
                location = _clean(str(row["location"] or ""))
                if location and location not in entry["locations"]:
                    entry["locations"] = (entry["locations"] + [location])[:2]

            # Leadership-gap detection input: track IC-vs-leadership postings
            # per verified direct employer regardless of personal fit score.
            if tech_title:
                bucket = employer_roles.setdefault(employer_key, {
                    "name": employer_name, "ic_titles": [], "lead_count": 0,
                    "last_seen": "", "domain": "",
                })
                bucket["last_seen"] = max(bucket["last_seen"], scraped)
                bucket["domain"] = bucket["domain"] or domain
                if leadership_title:
                    bucket["lead_count"] += 1
                elif title not in bucket["ic_titles"]:
                    bucket["ic_titles"].append(title)

    leadership_gaps = [
        {
            "name": bucket["name"],
            "ic_count": len(bucket["ic_titles"]),
            "sample_titles": bucket["ic_titles"][:4],
            "last_seen": bucket["last_seen"],
            "domain": bucket["domain"],
        }
        for bucket in employer_roles.values()
        if len(bucket["ic_titles"]) >= 2 and bucket["lead_count"] == 0
    ]

    return {
        "window_days": int(days or 60),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recruiters": sorted(recruiters.values(), key=lambda r: (r["roles"], r["last_seen"]), reverse=True)[:limit],
        "direct_employers": sorted(employers.values(), key=lambda e: (e["best_score"], e["roles"]), reverse=True)[:limit],
        "leadership_gaps": sorted(leadership_gaps, key=lambda g: (g["ic_count"], g["last_seen"]), reverse=True)[:limit],
    }


# ---------------------------------------------------------------------------
# Hidden-market outreach leads (the to-do tracker). Outreach has its own
# lifecycle: a lead may go through several contact/wait touchpoints and most
# never become interviews, so leads live outside the job pipeline. A lead that
# does convert is pushed straight to the 'applied' stage (it is not pre-triage).
# ---------------------------------------------------------------------------

HIDDEN_MARKET_STATUSES = ("todo", "contacted", "awaiting", "done")


def hidden_market_target_key(target_type, name):
    return f"{target_type}:{_company_key(name)}"


def _hidden_market_lead_to_dict(row):
    lead = dict(row)
    try:
        lead["touchpoints"] = json.loads(row["touchpoints"]) if row["touchpoints"] else []
    except (TypeError, ValueError):
        lead["touchpoints"] = []
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
                           contact_person=None, contact_email=None, contact_phone=None, domain=None):
    """Start tracking a hidden-market target. Idempotent on (profile, type, key):
    re-tracking an existing target returns the existing lead untouched."""
    target_type = str(target_type or "").strip() or "target"
    target_name = _clean(str(target_name or "")) or "Unknown target"
    target_key = hidden_market_target_key(target_type, target_name)
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
                 contact_person, contact_email, contact_phone, domain, touchpoints, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, '[]', ?, ?)
            """,
            (profile_id, target_type, target_key, target_name, _clean(str(action or "")) or None,
             _clean(str(contact_person or "")) or None, _clean(str(contact_email or "")) or None,
             _clean(str(contact_phone or "")) or None, _clean(str(domain or "")) or None, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM hidden_market_leads WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _hidden_market_lead_to_dict(row)


def update_hidden_market_lead(lead_id, updates):
    allowed = {"action", "status", "outcome", "notes", "next_step_date",
               "contact_person", "contact_email", "contact_phone", "domain"}
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
        if status != "todo" or lead.get("touchpoints"):
            contacted_plus += 1
        outcome = lead.get("outcome") or ""
        if outcome in replied_outcomes:
            replied_plus += 1
        if outcome == "converted":
            converted_total += 1
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
        "reads": reads,
    }


def get_activity_stats(profile_id=None, include_all_profiles=False, days=7):
    """Weekly/monthly rollup: the market, the user's applications, and general
    activity — current window plus the previous window so the UI can show deltas.

    All comparisons use SQL datetime('now', offset) so they line up with the
    UTC timestamps the app writes (date_scraped, created_at, etc.)."""
    days = max(1, int(days or 7))
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    event_profile_clause = profile_clause  # both alias the jobs table

    def collect(conn, start_offset, end_offset=None):
        def time_clause(column):
            clause = f"{column} >= datetime('now', ?)"
            if end_offset:
                clause += f" AND {column} < datetime('now', ?)"
            return clause

        def time_params():
            return [start_offset] + ([end_offset] if end_offset else [])

        scraped_where = f"{time_clause('jobs.date_scraped')} {profile_clause}"
        scraped_params = time_params() + params

        scraped = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {scraped_where}", scraped_params
        ).fetchone()[0]
        analyzed = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {scraped_where} AND jobs.match_score IS NOT NULL",
            scraped_params,
        ).fetchone()[0]
        band_rows = conn.execute(
            f"""
            SELECT CASE
                WHEN jobs.match_score IS NULL THEN 'unscored'
                WHEN jobs.match_score >= 78 THEN '78+'
                WHEN jobs.match_score >= 70 THEN '70-77'
                WHEN jobs.match_score >= 60 THEN '60-69'
                WHEN jobs.match_score >= 45 THEN '45-59'
                ELSE '<45'
            END AS band, COUNT(*) AS count
            FROM jobs WHERE {scraped_where}
            GROUP BY band
            """,
            scraped_params,
        ).fetchall()
        band_counts = {row["band"]: row["count"] for row in band_rows}
        bands = [{"band": band, "count": band_counts.get(band, 0)} for band in ("78+", "70-77", "60-69", "45-59", "<45", "unscored")]

        sources = conn.execute(
            f"""
            SELECT jobs.source, COUNT(*) AS count FROM jobs
            WHERE {scraped_where}
            GROUP BY jobs.source ORDER BY count DESC LIMIT 6
            """,
            scraped_params,
        ).fetchall()
        employers = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(jobs.actual_company, ''), jobs.company) AS employer, COUNT(*) AS count
            FROM jobs
            WHERE {scraped_where}
            AND jobs.employer_type = 'direct_employer'
            AND COALESCE(jobs.match_score, 0) >= 60
            AND COALESCE(NULLIF(jobs.actual_company, ''), jobs.company, '') NOT IN ('', 'Unknown')
            GROUP BY employer ORDER BY count DESC LIMIT 6
            """,
            scraped_params,
        ).fetchall()

        applied = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {time_clause('jobs.application_date')} {profile_clause}",
            time_params() + params,
        ).fetchone()[0]
        interviews = conn.execute(
            f"""
            SELECT COUNT(*) FROM interviews JOIN jobs ON jobs.id = interviews.job_id
            WHERE {time_clause("COALESCE(interviews.interview_date, interviews.created_at)")} {event_profile_clause}
            """,
            time_params() + params,
        ).fetchone()[0]

        def event_count(where, extra_params=()):
            return conn.execute(
                f"""
                SELECT COUNT(*) FROM application_events JOIN jobs ON jobs.id = application_events.job_id
                WHERE {time_clause('application_events.created_at')} AND {where} {event_profile_clause}
                """,
                time_params() + list(extra_params) + params,
            ).fetchone()[0]

        offers = event_count("application_events.event_type = 'stage' AND application_events.title LIKE 'Moved to Offer%'")
        docs_generated = event_count("application_events.event_type = 'documents'")
        prompts_generated = event_count("application_events.event_type = 'prompt'")
        auto_rejected = event_count("application_events.title = 'Auto-rejected low match'")
        archived = event_count("application_events.event_type IN ('cleanup', 'retired') OR application_events.title LIKE 'Archived%'")

        stage_moves = conn.execute(
            f"""
            SELECT application_events.title, COUNT(*) AS count
            FROM application_events JOIN jobs ON jobs.id = application_events.job_id
            WHERE {time_clause('application_events.created_at')}
            AND application_events.event_type = 'stage'
            AND application_events.title LIKE 'Moved to %'
            {event_profile_clause}
            GROUP BY application_events.title ORDER BY count DESC LIMIT 8
            """,
            time_params() + params,
        ).fetchall()

        return {
            "scraped": scraped,
            "analyzed": analyzed,
            "bands": bands,
            "top_sources": [{"source": normalize_source(row["source"]), "count": row["count"]} for row in sources],
            "top_employers": [{"employer": row["employer"], "count": row["count"]} for row in employers],
            "applied": applied,
            "interviews": interviews,
            "offers": offers,
            "docs_generated": docs_generated,
            "prompts_generated": prompts_generated,
            "auto_rejected": auto_rejected,
            "archived": archived,
            "stage_moves": [{"title": row["title"], "count": row["count"]} for row in stage_moves],
        }

    with get_db_connection() as conn:
        current = collect(conn, f"-{days} days")
        previous = collect(conn, f"-{days * 2} days", f"-{days} days")
        last_scrape = conn.execute(
            "SELECT started_at, finished_at, status, summary FROM scraper_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    return {
        "window_days": days,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "current": current,
        "previous": previous,
        "last_scrape": {key: last_scrape[key] for key in last_scrape.keys()} if last_scrape else None,
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
        conn.commit()
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


def record_scraper_run(profile_id=None, scope="profile", sources=None, status="running", summary=None, run_id=None):
    with get_db_connection() as conn:
        if run_id:
            conn.execute(
                "UPDATE scraper_runs SET finished_at = datetime('now'), status = ?, summary = ? WHERE id = ?",
                (status, summary, run_id),
            )
            conn.commit()
            return run_id
        cursor = conn.execute(
            "INSERT INTO scraper_runs (profile_id, scope, sources, status, summary) VALUES (?, ?, ?, ?, ?)",
            (profile_id, scope, ",".join(sources or []), status, summary),
        )
        conn.commit()
        return cursor.lastrowid


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


def get_calendar_items(profile_id=None, include_all_profiles=False, days=45):
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        job_rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.pipeline_stage, jobs.next_action,
                   jobs.next_action_date, jobs.interview_date, jobs.closing_date,
                   NULL AS interview_round, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            AND (jobs.match_score IS NULL OR jobs.match_score >= 50)
            AND (
                jobs.next_action_date IS NOT NULL OR
                jobs.closing_date IS NOT NULL
            )
            {profile_clause}
            LIMIT 100
            """,
            params,
        ).fetchall()
        interview_rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.pipeline_stage,
                   COALESCE(interviews.next_action, 'Interview') AS next_action,
                   interviews.next_action_date,
                   interviews.interview_date,
                   jobs.closing_date,
                   interviews.round_number AS interview_round,
                   profiles.name AS profile_name
            FROM interviews
            JOIN jobs ON jobs.id = interviews.job_id
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            AND (jobs.match_score IS NULL OR jobs.match_score >= 50)
            AND interviews.interview_date IS NOT NULL
            {profile_clause}
            LIMIT 100
            """,
            params,
        ).fetchall()
    rows = list(job_rows) + list(interview_rows)
    return sorted(
        rows,
        key=lambda row: row["next_action_date"] or row["interview_date"] or row["closing_date"] or "9999-12-31",
    )[:100]


def get_all_sources(profile_id=None):
    """Returns a list of all unique scraper sources in the database."""
    query = "SELECT DISTINCT source FROM jobs"
    params = []
    if profile_id:
        query += " WHERE profile_id = ?"
        params.append(profile_id)
    query += " ORDER BY source"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return sorted({normalize_source(row[0]) for row in cursor.fetchall() if row[0]})


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
        keep_ids = set()
        seen_urls = set()
        seen_fingerprints = set()
        for row in rows:
            url_key = normalized_by_id[row["id"]]
            fingerprint_key = (row["profile_id"], fingerprint_by_id[row["id"]]) if "profile_id" in row.keys() else None
            if url_key in seen_urls:
                continue
            if fingerprint_key and fingerprint_key[1] and fingerprint_key in seen_fingerprints:
                continue
            keep_ids.add(row["id"])
            seen_urls.add(url_key)
            if fingerprint_key and fingerprint_key[1]:
                seen_fingerprints.add(fingerprint_key)
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            conn.execute(f"DELETE FROM jobs WHERE id NOT IN ({placeholders})", tuple(keep_ids))

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
            if not _is_meaningful_job_identity(row["title"], row["company"]):
                continue
            key = (row["profile_id"], *_job_identity_key(row["title"], row["company"]))
            identity_groups.setdefault(key, []).append(row)
        delete_identity_ids = []
        for group in identity_groups.values():
            if len(group) < 2:
                continue
            keep = max(group, key=lambda row: (_stage_dedupe_rank(row), str(row["recency"] or ""), row["id"]))
            delete_identity_ids.extend(row["id"] for row in group if row["id"] != keep["id"])
        if delete_identity_ids:
            placeholders = ",".join("?" for _ in delete_identity_ids)
            conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(delete_identity_ids))

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
