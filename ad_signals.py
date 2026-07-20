"""Deterministic job-advertisement signals.

Cheap, no-LLM intelligence derived from columns already stored on a job row:
repost/recurrence, application friction, apply channel, salary transparency,
and vacancy age/urgency (Tier 1), plus best-effort hiring trigger, reporting
line, team size, and ATS keyword cues (Tier 2 deterministic shadow of the
LLM extraction).

Pure functions only — no DB or network. ``derive`` is called per job when the
board list and job detail payloads are assembled, so it must stay fast: a few
regex passes over a truncated description is the budget.
"""
import re
from datetime import datetime

# --- Tier 1: application friction -------------------------------------------
# phrase -> short label surfaced on the card
_FRICTION_PATTERNS = [
    (r"psychometric|aptitude test|cognitive (?:test|assessment)", "psychometric test"),
    (r"\bassessment centre\b|assessment center", "assessment centre"),
    (r"technical (?:test|challenge|assessment)|coding (?:test|challenge)|take[- ]home", "technical test"),
    (r"portfolio", "portfolio"),
    (r"writing sample|written exercise|presentation to", "work sample"),
    (r"security clearance|baseline clearance|nv1|nv2|positive vetting|agsva", "security clearance"),
    (r"police check|criminal (?:history|record) check|national police", "police check"),
    (r"working with children|wwcc|wwc check", "WWCC"),
    (r"video interview|one[- ]way interview|hirevue|spark hire", "video interview"),
    (r"selection criteria", "selection criteria"),
    (r"pre[- ]employment medical|functional capacity|medical assessment", "medical check"),
]

# --- Tier 1: apply channel --------------------------------------------------
_ATS_DOMAINS = (
    "myworkday", "workday", "taleo", "smartrecruiters", "lever.co", "greenhouse",
    "pageuppeople", "pageup", "jobadder", "livehire", "expr3ss", "snaphire",
    "scouterecruit", "elmo", "bigredsky", "mercury.com", "applynow", "springboard",
    "recruitee", "jobvite", "icims", "successfactors", "hr.partners", "fitzroy",
)
_BOARD_DOMAINS = ("seek.com", "linkedin.com", "indeed.com", "jora.com", "adzuna")

# --- Tier 2: hiring trigger -------------------------------------------------
_TRIGGER_PATTERNS = [
    ("growth", r"newly created|new role|due to (?:continued )?growth|growing team|expansion|expanding team|scale up|scaling"),
    ("replacement", r"replacing|replacement|successor|departure of|stepping down|backfilling a permanent"),
    ("backfill", r"backfill|maternity|parental leave|cover (?:a )?leave|secondment|temporary cover|fixed[- ]term"),
    ("restructure", r"restructure|newly formed team|transformation programme|stand[- ]?up a (?:new )?team|establish a new"),
]

_REPORTING_RE = re.compile(
    r"report(?:ing|s)?\s+(?:directly\s+)?to\s+(?:the\s+)?([A-Za-z][A-Za-z/&,\-' ]{2,45})",
    re.IGNORECASE,
)
_TEAM_SIZE_RES = [
    re.compile(r"team of (\d{1,3})", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s+(?:direct reports|reports|staff|team members|fte|engineers|analysts)", re.IGNORECASE),
    re.compile(r"manage[s]?\s+(?:a team of\s+)?(\d{1,3})", re.IGNORECASE),
]

# Curated capability vocabulary for ATS keyword cues (phrases an ATS parser weights).
_ATS_VOCAB = (
    "stakeholder management", "vendor management", "service delivery", "change management",
    "project management", "program management", "business analysis", "data governance",
    "cyber security", "cybersecurity", "information security", "risk management",
    "cloud", "azure", "aws", "gcp", "kubernetes", "devops", "itil", "agile", "scrum",
    "power bi", "sql", "python", "salesforce", "sap", "erp", "integration",
    "digital transformation", "leadership", "people management", "budget management",
    "procurement", "compliance", "governance", "architecture", "networking",
)


def _text(job):
    return str(job.get("description") or job.get("ad_text") or "")


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19].replace(" ", "T"))
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return None


def _friction(text_lower):
    hits = []
    for pattern, label in _FRICTION_PATTERNS:
        if re.search(pattern, text_lower) and label not in hits:
            hits.append(label)
    return hits


def _apply_channel(job, text_lower):
    if str(job.get("employer_type") or "") == "recruiter" or "our client" in text_lower:
        return "recruiter"
    urls = " ".join(str(job.get(k) or "") for k in ("application_url", "url")).lower()
    if any(d in urls for d in _ATS_DOMAINS):
        return "ats"
    email = str(job.get("contact_email") or "").lower()
    if "@" in email and not any(d in email for d in ("gmail", "hotmail", "outlook", "yahoo")):
        return "email_direct"
    if any(d in urls for d in _BOARD_DOMAINS):
        return "board_apply"
    return "unknown"


