"""Public-source, evidence-first contact enrichment for Intelligence targets.

This module deliberately does not authenticate to or scrape LinkedIn pages. It
uses publicly indexed search-result titles/snippets and organisation pages,
retains their URLs as provenance, and exposes ambiguity instead of guessing.
"""
import hashlib
import re
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from xml.etree import ElementTree

import requests

import database_manager as db


SEARCH_URL = "https://html.duckduckgo.com/html/?q={}"
BING_RSS_URL = "https://www.bing.com/search?format=rss&q={}"
USER_AGENT = "Mozilla/5.0 (compatible; JSE-ContactResearch/1.0)"
GENERIC_EMAIL_NAMES = {"info", "jobs", "careers", "recruitment", "talent", "hello", "admin", "contact", "apply"}
RESEARCH_VERSION = 2
RELEVANT_ROLE_TERMS = {"technology", "digital", "it", "cloud", "data", "change", "transformation", "project", "program", "agile", "executive"}


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _name_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _candidate_id(name, email=""):
    raw = f"{_name_key(name)}|{str(email or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _email_inferred_name(email):
    local = str(email or "").split("@", 1)[0].lower()
    parts = [part for part in re.split(r"[._-]+", local) if part and part not in GENERIC_EMAIL_NAMES]
    if len(parts) < 2 or any(not part.isalpha() for part in parts):
        return ""
    return " ".join(part.capitalize() for part in parts[:3])


def _name_matches_email(name, email):
    inferred = _email_inferred_name(email)
    if not inferred or not name:
        return True
    left = set(_name_key(name).split())
    right = set(_name_key(inferred).split())
    return bool(left & right) and (next(iter(right), "") in left or len(left & right) >= 2)


def _canonical_ad_contact(item):
    email = _clean(item.get("email") or item.get("contact_email")).lower()
    raw_name = _clean(item.get("name") or item.get("contact_person"))
    explicit = db._canonical_person_name(raw_name)  # shared extraction contract
    inferred = _email_inferred_name(email)
    discarded = ""
    if inferred:
        if explicit and _name_matches_email(explicit, email):
            name = explicit
        else:
            name = inferred
            discarded = raw_name if raw_name and _name_key(raw_name) != _name_key(inferred) else ""
    else:
        name = explicit
        discarded = raw_name if raw_name and not explicit else ""
    if not name and not email and not item.get("phone") and not item.get("contact_phone"):
        return None
    return {
        "name": name or email,
        "email": email,
        "phone": _clean(item.get("phone") or item.get("contact_phone")),
        "quality": item.get("quality") or ("explicit" if explicit else "email-derived" if inferred else "unverified"),
        "discarded_labels": [discarded] if discarded else [],
        "conflicts": [],
    }


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current = None
        self.capture = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and "result__a" in classes:
            self.current = {"url": _unwrap_url(attrs.get("href", "")), "title": "", "snippet": ""}
            self.capture = "title"
        elif self.current and "result__snippet" in classes:
            self.capture = "snippet"

    def handle_data(self, data):
        if self.current and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag):
        if self.current and tag == "a" and self.capture == "title":
            self.current["title"] = _clean(self.current["title"])
            if self.current["url"] and self.current["title"]:
                self.results.append(self.current)
            self.current = None
            self.capture = ""


def _unwrap_url(url):
    value = str(url or "")
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    redirect = parse_qs(parsed.query).get("uddg")
    return unquote(redirect[0]) if redirect else value


