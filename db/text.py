"""Pure normalisation and key-derivation helpers. No database access.

Split out of database_manager.py, which re-exports everything here.
"""
import re
import hashlib
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from .constants import (
    COMPANY_CANDIDATE_STOPWORDS,
    MONTHS,
    ROLE_STOPWORDS,
    SOURCE_ALIASES,
)

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


def _json_loads_maybe(value, default=None):
    if value in (None, ""):
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else {}


def _json_dumps_compact(value):
    return json.dumps(value or [], ensure_ascii=False, separators=(",", ":"))


def _normalized_title_key(title):
    text = re.sub(r"[^a-z0-9 ]+", " ", str(title or "").lower())
    tokens = sorted({t for t in text.split() if t and t not in ROLE_STOPWORDS})
    return " ".join(tokens)


def _advertiser_key(job):
    return _company_key(job.get("advertiser_company") or job.get("company") or "")


# Role-linking similarity threshold: a re-advertised role rarely has a
# byte-identical description (that would already have been deduped into one
# job), so exact-fingerprint match is not enough. A compact significant-token
# signature + Jaccard overlap catches the "same role, reworded ad" case that
# left 6353/22508 as two separate rows.
_ROLE_SIG_JACCARD = 0.6


_ROLE_SIG_TOKEN_CAP = 400


def _desc_signature(description):
    text = re.sub(r"https?://\S+", " ", str(description or "").lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = {
        t for t in text.split()
        if len(t) >= 4 and t not in ROLE_STOPWORDS and t not in COMPANY_CANDIDATE_STOPWORDS
    }
    return sorted(tokens)[:_ROLE_SIG_TOKEN_CAP]


def _signature_similarity(a, b):
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _iso_day(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").split(".")[0][:19])
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _within_days(a, b, days):
    da, db_ = _iso_day(a), _iso_day(b)
    if da is None or db_ is None:
        return True  # missing dates never block a match
    return abs((da - db_).days) <= days


def _row_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}
