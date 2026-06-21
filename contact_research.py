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
    """Reconcile ad contacts and split obvious name/email-owner conflicts."""
    raw = []
    evidence = list(target.get("evidence") or [])
    if target.get("contact_person") or target.get("contact_email") or target.get("contact_phone"):
        evidence.append({
            "contact_person": target.get("contact_person"), "contact_email": target.get("contact_email"),
            "contact_phone": target.get("contact_phone"), "url": target.get("url"), "title": "Aggregated target contact",
        })
    for item in evidence:
        name = _clean(item.get("contact_person"))
        email = _clean(item.get("contact_email")).lower()
        phone = _clean(item.get("contact_phone"))
        source = {"url": item.get("url") or "", "title": item.get("title") or "Job advertisement", "source_type": "Job advertisement"}
        if name and email and not _name_matches_email(name, email):
            raw.append({"name": name, "email": "", "phone": phone, "sources": [source], "conflicts": [f"The same ad pairs {name} with {email}, whose address appears to belong to another person."]})
            inferred = _email_inferred_name(email)
            raw.append({"name": inferred or email, "email": email, "phone": "", "sources": [source], "conflicts": [f"Email inferred separately because it does not match the named contact {name}."]})
        elif name or email or phone:
            raw.append({"name": name or _email_inferred_name(email) or email or phone, "email": email, "phone": phone, "sources": [source], "conflicts": []})

    merged = {}
    for item in raw:
        key = item["email"] or _name_key(item["name"]) or re.sub(r"\D", "", item["phone"])
        candidate = merged.setdefault(key, {**item, "ad_mentions": 0})
        candidate["ad_mentions"] += 1
        candidate["email"] = candidate.get("email") or item.get("email")
        candidate["phone"] = candidate.get("phone") or item.get("phone")
        candidate["conflicts"] = list(dict.fromkeys((candidate.get("conflicts") or []) + item.get("conflicts", [])))
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
            surname = _name_key(candidate.get("name")).split()[-1:] or [""]
            if surname[0] and surname[0] in blob:
                matched = candidate
                break
        if not matched:
            name = _public_name_from_title(result.get("title"), organisation)
            if not name:
                continue
            matched = {"name": name, "email": "", "phone": "", "sources": [], "conflicts": [], "ad_mentions": 0}
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
        score = min(45, int(candidate.get("ad_mentions") or 0) * 15)
        score += 15 if candidate.get("email") else 0
        score += 8 if candidate.get("phone") else 0
        score += min(20, len(public_sources) * 10)
        score += 12 if candidate.get("profile_url") else 0
        score -= min(30, len(candidate.get("conflicts") or []) * 15)
        candidate["confidence_score"] = max(0, min(100, score))
        candidate["confidence"] = "high" if score >= 70 else "medium" if score >= 40 else "low"
        candidate["candidate_id"] = _candidate_id(candidate.get("name"), candidate.get("email"))
        candidate["organisation"] = organisation
    return sorted(candidates, key=lambda item: (item["confidence_score"], item.get("ad_mentions", 0)), reverse=True)


def research_target_contacts(target, search_func=public_web_search):
    organisation = _clean(target.get("name") or target.get("target_name"))
    candidates = contacts_from_ad_evidence(target)
    queries = []
    for candidate in candidates[:5]:
        if candidate.get("name") and "@" not in candidate["name"]:
            queries.append(f'"{candidate["name"]}" "{organisation}" recruiter OR consultant')
    queries.extend([
        f'"{organisation}" recruitment consultants technology',
        f'site:linkedin.com/in "{organisation}" recruiter OR consultant OR talent',
        f'"{organisation}" team recruitment',
    ])
    results, errors = [], []
    for query in list(dict.fromkeys(queries))[:8]:
        try:
            results.extend(search_func(query, limit=5))
        except Exception as exc:  # public research is best-effort
            errors.append(f"{type(exc).__name__}: {exc}")
    deduped = {item.get("url"): item for item in results if item.get("url")}
    candidates = _attach_public_results(candidates, list(deduped.values()), organisation)
    candidates = _score_candidates(candidates, organisation)
    selected = candidates[0]["candidate_id"] if candidates else None
    ambiguous = False
    if candidates:
        top = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None
        ambiguous = bool(top.get("conflicts")) or top["confidence"] == "low" or bool(second and top["confidence_score"] - second["confidence_score"] < 12)
        if ambiguous:
            selected = None
    conflicts = list(dict.fromkeys(conflict for candidate in candidates for conflict in candidate.get("conflicts", [])))
    requires_selection = bool(candidates) and (ambiguous or not selected)
    return {
        "target_name": organisation,
        "researched_at": datetime.now().isoformat(timespec="seconds"),
        "candidates": candidates,
        "selected_candidate_id": selected,
        "requires_selection": requires_selection,
        "conflicts": conflicts,
        "public_results_checked": len(deduped),
        "warnings": (["No reliable person was found; the strategy will remain organisation-level."] if not candidates else []) + (["Public web research was unavailable; only job-ad contact evidence was used."] if errors and not deduped else []) + errors[:2],
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
            if researched >= datetime.now() - timedelta(days=7):
                return cached["research"]
        except (TypeError, ValueError):
            pass
    research = research_target_contacts(target)
    previous_selected = (cached or {}).get("selected_candidate_id")
    if previous_selected and any(item.get("candidate_id") == previous_selected for item in research.get("candidates", [])):
        research["selected_candidate_id"] = previous_selected
        research["requires_selection"] = False
    return db.save_hidden_market_contact_research(profile_id, target_type, target_key, target_name, research)["research"]