def _salary_disclosed(job, text):
    salary = str(job.get("salary") or "")
    if re.search(r"\d", salary):
        return True
    return bool(re.search(r"\$\s?\d{2,3}[,\d]{2,}", text))


def _hiring_trigger(text_lower):
    for label, pattern in _TRIGGER_PATTERNS:
        if re.search(pattern, text_lower):
            return label
    return "unknown"


def _reporting_line(text):
    match = _REPORTING_RE.search(text)
    if not match:
        return ""
    # Trim at the first sentence/clause boundary.
    line = re.split(r"[.,;\n]| and | who ", match.group(1))[0].strip()
    return line[:48] if len(line.split()) <= 7 else ""


def _team_size(text):
    for regex in _TEAM_SIZE_RES:
        match = regex.search(text)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, TypeError):
                continue
    return None


def _ats_keywords(job, text_lower):
    found = [term for term in _ATS_VOCAB if term in text_lower]
    # De-dupe near-duplicates (cyber security / cybersecurity).
    if "cybersecurity" in found and "cyber security" in found:
        found.remove("cybersecurity")
    title_tokens = [t for t in re.findall(r"[a-z]{4,}", str(job.get("title") or "").lower())
                    if t not in {"with", "from", "your", "team", "role", "lead"}]
    keywords = []
    for term in found + title_tokens:
        if term not in keywords:
            keywords.append(term)
    return keywords[:12]


def _text_fingerprint(job, text):
    """Cheap change-detection key for the regex-derived signals of one job.

    Covers every field _text_signals reads. hash(text) is O(n) but orders of
    magnitude cheaper than the ~20 regex passes it lets us skip.
    """
    return (
        len(text),
        hash(text),
        str(job.get("title") or ""),
        str(job.get("salary") or ""),
        str(job.get("employer_type") or ""),
        str(job.get("application_url") or ""),
        str(job.get("url") or ""),
        str(job.get("contact_email") or ""),
    )


def _text_signals(job, text, text_lower):
    """The regex-heavy signals that depend only on stored job fields (no clock)."""
    return {
        "friction": _friction(text_lower),
        "apply_channel": _apply_channel(job, text_lower),
        "salary_disclosed": _salary_disclosed(job, text),
        "hiring_trigger": _hiring_trigger(text_lower),
        "reporting_line": _reporting_line(text),
        "team_size": _team_size(text),
        "ats_keywords": _ats_keywords(job, text_lower),
    }


def derive(job, recurrence_count=0, cache=None):
    """Return a compact deterministic-signals dict for one job row/dict.

    ``recurrence_count`` is the number of rows sharing this job's normalised
    company+title (1 = only this posting). Everything else is read from ``job``.

    ``cache`` (optional) is a dict of job_id -> (fingerprint, text_signals)
    owned by the caller. The regex passes dominate list assembly (~1.5s for a
    ~5000-job board), so a persistent process should pass a long-lived dict;
    only jobs whose text/fields changed are re-scanned. Date-relative fields
    (age, closing window, urgency) are always computed fresh.
    """
    text = _text(job)

    signals = None
    if cache is not None:
        job_id = job.get("id")
        fingerprint = _text_fingerprint(job, text)
        cached = cache.get(job_id) if job_id is not None else None
        if cached is not None and cached[0] == fingerprint:
            signals = cached[1]
        else:
            signals = _text_signals(job, text, text.lower())
            if job_id is not None:
                if len(cache) > 20000:
                    cache.clear()  # unbounded growth guard; repopulates naturally
                cache[job_id] = (fingerprint, signals)
    else:
        signals = _text_signals(job, text, text.lower())

    now = datetime.now()
    scraped = _parse_date(job.get("date_scraped") or job.get("updated_at"))
    age_days = (now - scraped).days if scraped else None
    closing = _parse_date(job.get("closing_date"))
    closes_in_days = (closing - now).days if closing else None

    if closes_in_days is not None and 0 <= closes_in_days <= 7:
        urgency = "closing_soon"
    elif age_days is not None and age_days <= 3:
        urgency = "fresh"
    elif age_days is not None and age_days >= 45:
        urgency = "stale"
    else:
        urgency = None

    count = int(recurrence_count or 0)

    return {
        # Tier 1
        "recurrence_count": count,
        "is_recurring": count >= 2,
        "age_days": age_days,
        "closes_in_days": closes_in_days,
        "urgency": urgency,
        # Regex-derived Tier 1 + Tier 2 (deterministic shadow; LLM extraction
        # enriches the same fields)
        **signals,
    }