def public_web_search(query, limit=6, timeout=15):
    """Return public search metadata only; never fetch authenticated profiles."""
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(
        BING_RSS_URL.format(quote_plus(query)),
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    results = []
    try:
        root = ElementTree.fromstring(response.text)
        for item in root.findall("./channel/item"):
            url = _clean(item.findtext("link"))
            title = _clean(unescape(item.findtext("title") or ""))
            snippet = _clean(re.sub(r"<[^>]+>", " ", unescape(item.findtext("description") or "")))
            if url and title:
                results.append({
                    "url": url, "title": title, "snippet": snippet,
                    "source_type": "LinkedIn public result" if "linkedin.com/" in url.lower() else "Public web result",
                })
            if len(results) >= limit:
                return results
    except ElementTree.ParseError:
        results = []

    # DuckDuckGo HTML is a secondary path; it sometimes presents an anti-bot
    # page, in which case the empty parse is handled by the caller as a warning.
    response = requests.get(
        SEARCH_URL.format(quote_plus(query)),
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    parser = _SearchParser()
    parser.feed(response.text[:2_000_000])
    results = list(results)
    seen = set()
    for item in parser.results:
        url = item.get("url") or ""
        if url in seen:
            continue
        seen.add(url)
        item["source_type"] = "LinkedIn public result" if "linkedin.com/" in url.lower() else "Public web result"
        results.append(item)
        if len(results) >= limit:
            break
    return results


def contacts_from_ad_evidence(target):
    """Build people from structured ad contact blocks, discarding noisy labels."""
    raw = []
    evidence = list(target.get("evidence") or [])
    has_structured = any(item.get("contacts") for item in evidence)
    if not has_structured and (target.get("contact_person") or target.get("contact_email") or target.get("contact_phone")):
        evidence.append({
            "contact_person": target.get("contact_person"), "contact_email": target.get("contact_email"),
            "contact_phone": target.get("contact_phone"), "url": target.get("url"), "title": "Aggregated target contact",
        })
    for item in evidence:
        source = {"url": item.get("url") or "", "title": item.get("title") or "Job advertisement", "source_type": "Job advertisement"}
        contacts = item.get("contacts") or [{
            "contact_person": item.get("contact_person"), "contact_email": item.get("contact_email"),
            "contact_phone": item.get("contact_phone"),
        }]
        for contact in contacts:
            canonical = _canonical_ad_contact(contact)
            if canonical:
                canonical.update({"sources": [source], "ad_mentions": 1})
                raw.append(canonical)

    merged = {}
    for item in raw:
        key = item["email"] or _name_key(item["name"]) or re.sub(r"\D", "", item["phone"])
        candidate = merged.setdefault(key, {**item, "ad_mentions": 0})
        candidate["ad_mentions"] += int(item.get("ad_mentions") or 1)
        candidate["email"] = candidate.get("email") or item.get("email")
        candidate["phone"] = candidate.get("phone") or item.get("phone")
        candidate["discarded_labels"] = list(dict.fromkeys((candidate.get("discarded_labels") or []) + item.get("discarded_labels", [])))
        for source in item.get("sources", []):
            if source not in candidate["sources"]:
                candidate["sources"].append(source)
    return list(merged.values())


def _public_name_from_title(title, organisation):
    first = re.split(r"\s[-|–]\s", _clean(title))[0]
    if organisation and _name_key(first) == _name_key(organisation):
        return ""
    if re.fullmatch(r"[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3}", first):
        return first
    return ""


def _attach_public_results(candidates, results, organisation):
    for result in results:
        blob = _name_key(f"{result.get('title')} {result.get('snippet')}")
        matched = None
        for candidate in candidates:
            tokens = set(_name_key(candidate.get("name")).split())
            email = str(candidate.get("email") or "").lower()
            result_text = f"{result.get('title') or ''} {result.get('snippet') or ''}".lower()
            if (email and email in result_text) or (len(tokens) >= 2 and len(tokens & set(blob.split())) >= 2):
                matched = candidate
                break
        if not matched:
            name = _public_name_from_title(result.get("title"), organisation)
            organisation_tokens = set(_name_key(organisation).split())
            if not name or not (organisation_tokens & set(blob.split())):
                continue
            matched = {"name": name, "email": "", "phone": "", "sources": [], "conflicts": [], "discarded_labels": [], "ad_mentions": 0, "quality": "public-only"}
            candidates.append(matched)
        source = {key: result.get(key) for key in ("url", "title", "snippet", "source_type")}
        if source not in matched["sources"]:
            matched["sources"].append(source)
        if "linkedin.com/in/" in str(result.get("url") or "").lower():
            matched["profile_url"] = result["url"]
        title_parts = re.split(r"\s[-|–]\s", result.get("title") or "")
        if len(title_parts) > 1 and not matched.get("role"):
            matched["role"] = title_parts[1][:120]
    return candidates


def _score_candidates(candidates, organisation):
    for candidate in candidates:
        public_sources = [source for source in candidate.get("sources", []) if source.get("source_type") != "Job advertisement"]
        score = min(30, int(candidate.get("ad_mentions") or 0) * 15)
        score += 20 if candidate.get("email") else 0
        score += 8 if candidate.get("phone") else 0
        score += min(20, len(public_sources) * 10)
        score += 15 if candidate.get("profile_url") else 0
        score += 15 if candidate.get("quality") in {"explicit", "provided", "scraper-provided"} else 5 if candidate.get("quality") == "email-derived" else 0
        role_tokens = set(_name_key(candidate.get("role")).split())
        score += 10 if role_tokens & RELEVANT_ROLE_TERMS else 0
        candidate["confidence_score"] = max(0, min(100, score))
        candidate["confidence"] = "high" if score >= 75 else "medium" if score >= 55 else "low"
        candidate["candidate_id"] = _candidate_id(candidate.get("name"), candidate.get("email"))
        candidate["organisation"] = organisation
    return sorted(candidates, key=lambda item: (item["confidence_score"], item.get("ad_mentions", 0)), reverse=True)


def research_target_contacts(target, search_func=public_web_search):
    organisation = _clean(target.get("name") or target.get("target_name"))
    candidates = contacts_from_ad_evidence(target)
    queries = []
    domain = next((str(candidate.get("email") or "").split("@", 1)[1] for candidate in candidates if "@" in str(candidate.get("email") or "")), "")
    for candidate in candidates[:5]:
        if candidate.get("email"):
            queries.append(f'"{candidate["email"]}"')
        if candidate.get("name") and "@" not in candidate["name"]:
            queries.append(f'"{candidate["name"]}" "{organisation}"')
            queries.append(f'site:linkedin.com/in "{candidate["name"]}" "{organisation}"')
            if domain:
                queries.append(f'site:{domain} "{candidate["name"]}"')
    if len(candidates) < 3:
        queries.extend([f'"{organisation}" recruitment consultants technology', f'"{organisation}" team recruitment'])
    results, errors = [], []
    for query in list(dict.fromkeys(queries))[:8]:
        try:
            results.extend(search_func(query, limit=5))
        except Exception as exc:  # public research is best-effort
            errors.append(f"{type(exc).__name__}: {exc}")
    deduped = {item.get("url"): item for item in results if item.get("url")}
    candidates = _attach_public_results(candidates, list(deduped.values()), organisation)
    candidates = _score_candidates(candidates, organisation)
    credible = [candidate for candidate in candidates if candidate["confidence_score"] >= 45]
    top = credible[0] if credible else None
    second = credible[1] if len(credible) > 1 else None
    ambiguous = bool(top and second and top["confidence_score"] >= 55 and second["confidence_score"] >= 55 and top["confidence_score"] - second["confidence_score"] < 15)
    selected = None if ambiguous or not top or top["confidence_score"] < 55 else top["candidate_id"]
    conflicts = list(dict.fromkeys(conflict for candidate in candidates for conflict in candidate.get("conflicts", [])))
    visible = credible[:3]
    discarded_labels = list(dict.fromkeys(label for candidate in candidates for label in candidate.get("discarded_labels", []) if label))
    requires_selection = ambiguous
    return {
        "research_version": RESEARCH_VERSION,
        "target_name": organisation,
        "researched_at": datetime.now().isoformat(timespec="seconds"),
        "candidates": candidates,
        "selected_candidate_id": selected,
        "requires_selection": requires_selection,
        "recommended_candidate_id": top["candidate_id"] if top else None,
        "visible_candidate_ids": [candidate["candidate_id"] for candidate in visible],
        "suppressed_candidate_count": max(0, len(candidates) - len(visible)),
        "discarded_labels_count": len(discarded_labels),
        "conflicts": conflicts,
        "public_results_checked": len(deduped),
        "warnings": (["No reliable person was found; JSE will build an organisation-level strategy."] if not top else []) + (["Public research was unavailable; only job-ad evidence was used."] if errors and not deduped else []),
        "research_policy": "Public search metadata and organisation pages only; no authenticated LinkedIn scraping.",
    }


def selected_contact(research):
    selected_id = (research or {}).get("selected_candidate_id")
    return next((item for item in (research or {}).get("candidates", []) if item.get("candidate_id") == selected_id), None)


def enrich_target_contacts(profile_id, target, force=False):
    target_type = target.get("target_type") or "target"
    target_name = target.get("name") or target.get("target_name") or "Unknown target"
    target_key = target.get("target_key") or db.hidden_market_target_key(target_type, target_name, target.get("entity_key"))
    cached = db.get_hidden_market_contact_research(profile_id, target_type, target_key)
    if cached and not force:
        try:
            researched = datetime.fromisoformat(str(cached.get("researched_at") or ""))
            if researched >= datetime.now() - timedelta(days=7) and int((cached.get("research") or {}).get("research_version") or 0) == RESEARCH_VERSION:
                return cached["research"]
        except (TypeError, ValueError):
            pass
    research = research_target_contacts(target)
    previous_selected = (cached or {}).get("selected_candidate_id")
    if previous_selected and any(item.get("candidate_id") == previous_selected for item in research.get("candidates", [])):
        research["selected_candidate_id"] = previous_selected
        research["requires_selection"] = False
    return db.save_hidden_market_contact_research(profile_id, target_type, target_key, target_name, research)["research"]
