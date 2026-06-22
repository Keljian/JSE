"""Guided local-LLM builder and smoke tester for scraper plugins."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import database_manager as db
import llm_handler
import scraper_plugins


APP_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(scraper_plugins.LOCAL_PLUGIN_DIR)
REPAIR_ROOT = Path(scraper_plugins.USER_PLUGIN_DIR)
REPAIR_BACKUP_ROOT = Path(scraper_plugins.DATA_DIR) / "scraper_repair_backups"
ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "bs4",
    "concurrency",
    "database_manager",
    "datetime",
    "html",
    "hashlib",
    "json",
    "lxml",
    "math",
    "re",
    "requests",
    "selenium",
    "scraping_helpers",
    "threading",
    "time",
    "traceback",
    "urllib",
    "urllib3",
}
BLOCKED_CALLS = {"eval", "exec", "compile", "open", "__import__", "input"}

SCRAPER_REFERENCE_PATH = APP_ROOT / "SCRAPER_REFERENCE.md"

_ATS_FINGERPRINTS = [
    (["pageuppeople.com"], "PageUp", "selenium",
     "PageUp portals render via JS. Use @scraper_resource_manager + scrape_job_details. "
     "Job links contain /job/ or /listing/ paths. See the monash/knox bundled plugins."),
    (["myworkdayjobs.com"], "Workday", "http_api",
     'Workday REST API: POST /wday/cxs/{tenant}/{site}/jobs with body '
     '{"searchText":"{keyword}","limit":20,"offset":0}. Tenant/site slugs are in the URL.'),
    (["boards.greenhouse.io", "greenhouse.io/boards"], "Greenhouse", "http_api",
     "Public JSON API: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true "
     "where {slug} is from the URL (e.g. boards.greenhouse.io/acme -> slug='acme')."),
    (["jobs.lever.co"], "Lever", "http_api",
     "Public JSON API: GET https://api.lever.co/v0/postings/{slug}?mode=json "
     "where {slug} is from the URL (e.g. jobs.lever.co/acme -> slug='acme')."),
    (["jobs.smartrecruiters.com"], "SmartRecruiters", "http_api",
     "JSON API: GET https://api.smartrecruiters.com/v1/companies/{company}/postings"
     "?keyword={keyword}&limit=100 where {company} is from the URL."),
    (["successfactors.com", "sapsf.com"], "SAP SuccessFactors", "selenium",
     "Heavy SPA. Use @scraper_resource_manager. Wait for job rows to load in main content area."),
    (["taleo.net", "tbe.taleo.net"], "Taleo", "selenium",
     "Legacy ATS. Use Selenium. Job listings appear in a table at /careersection/ paths."),
    (["bamboohr.com"], "BambooHR", "http_api",
     "JSON: GET https://{company}.bamboohr.com/careers/list returns a positions array."),
    (["recruitee.com"], "Recruitee", "http_api",
     "JSON API: GET https://{company}.recruitee.com/api/offers/?status=open."),
    (["ashbyhq.com", "jobs.ashbyhq.com"], "Ashby", "http_api",
     'JSON API: POST https://api.ashbyhq.com/posting-public/job/list '
     'with body {"organizationHostedJobsPageName":"{slug}"}.'),
    (["jobvite.com", "jobs.jobvite.com"], "Jobvite", "http_api",
     "JSON API: GET https://jobs.jobvite.com/api/job?c={company_code} returns jobs list."),
    (["nga.net", "ngahr.com"], "NGA.net", "selenium",
     "Use Selenium for NGA.net portals. Job links in table rows."),
]

_TIER_DIRECTIVE = {
    "jsonld": (
        "REQUIRED APPROACH [A — JSON-LD]: Static HTML contains <script type='application/ld+json'> "
        "JobPosting objects. Use requests to fetch, parse each script block with json.loads(). "
        "No Selenium needed."
    ),
    "embedded": (
        "REQUIRED APPROACH [B — Embedded JSON]: Job data is in __NEXT_DATA__ or __NUXT__ in the HTML. "
        "Use requests to fetch, regex-extract the JSON blob, parse with json.loads(). "
        "No Selenium needed."
    ),
    "http_api": (
        "REQUIRED APPROACH [C — JSON API]: The ATS exposes a documented REST API (see ATS hint above). "
        "Use requests.get/post against the API endpoint. No HTML parsing or Selenium needed."
    ),
    "http_bs4": (
        "REQUIRED APPROACH [D — Static HTML]: Job links are visible in the static HTTP response. "
        "Use requests + BeautifulSoup. Base your selectors on the container class hints shown below. "
        "No Selenium needed."
    ),
    "selenium": (
        "REQUIRED APPROACH [E — Selenium]: The site is JS-rendered or requires browser interaction. "
        "Use @scraper_resource_manager decorator + scrape_job_details helper from scraping_helpers. "
        "Assign scrape = your_decorated_function at module level."
    ),
    "uncertain": (
        "APPROACH [F — uncertain]: Reconnaissance was inconclusive. Try requests + BeautifulSoup first. "
        "If job data is absent from static HTML, use Selenium (@scraper_resource_manager). "
        "Note limitations clearly in dry_run warnings."
    ),
}

_MINIMAL_HTTP_EXAMPLE = """\
import json, re, requests
from bs4 import BeautifulSoup
import database_manager as db
from concurrency import cancel_event, paused, OperationCancelledError

def scrape(keyword, status_callback=None, log_callback=None, profile_id=1,
           base_url="", company_name="", location="", max_pages=3, dry_run=False, **config):
    log = log_callback or print
    sample_jobs, found = [], 0
    try:
        for page in range(1, max_pages + 1):
            if cancel_event.is_set():
                raise OperationCancelledError("Cancelled.")
            paused.wait()
            log(f"Fetching page {page}")
            resp = requests.get(base_url, params={"keyword": keyword, "page": page},
                                timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            # Option A — JSON API:
            data = resp.json()
            jobs = data.get("results") or data.get("jobs") or []
            # Option B — BeautifulSoup (replace Option A):
            # soup = BeautifulSoup(resp.text, "html.parser")
            # jobs = soup.select(".job-card")  # adapt selector from recon
            if not jobs:
                log(f"Page {page}: no results, stopping.")
                break
            for item in jobs:
                title = str(item.get("title") or "").strip()
                company_str = str(item.get("company") or company_name).strip()
                url = str(item.get("url") or "").strip()
                description = str(item.get("description") or "").strip()
                log(f"Found: {title}")
                if not title or not url:
                    continue
                job = {"title": title, "company": company_str, "location": location,
                       "url": url, "description": description, "search_keyword": keyword}
                if dry_run:
                    sample_jobs.append({"title": title, "company": company_str})
                    found += 1
                elif db.add_job(job, company_name, profile_id=profile_id, log_callback=log):
                    found += 1
            if dry_run and found:
                break
    except OperationCancelledError:
        raise
    except Exception as exc:
        log(f"Scraper error: {exc}")
        if dry_run:
            return {"ok": False, "found": 0, "sample_jobs": [], "warnings": [str(exc)]}
        return False
    if dry_run:
        return {"ok": found > 0, "found": found, "sample_jobs": sample_jobs[:3], "warnings": []}
    return found > 0"""

_MINIMAL_SELENIUM_EXAMPLE = """\
import time
from urllib.parse import urljoin
from selenium.webdriver.common.by import By
from scraping_helpers import scraper_resource_manager, scrape_job_details
import database_manager as db
from concurrency import cancel_event, paused, OperationCancelledError

@scraper_resource_manager(wait_timeout=20)
def _scrape_inner(driver, wait, keyword, status_callback, log_callback, location, max_pages,
                  base_url="", company_name="", profile_id=1, dry_run=False, **config):
    log = log_callback or print
    log(f"Loading {base_url}")
    driver.get(base_url)
    time.sleep(4)
    jobs_found = []
    for selector in ['a[href*="/job/"]', 'a[href*="/vacancy/"]', 'a[href*="/listing/"]',
                     '.job-title a', '.vacancy-title a', 'h3 a', 'h4 a']:
        try:
            links = driver.find_elements(By.CSS_SELECTOR, selector)
            log(f"Selector {selector!r}: {len(links)} elements")
            for link in links:
                title = (link.text or "").strip()
                href = link.get_attribute("href") or ""
                if title and href and len(title) > 5:
                    jobs_found.append({"title": title, "url": href,
                                       "company": company_name, "location": location})
            if jobs_found:
                break
        except Exception as exc:
            log(f"Selector {selector!r} failed: {exc}")
    if not jobs_found:
        warnings = [f"No job links found at {base_url}. Page title: {driver.title}"]
        log(warnings[0])
        if dry_run:
            return {"ok": False, "found": 0, "sample_jobs": [], "warnings": warnings}
        return False
    if dry_run:
        sample = [{"title": j["title"], "company": j["company"]} for j in jobs_found[:3]]
        return {"ok": True, "found": len(jobs_found), "sample_jobs": sample, "warnings": []}
    saved = scrape_job_details(driver, wait, jobs_found, log, profile_id)
    return saved > 0

scrape = _scrape_inner"""


def _slug(value):
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text or "custom_scraper"


def _json(data):
    return json.dumps(data or {}, indent=2, sort_keys=True)


def _existing_plugin_path(plugin_id):
    """Return an installed plugin directory for this id, if one exists.

    Seek and LinkedIn are conventional ids, not permanently reserved words.
    A generated plugin may use either id after the corresponding bundled or
    user plugin directory has actually been removed.
    """
    for root in (PLUGIN_ROOT, REPAIR_ROOT):
        candidate = Path(root) / plugin_id
        if candidate.exists():
            return candidate
    return None


def _extract_json_object(text):
    if isinstance(text, dict):
        return text
    raw = str(text or "")
    # Some OpenAI-compatible local models double-encode structured output, or
    # surround it with prose containing unrelated braces.  raw_decode lets us
    # select the first *complete* object instead of greedily taking everything
    # between the first and last brace.
    decoder = json.JSONDecoder()
    candidates = [raw]
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, str):
            candidates.insert(0, decoded)
    except (TypeError, json.JSONDecodeError):
        pass
    if hasattr(llm_handler, "_escape_control_chars_in_json_strings"):
        escaped = llm_handler._escape_control_chars_in_json_strings(raw)  # pylint: disable=protected-access
        if escaped != raw:
            candidates.append(escaped)
    for candidate in candidates:
        value_text = candidate.strip()
        try:
            value = json.loads(value_text)
            if isinstance(value, dict):
                return value
        except (TypeError, json.JSONDecodeError):
            pass
        try:
            value = ast.literal_eval(value_text)
            if isinstance(value, dict):
                return value
        except (SyntaxError, ValueError):
            pass
        decoded_objects = []
        for match in re.finditer(r"\{", candidate):
            try:
                value, consumed = decoder.raw_decode(candidate[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                decoded_objects.append((value, consumed))
        if decoded_objects:
            # Prefer the scraper envelope over nested objects such as manifest
            # or config_schema defaults, then prefer the most complete object.
            decoded_objects.sort(
                key=lambda item: (
                    "scraper_code" in item[0],
                    "manifest" in item[0],
                    len(item[0]),
                    item[1],
                ),
                reverse=True,
            )
            return decoded_objects[0][0]
        # A few small models emit a Python dict despite the JSON instruction.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                value = ast.literal_eval(candidate[start:end + 1])
                if isinstance(value, dict):
                    return value
            except (SyntaxError, ValueError):
                pass
    if not raw.strip():
        raise ValueError(
            "The local LLM returned an empty response. Check the selected model's LM Studio chat template "
            "and context/output limits."
        )
    if "{" not in raw:
        raise ValueError(
            f"The local LLM returned text but no JSON object ({len(raw)} characters)."
        )
    raise ValueError(
        "The local LLM returned malformed or truncated JSON for the scraper plugin."
    )


def _extract_python_code(text):
    """Extract a Python-only fallback response from a less capable local LLM."""
    raw = str(text or "").strip()
    fenced = re.findall(r"```(?:python|py)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    candidates = fenced + [raw]
    for candidate in candidates:
        code = candidate.strip()
        if "def scrape(" in code or ("scrape =" in code and "def " in code):
            return code
    if not raw:
        raise ValueError(
            "The local LLM returned an empty response. Check the selected model's LM Studio chat template "
            "and context/output limits."
        )
    raise ValueError("The local LLM did not return a complete Python scraper.")


RECON_TIMEOUT = 20
RECON_MAX_BYTES = 2_000_000
RECON_USER_AGENT = "Mozilla/5.0 (compatible; JSE-ScraperBuilder/1.0)"
_JOB_LINK_HINTS = ("job", "career", "vacanc", "position", "requisition", "/jobs/", "jobid", "job-id", "jr-")


def _load_reference_md():
    """Load SCRAPER_REFERENCE.md content for injection into the builder prompt."""
    try:
        if SCRAPER_REFERENCE_PATH.exists():
            return SCRAPER_REFERENCE_PATH.read_text(encoding="utf-8")[:4000]
    except Exception:
        pass
    return ""


def _detect_ats(html, url):
    """Return (ats_name, tier, hint) if the page/URL matches a known ATS, else (None, None, None)."""
    blob = f"{url or ''} {html or ''}".lower()
    for markers, ats_name, tier, hint in _ATS_FINGERPRINTS:
        if any(marker in blob for marker in markers):
            return ats_name, tier, hint
    return None, None, None


def _determine_tier(recon):
    """Determine the recommended scraping approach tier from reconnaissance data."""
    recon = recon or {}
    ats = recon.get("ats") or {}
    if ats.get("tier") == "http_api":
        return "http_api"
    if ats.get("tier") == "selenium":
        return "selenium"
    if (recon.get("jsonld") or {}).get("job_posting_found"):
        return "jsonld"
    if recon.get("embedded_state") or recon.get("next_data_sample"):
        return "embedded"
    if len(recon.get("candidate_links") or []) >= 3:
        return "http_bs4"
    render_hint = recon.get("render_hint") or ""
    if "client-side" in render_hint or "SPA" in render_hint.upper():
        return "selenium"
    if not recon.get("fetched"):
        return "uncertain"
    return "uncertain"


def _sample_job_card_html(soup, candidate_links):
    """Extract a short HTML snippet from the first detected job card container."""
    if not candidate_links or not soup:
        return ""
    try:
        first_href = candidate_links[0].get("href") or ""
        first_text = (candidate_links[0].get("text") or "")[:40]
        anchor = None
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            text = a.get_text() or ""
            if (first_href and first_href.endswith(href.split("?")[0][-40:])) or \
               (first_text and first_text[:20] in text):
                anchor = a
                break
        if anchor is None:
            return ""
        parent = anchor
        for _ in range(5):
            parent = getattr(parent, "parent", None)
            if parent is None:
                break
            tag = getattr(parent, "name", None)
            classes = parent.get("class") if hasattr(parent, "get") else None
            if tag in {"article", "li", "div", "tr", "section"} and classes:
                return str(parent)[:800]
        return str(anchor.parent)[:400] if getattr(anchor, "parent", None) else ""
    except Exception:
        return ""


def _pick_example_plugin(tier):
    """Return (code, plugin_name) for an installed working plugin matching the approach tier.

    Prefers simpler (shorter) plugins. Falls back to (None, None) if none qualify.
    """
    use_selenium = tier == "selenium"
    candidates = []
    for root in (PLUGIN_ROOT, REPAIR_ROOT):
        try:
            root_path = Path(root)
            if not root_path.exists():
                continue
            for plugin_dir in sorted(root_path.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                scraper_path = plugin_dir / "scraper.py"
                if not scraper_path.exists():
                    continue
                code = scraper_path.read_text(encoding="utf-8-sig")
                has_selenium = "scraper_resource_manager" in code or "webdriver" in code.lower()
                if use_selenium == has_selenium:
                    candidates.append((len(code), plugin_dir.name, code))
        except Exception:
            continue
    if not candidates:
        return None, None
    candidates.sort()
    _, name, code = candidates[0]
    lines = code.splitlines()
    trimmed = "\n".join(lines[:120])
    if len(lines) > 120:
        trimmed += "\n# ... (truncated for brevity)"
    return trimmed, name


def _looks_like_job_link(href, text):
    blob = f"{href or ''} {text or ''}".lower()
    return any(hint in blob for hint in _JOB_LINK_HINTS)


def _iter_jsonld_objects(data):
    if isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for item in data["@graph"]:
                yield from _iter_jsonld_objects(item)
        yield data
    elif isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_objects(item)


def _jsonld_jobposting_summary(soup):
    summary = {"job_posting_found": False}
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = block.string or block.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for obj in _iter_jsonld_objects(data):
            if not isinstance(obj, dict):
                continue
            types = obj.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any("JobPosting" in str(t) for t in types):
                summary["job_posting_found"] = True
                summary["sample_fields"] = sorted(str(k) for k in obj.keys())[:25]
                example = {
                    key: obj.get(key)
                    for key in ("title", "datePosted", "validThrough", "employmentType", "hiringOrganization", "jobLocation")
                    if key in obj
                }
                summary["example"] = json.dumps(example, default=str)[:600]
                return summary
    return summary


def _detect_embedded_state(html):
    markers = {
        "__NEXT_DATA__": "Next.js (__NEXT_DATA__)",
        "__NUXT__": "Nuxt (__NUXT__)",
        "__APOLLO_STATE__": "Apollo GraphQL state",
        "window.__INITIAL_STATE__": "Redux/initial state",
        "window.__PRELOADED_STATE__": "preloaded state",
    }
    return [label for token, label in markers.items() if token in html]


def _guess_render_type(html, soup):
    spa_markers = ("__NEXT_DATA__", "__NUXT__", "ng-version", "data-reactroot", 'id="root"', 'id="app"')
    job_anchors = [
        a for a in (soup.find_all("a", href=True) if soup else [])
        if _looks_like_job_link(a.get("href"), a.get_text())
    ]
    if len(job_anchors) >= 3:
        return "server-rendered (job links present in static HTML)"
    if any(marker in html for marker in spa_markers):
        return ("likely client-side rendered (few static job links, SPA markers present) — "
                "the listing probably comes from a JSON API or embedded state, not static HTML")
    return "uncertain (few static job links found in the fetched HTML)"


def _candidate_job_links(soup, base_url):
    from collections import Counter
    from urllib.parse import urljoin

    seen = set()
    links = []
    container_classes = Counter()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        text = " ".join((anchor.get_text() or "").split())
        if not _looks_like_job_link(href, text):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        container = ""
        parent = anchor
        for _ in range(3):
            parent = getattr(parent, "parent", None)
            if parent is None:
                break
            classes = parent.get("class") if hasattr(parent, "get") else None
            if classes:
                container = ".".join(classes[:3])
                container_classes[container] += 1
                break
        links.append({"text": text[:90], "href": absolute[:200], "container": container})
        if len(links) >= 20:
            break
    return links, [name for name, _ in container_classes.most_common(5)]


def _reconnoitre(url, keyword=None):
    """Best-effort fetch of the target page so selector generation is grounded in
    the real DOM. Never raises — reconnaissance is advisory."""
    findings = {"url": url, "fetched": False}
    if not url or not str(url).lower().startswith(("http://", "https://")):
        findings["error"] = "No fetchable http(s) URL provided."
        return findings
    fetch_url = url.replace("{keyword}", keyword or "") if (keyword and "{keyword}" in url) else url

    # ATS detection from the URL alone — fires even if the HTTP fetch fails
    ats_name_url, ats_tier_url, ats_hint_url = _detect_ats("", fetch_url)
    if ats_name_url:
        findings["ats"] = {"name": ats_name_url, "tier": ats_tier_url, "hint": ats_hint_url}

    try:
        import requests

        response = requests.get(fetch_url, timeout=RECON_TIMEOUT, headers={"User-Agent": RECON_USER_AGENT})
        findings["status_code"] = response.status_code
        response.raise_for_status()
        html = response.text[:RECON_MAX_BYTES]
        findings.update(fetched=True, final_url=response.url)
    except Exception as exc:  # noqa: BLE001 - recon is best-effort
        findings["error"] = f"{type(exc).__name__}: {exc}"
        return findings

    # Refine ATS detection with actual HTML content
    ats_name, ats_tier, ats_hint = _detect_ats(html[:50_000], findings.get("final_url") or fetch_url)
    if ats_name:
        findings["ats"] = {"name": ats_name, "tier": ats_tier, "hint": ats_hint}

    # __NEXT_DATA__ JSON sample (grab it before BeautifulSoup strips it)
    next_data_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    )
    if next_data_match:
        findings["next_data_sample"] = next_data_match.group(1)[:1500]

    soup = None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - parsing is advisory
        soup = None
    findings["jsonld"] = _jsonld_jobposting_summary(soup) if soup else {}
    findings["embedded_state"] = _detect_embedded_state(html)
    findings["render_hint"] = _guess_render_type(html, soup)
    if soup:
        if soup.title and soup.title.string:
            findings["page_title"] = soup.title.string.strip()[:160]
        base = findings.get("final_url") or fetch_url
        links, class_hints = _candidate_job_links(soup, base)
        findings["candidate_links"] = links
        findings["container_class_hints"] = class_hints
        findings["sample_card_html"] = _sample_job_card_html(soup, links)
    return findings


def _recon_section(recon):
    if not recon or not recon.get("fetched"):
        reason = (recon or {}).get("error") or "no URL supplied"
        return (f"SITE RECONNAISSANCE: unavailable ({reason}). "
                "Generate defensively, try multiple selector strategies, and add clear dry_run warnings.")
    lines = [
        f"SITE RECONNAISSANCE for {recon.get('final_url') or recon.get('url')} "
        "(use this real evidence — do not rely on assumptions):"
    ]
    if recon.get("page_title"):
        lines.append(f"- Page title: {recon['page_title']}")
    lines.append(f"- Render type: {recon.get('render_hint')}")

    ats = recon.get("ats") or {}
    if ats.get("name"):
        lines.append(f"- DETECTED ATS: {ats['name']} — {ats.get('hint', '')}")

    jsonld = recon.get("jsonld") or {}
    if jsonld.get("job_posting_found"):
        lines.append(
            "- JSON-LD JobPosting FOUND -> STRONGLY PREFER parsing <script type=\"application/ld+json\"> "
            f"JobPosting objects. Available fields: {', '.join(jsonld.get('sample_fields', []))}"
        )
        if jsonld.get("example"):
            lines.append(f"  Example JobPosting values: {jsonld['example']}")
    else:
        lines.append("- No JSON-LD JobPosting detected in static HTML.")

    if recon.get("embedded_state"):
        lines.append(
            f"- Embedded client-state detected: {', '.join(recon['embedded_state'])}. "
            "Job data is likely inside this JSON blob — parse it directly rather than scraping HTML."
        )

    next_data = recon.get("next_data_sample") or ""
    if next_data:
        lines.append(
            f"- __NEXT_DATA__ sample (first 1500 chars — inspect structure to locate the jobs array):\n{next_data}"
        )

    links = recon.get("candidate_links") or []
    if links:
        lines.append(f"- {len(links)} candidate job links found in static HTML (sample):")
        for link in links[:10]:
            lines.append(f"    text={link['text']!r} href={link['href']} container_class={link['container']!r}")
    else:
        lines.append(
            "- No obvious job links found in static HTML "
            "(the site may require a keyword in the URL or be JS-rendered)."
        )

    if recon.get("container_class_hints"):
        lines.append(f"- Most common job-card container classes: {', '.join(recon['container_class_hints'])}")

    card_html = recon.get("sample_card_html") or ""
    if card_html:
        lines.append(f"- Sample job card HTML (derive selectors from this real markup):\n{card_html}")

    tier = _determine_tier(recon)
    lines.append(f"\n{_TIER_DIRECTIVE.get(tier, _TIER_DIRECTIVE['uncertain'])}")
    return "\n".join(lines)


def _recon_public_summary(recon):
    recon = recon or {}
    return {
        "fetched": recon.get("fetched", False),
        "url": recon.get("final_url") or recon.get("url"),
        "render_hint": recon.get("render_hint"),
        "jsonld_jobposting": bool((recon.get("jsonld") or {}).get("job_posting_found")),
        "embedded_state": recon.get("embedded_state") or [],
        "next_data_found": bool(recon.get("next_data_sample")),
        "candidate_links": len(recon.get("candidate_links") or []),
        "container_class_hints": recon.get("container_class_hints") or [],
        "ats": recon.get("ats") or {},
        "tier": _determine_tier(recon),
        "error": recon.get("error"),
    }


def _feedback_section(feedback):
    if not feedback:
        return ""
    return (
        "\n\nPREVIOUS ATTEMPT FAILED ITS DRY RUN — return a corrected COMPLETE plugin and do "
        "not repeat the mistake. Diagnose why it found nothing or errored, then fix the "
        "fetching/parsing accordingly:\n" + feedback
    )


_HELPERS_REFERENCE = """\
AVAILABLE HELPERS (import from scraping_helpers):

1. scraper_resource_manager(wait_timeout=20) — DECORATOR for Selenium scrapers.
   Manages headless Chrome with stealth settings. Decorated function signature:
     def _inner(driver, wait, keyword, status_callback, log_callback, location, max_pages, **kwargs)
   Assign to scrape at module level:  scrape = _inner

2. scrape_job_details(driver, wait, jobs_list, log_callback, profile_id) — Selenium detail helper.
   Visits each {'title', 'url', 'company', 'location'} dict, extracts description, calls db.add_job.
   Returns int saved_count. Use instead of writing your own detail-page loop.

3. _get_pdf_text_from_url(pdf_url, base_url, log_callback) — downloads and extracts PDF text.

DATABASE:
  import database_manager as db
  db.add_job(job_dict, source_name, profile_id=profile_id, log_callback=log)
  job_dict keys: title, company, location, url, description, pdf_text, salary, search_keyword

CONCURRENCY:
  from concurrency import cancel_event, paused, OperationCancelledError
  if cancel_event.is_set(): raise OperationCancelledError("Cancelled.")
  paused.wait()  # blocks while user has paused scraping\
"""

_DRY_RUN_CONTRACT = """\
DRY-RUN CONTRACT (the test harness checks this exactly):
  When dry_run=True:
    - Fetch/parse at most 1-2 pages. Do NOT call db.add_job().
    - Return this exact dict:
        {"ok": True, "found": <int jobs parsed>, "sample_jobs": [{"title":"...","company":"..."}], "warnings": []}
    - "ok" is True when at least one job was successfully parsed.
  When dry_run=False:
    - Call db.add_job() for every job. Return True if any were stored, else False.\
"""


def _builder_prompt(answers, recon=None, feedback=None):
    plugin_id = _slug(answers.get("plugin_id") or answers.get("source_name") or answers.get("name"))
    source_name = answers.get("source_name") or answers.get("name") or plugin_id.replace("_", " ").title()
    mode = answers.get("mode") if answers.get("mode") in {"keyword", "sweep"} else "keyword"
    default_keyword = answers.get("test_keyword") or "business analyst"
    max_pages = int(answers.get("max_pages") or 3)

    tier = _determine_tier(recon) if recon else "uncertain"
    mode_note = (
        'mode="keyword": scraper takes a keyword and queries the site.'
        ' mode="sweep": scraper ignores keyword and fetches all open jobs (e.g. a single employer careers page).'
    )

    # Pick a working installed plugin as a concrete example, or fall back to the minimal template.
    example_code, example_name = _pick_example_plugin(tier)
    if not example_code:
        example_code = _MINIMAL_SELENIUM_EXAMPLE if tier == "selenium" else _MINIMAL_HTTP_EXAMPLE
        example_name = "built-in reference"

    reference_md = _load_reference_md()

    parts = [
        f"Build a JSE scraper plugin from these answers:\n{_json(answers)}",
        "",
        "CRITICAL MISTAKES — these cause immediate failure, DO NOT make them:",
        '  BAD:  "config_schema": {"base_url": {"type": "text", "default": ""}}  <- DICT, wrong!',
        '  GOOD: "config_schema": [{"key": "base_url", "label": "Base Url", "type": "text", "default": ""}]  <- LIST',
        "  BAD:  import database_manager; database_manager.add_job(...)  <- wrong",
        "  BAD:  from database_manager import db; db.add_job(...)  <- wrong",
        "  GOOD: import database_manager as db; db.add_job(...)  <- correct",
        "  BAD:  if paused.is_set(): time.sleep(1)  <- wrong, paused.is_set() means NOT paused",
        "  BAD:  while paused.is_set(): time.sleep(0.5)  <- wrong",
        "  GOOD: paused.wait()  <- correct, blocks while user has paused; resumes automatically",
        "  BAD:  'found': True  <- found must be an int (count of jobs parsed), not a bool",
        "  GOOD: 'found': len(jobs_list)  <- correct",
        "  BAD:  if keyword and keyword.lower() not in title.lower(): continue  <- NEVER filter job titles by keyword text",
        "  GOOD: pass keyword as a URL search param (e.g. ?q=keyword); if the site has no search, return ALL jobs",
        "  BAD:  changing mode from 'sweep' to 'keyword' in the manifest  <- never override the mode from the answers",
        "",
        "PLUGIN CONTRACT:",
        f'- manifest.id must be "{plugin_id}".',
        f'- manifest.name/source_name should be "{source_name}".',
        '- manifest.module must be "scraper.py", manifest.callable must be "scrape".',
        f'- manifest.mode must be "{mode}". ({mode_note})',
        "- config_schema should include base_url, company_name, location, max_pages, test_keyword.",
        "- scraper.py must define:",
        f'    def scrape(keyword, status_callback=None, log_callback=None, profile_id=1,',
        f'               base_url="", company_name="", location="", max_pages={max_pages}, dry_run=False, **config):',
        f'- default keyword for testing: "{default_keyword}", page limit: {max_pages}.',
        "- Keep scraper_code concise (under 150 lines). Use helpers — do not reinvent WebDriver setup or detail loops.",
        "- Log selectors tried and element counts: log(f\"Selector X: {N} elements\") — helps repair if it fails.",
        "- Never include personal information.",
        "",
        _HELPERS_REFERENCE,
        "",
        _DRY_RUN_CONTRACT,
        "",
        _recon_section(recon),
        _feedback_section(feedback),
        "",
        f"EXAMPLE WORKING PLUGIN (from '{example_name}' — follow this exact structure):",
        example_code,
    ]
    if reference_md:
        parts.insert(2, f"\nPROJECT REFERENCE:\n{reference_md}\n")

    parts.append(
        '\nReturn valid JSON in exactly this shape (no markdown fences, no text outside the JSON):\n'
        '{\n'
        '  "manifest": {...},\n'
        '  "scraper_code": "complete Python source",\n'
        '  "readme": "short markdown",\n'
        '  "notes": ["..."],\n'
        '  "test_plan": ["..."]\n'
        '}'
    )

    return [
        {
            "role": "system",
            "content": (
                "You write safe, concise JSE scraper plugins. "
                "Return only valid JSON — no markdown fences, no text outside the JSON object. "
                "The JSON must contain exactly: manifest, scraper_code, readme, notes, test_plan. "
                "scraper_code must be complete runnable Python under 150 lines. "
                "RULES: config_schema must be a JSON ARRAY of objects, never a dict. "
                "Import database_manager as 'import database_manager as db', never 'from database_manager import db'. "
                "Use 'paused.wait()' for pause support, never 'paused.is_set()'. "
                "In dry_run return, 'found' must be an integer count, never a boolean. "
                "NEVER filter job listings by keyword text in the title — pass keyword as a URL parameter instead, or fetch all jobs. "
                "NEVER change the mode field in the manifest from what the user specified. "
                "Use the available helpers (scraper_resource_manager, scrape_job_details) — do not reimplement them. "
                "The scraper must be conservative, cancellable, and must never perform filesystem writes, "
                "subprocess calls, shell calls, or credential handling."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(parts),
        },
    ]


def _builder_code_prompt(answers, recon=None, feedback=None):
    """Compact Python-only fallback for models that struggle to JSON-escape code."""
    plugin_id = _slug(answers.get("plugin_id") or answers.get("source_name") or answers.get("name"))
    source_name = answers.get("source_name") or answers.get("name") or plugin_id.replace("_", " ").title()
    max_pages = int(answers.get("max_pages") or 3)
    parts = [
        f"Write scraper.py only for JSE source {source_name!r} (plugin id {plugin_id!r}).",
        f"Careers URL: {answers.get('careers_url') or answers.get('base_url') or ''}",
        f"Company: {answers.get('company_name') or source_name}",
        f"Default location: {answers.get('location') or ''}",
        f"Test keyword: {answers.get('test_keyword') or 'business analyst'}; max pages: {max_pages}",
        f"Mode: {answers.get('mode') or 'keyword'}; platform hint: {answers.get('platform_hint') or 'none'}",
        f"User notes: {answers.get('notes') or 'none'}",
        "",
        _HELPERS_REFERENCE,
        "",
        _DRY_RUN_CONTRACT,
        "",
        _recon_section(recon),
        _feedback_section(feedback),
        "",
        "Define scrape(keyword, status_callback=None, log_callback=None, profile_id=1, "
        f"base_url=\"\", company_name=\"\", location=\"\", max_pages={max_pages}, "
        "dry_run=False, **config), or assign scrape to a decorated function with those arguments.",
        "Keep it under 150 lines and use only the imports/helpers allowed by the reference above.",
        "Return ONLY complete Python source. Do not use JSON and do not use markdown fences.",
    ]
    return [
        {"role": "system", "content": "You write safe, concise, runnable JSE Python scraper plugins."},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _normalise_generation(data, answers):
    plugin_id = _slug(answers.get("plugin_id") or data.get("manifest", {}).get("id") or answers.get("source_name"))
    manifest = dict(data.get("manifest") or {})
    manifest.update({
        "id": plugin_id,
        "module": "scraper.py",
        "callable": manifest.get("callable") or "scrape",
        # answers.mode takes priority — the user chose it explicitly; the model
        # frequently overrides "sweep" → "keyword" which breaks single-employer pages.
        "mode": answers.get("mode") or (manifest.get("mode") if manifest.get("mode") in {"keyword", "sweep"} else "keyword"),
    })
    manifest["name"] = manifest.get("name") or answers.get("source_name") or plugin_id.replace("_", " ").title()
    manifest["source_name"] = manifest.get("source_name") or manifest["name"]
    manifest["version"] = manifest.get("version") or "0.1.0"
    schema = manifest.get("config_schema") or []
    # The LLM frequently generates config_schema as {"key": {"type": ..., "default": ...}}
    # instead of the required [{"key": "...", "label": "...", "type": "...", "default": ...}].
    if isinstance(schema, dict):
        schema = [
            {
                "key": k,
                "label": k.replace("_", " ").title(),
                "type": "number" if k in ("max_pages",) else "text",
                "default": v.get("default", "") if isinstance(v, dict) else v,
            }
            for k, v in schema.items()
        ]
    keys = {item.get("key") for item in schema if isinstance(item, dict)}
    defaults = {
        "base_url": answers.get("careers_url") or answers.get("base_url") or "",
        "company_name": answers.get("company_name") or manifest["source_name"],
        "location": answers.get("location") or "",
        "max_pages": int(answers.get("max_pages") or 3),
        "test_keyword": answers.get("test_keyword") or "",
    }
    for key, value in defaults.items():
        if key not in keys:
            schema.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "type": "number" if key == "max_pages" else "text",
                "default": value,
                **({"legacy_key": "max_pages"} if key == "max_pages" else {}),
            })
    manifest["config_schema"] = schema
    code = str(data.get("scraper_code") or "").strip()
    if not code:
        raise ValueError("The local LLM did not return scraper_code.")
    return {
        "manifest": manifest,
        "scraper_code": code,
        "readme": str(data.get("readme") or "").strip(),
        "notes": data.get("notes") if isinstance(data.get("notes"), list) else [],
        "test_plan": data.get("test_plan") if isinstance(data.get("test_plan"), list) else [],
    }


def _validate_code(code):
    try:
        tree = ast.parse(code, filename="scraper.py")
    except SyntaxError as exc:
        raise ValueError(f"Generated scraper.py has a syntax error: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root and root not in ALLOWED_IMPORT_ROOTS:
                    raise ValueError(f"Generated scraper imports blocked module: {name}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                raise ValueError(f"Generated scraper uses blocked call: {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in {"system", "popen", "remove", "unlink", "rmdir", "rmtree"}:
                raise ValueError(f"Generated scraper uses blocked method: {func.attr}")
    # Validate the `scrape` signature directly from the AST. We deliberately do
    # NOT exec the generated module: executing untrusted top-level code (with
    # full builtins) would defeat the static blocklist above and run any
    # import-time side effects of the generated plugin.
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    scrape_node = functions.get("scrape")
    if scrape_node is None:
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if any(isinstance(target, ast.Name) and target.id == "scrape" for target in targets):
                if isinstance(value, ast.Name):
                    scrape_node = functions.get(value.id)
                break
    if scrape_node is None:
        raise ValueError("Generated scraper.py must define a callable scrape function.")
    args = scrape_node.args
    param_names = {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)}
    for required in ("keyword", "profile_id"):
        if required not in param_names:
            raise ValueError(f"Generated scrape function must accept {required}.")
    if args.kwarg is None:
        raise ValueError(
            "Generated scrape function must accept a **config catch-all parameter "
            "(e.g. def scrape(keyword, ..., **config))."
        )
    return True


def _generate_once(answers, recon, feedback=None, temperature=0.15, output_mode="structured_json"):
    """Single generation pass: LLM call (optionally with repair feedback),
    normalisation, and static validation. Reconnaissance is computed once by the
    caller and reused across repair attempts."""
    python_only = output_mode == "python"
    response = llm_handler._call_unsloth(  # pylint: disable=protected-access
        _builder_code_prompt(answers, recon, feedback) if python_only else _builder_prompt(answers, recon, feedback),
        temperature=temperature,
        max_tokens=8000,
        json_mode=output_mode == "structured_json",
    )
    raw_data = (
        {"manifest": {}, "scraper_code": _extract_python_code(response), "notes": [], "test_plan": []}
        if python_only
        else _extract_json_object(response)
    )
    data = _normalise_generation(raw_data, answers)
    scraper_plugins.validate_manifest(data["manifest"])
    _validate_code(data["scraper_code"])
    data["reconnaissance"] = _recon_public_summary(recon)
    return data


def generate_plugin(answers):
    answers = dict(answers or {})
    recon = _reconnoitre(
        answers.get("careers_url") or answers.get("base_url"),
        keyword=answers.get("test_keyword"),
    )
    return _generate_once(answers, recon)


def save_generated_plugin(generated):
    manifest = generated["manifest"]
    plugin_id = _slug(manifest["id"])
    PLUGIN_ROOT.mkdir(parents=True, exist_ok=True)
    target = PLUGIN_ROOT / plugin_id
    if _existing_plugin_path(plugin_id):
        raise ValueError(
            f"A scraper plugin named '{plugin_id}' already exists. "
            "Choose a different name, or remove the existing plugin first."
        )
    target.mkdir(parents=True, exist_ok=True)
    (target / "scraper-plugin.json").write_text(_json(manifest), encoding="utf-8")
    (target / "scraper.py").write_text(generated["scraper_code"].rstrip() + "\n", encoding="utf-8")
    if generated.get("readme"):
        (target / "README.md").write_text(generated["readme"].rstrip() + "\n", encoding="utf-8")
    plugin = scraper_plugins.install_from_path(target)
    return plugin, target


def _write_candidate(generated, plugins_root):
    """Write a candidate plugin into a (temporary) plugin root for sandboxed
    testing, without touching the real plugins directory."""
    manifest = generated["manifest"]
    plugin_id = _slug(manifest["id"])
    target = Path(plugins_root) / plugin_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "scraper-plugin.json").write_text(_json(manifest), encoding="utf-8")
    (target / "scraper.py").write_text(generated["scraper_code"].rstrip() + "\n", encoding="utf-8")
    return plugin_id


def _summarise_test_failure(generated, test):
    """Build repair feedback from a failed dry-run for the next generation pass."""
    parts = []
    if not test:
        parts.append("The dry run produced no result.")
    else:
        if test.get("error"):
            parts.append(f"Dry run raised an error: {test['error']}")
        result = test.get("result")
        if isinstance(result, dict):
            if not result.get("found"):
                parts.append("Dry run found 0 jobs — the fetch/selectors/parse are wrong, or the page is JS-rendered and needs the JSON API / embedded state.")
            warnings = result.get("warnings") or []
            if warnings:
                parts.append("Scraper warnings: " + "; ".join(str(w) for w in warnings[:5]))
        elif result not in (True, None):
            parts.append(f"Dry run returned {result!r} instead of storing jobs.")
        logs = test.get("logs") or []
        if logs:
            parts.append("Recent dry-run logs:\n" + "\n".join(logs[-12:]))
    prior_code = (generated or {}).get("scraper_code") or ""
    feedback = "\n".join(parts) or "Dry run did not succeed."
    if prior_code:
        feedback += "\n\nYour previous scraper.py (correct it, keep what worked):\n" + prior_code[:6000]
    return feedback


def build_and_install(answers, max_attempts=None, log_callback=None):
    """Generate a scraper plugin, dry-run it in a sandbox, and self-repair on
    failure before installing the best candidate.

    Each candidate is written to a throwaway plugin directory and tested in the
    isolated subprocess (``test_plugin``), so untrusted code never runs in this
    process, never touches the live database, and never lands in the real plugins
    directory until a working (or final) version is chosen.
    """
    answers = dict(answers or {})

    def log(message):
        if log_callback:
            log_callback(str(message))

    try:
        max_attempts = int(answers.get("max_attempts") or max_attempts or 3)
    except (TypeError, ValueError):
        max_attempts = 3
    max_attempts = max(1, min(max_attempts, 4))

    # Fail fast on id collisions before spending any LLM calls (mirrors the
    # final save_generated_plugin guards; the id is deterministic from answers).
    intended_id = _slug(answers.get("plugin_id") or answers.get("source_name") or answers.get("name"))
    if _existing_plugin_path(intended_id):
        raise ValueError(
            f"A scraper plugin named '{intended_id}' already exists. "
            "Choose a different name, or remove the existing plugin first."
        )

    recon = _reconnoitre(
        answers.get("careers_url") or answers.get("base_url"),
        keyword=answers.get("test_keyword"),
    )
    log(f"Reconnaissance: {'fetched ' + str(recon.get('final_url') or recon.get('url')) if recon.get('fetched') else 'unavailable (' + str(recon.get('error')) + ')'}")
    try:
        max_pages = int(answers.get("max_pages") or 1)
    except (TypeError, ValueError):
        max_pages = 1
    keyword = answers.get("test_keyword")
    profile_id = answers.get("profile_id") or 1

    history = []
    feedback = None
    best = None          # generated dict that passed its dry run
    last_valid = None    # last dict that at least validated
    last_test = None
    last_error = None

    with tempfile.TemporaryDirectory(prefix="jse_scraper_build_") as tmp_plugins:
        for attempt in range(1, max_attempts + 1):
            output_mode = "structured_json" if attempt == 1 else ("json_text" if attempt == 2 else "python")
            mode_label = {
                "structured_json": "structured JSON",
                "json_text": "portable JSON text",
                "python": "compact Python fallback",
            }[output_mode]
            log(f"Generating scraper (attempt {attempt}/{max_attempts}, {mode_label})...")
            temperature = 0.15 if attempt == 1 else 0.07
            try:
                generated = _generate_once(
                    answers, recon, feedback, temperature=temperature, output_mode=output_mode
                )
            except Exception as exc:  # noqa: BLE001 - feed generation errors back in
                last_error = exc
                feedback = f"The previous output failed generation/validation: {type(exc).__name__}: {exc}"
                history.append({
                    "attempt": attempt,
                    "ok": False,
                    "stage": "generate",
                    "output_mode": output_mode,
                    "error": str(exc),
                })
                log(f"Attempt {attempt} failed to generate valid code: {exc}")
                continue
            last_valid = generated

            plugin_id = _write_candidate(generated, tmp_plugins)
            log(f"Attempt {attempt}: dry-running '{plugin_id}' in sandbox...")
            try:
                test = test_plugin(
                    plugin_id,
                    profile_id=profile_id,
                    keyword=keyword,
                    max_pages=max_pages,
                    plugin_root=tmp_plugins,
                )
            except Exception as exc:  # noqa: BLE001 - subprocess/runtime failure
                test = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "logs": []}
            last_test = test

            found = test.get("result", {}).get("found") if isinstance(test.get("result"), dict) else None
            history.append({"attempt": attempt, "ok": bool(test.get("ok")), "found": found, "error": test.get("error")})
            if test.get("ok"):
                best = generated
                log(f"Attempt {attempt} passed the dry run.")
                break
            feedback = _summarise_test_failure(generated, test)
            log(f"Attempt {attempt} did not pass (found={found}). Repairing...")

    chosen = best or last_valid
    if chosen is None:
        raise ValueError(
            f"Scraper generation failed after {max_attempts} attempt(s). Last error: {last_error}"
        )
    plugin, target = save_generated_plugin(chosen)
    log("Installed verified scraper." if best else "Installed best-effort scraper (dry run did not pass — review and edit).")
    return {
        "plugin": plugin,
        "plugin_dir": str(target),
        "manifest": chosen["manifest"],
        "notes": chosen.get("notes") or [],
        "test_plan": chosen.get("test_plan") or [],
        "readme": chosen.get("readme") or "",
        "reconnaissance": chosen.get("reconnaissance") or {},
        "verified": bool(best),
        "attempts": len(history),
        "attempt_history": history,
        "test": last_test,
    }


def _run_plugin_smoke_test(plugin_id, profile_id=1, keyword=None, max_pages=1):
    """Core dry-run smoke test.

    Runs against whatever database the current process is pointed at, so callers
    MUST isolate it before invoking with untrusted plugin code. ``test_plugin``
    is the public entry point and runs this inside a sandboxed subprocess.
    """
    scraper_plugins.ensure_registered()
    plugin = scraper_plugins.get_plugin(plugin_id, profile_id=profile_id, include_disabled=True)
    if not plugin:
        raise ValueError(f"Unknown scraper plugin: {plugin_id}")
    func = scraper_plugins.load_callable(plugin)
    config = scraper_plugins.build_config(plugin, {"max_pages": max_pages})
    config["max_pages"] = min(int(config.get("max_pages") or max_pages or 1), 2)
    config["dry_run"] = True
    test_keyword = keyword or config.get("test_keyword") or plugin.get("source_name") or plugin_id
    logs = []

    def log(message):
        logs.append(str(message))

    # Safety net: dry_run is requested, but a generated scraper is not guaranteed
    # to honour it. Suppress writes to the live database for the duration of the
    # smoke test so a misbehaving plugin cannot pollute real job data. The
    # generated contract uses `import database_manager as db; db.add_job(...)`,
    # so patching the module attribute covers the supported call style.
    suppressed = []

    def _suppressed_add_job(job_data, source, profile_id=1, log_callback=None):
        suppressed.append((job_data or {}).get("title"))
        if log_callback:
            log_callback("dry-run: db.add_job suppressed during plugin test.")
        return False

    original_add_job = db.add_job
    db.add_job = _suppressed_add_job
    try:
        result = func(
            keyword=test_keyword,
            status_callback=log,
            log_callback=log,
            profile_id=profile_id,
            **config,
        )
    finally:
        db.add_job = original_add_job

    if result is True:
        ok = True
    elif isinstance(result, dict):
        # A dict missing "ok" was previously treated as success (None is not
        # False); infer from found/sample_jobs instead.
        ok = bool(result["ok"]) if "ok" in result else bool(result.get("found") or result.get("sample_jobs"))
    else:
        # Older plugins predate the dry_run return contract. Suppressed writes
        # prove they parsed usable jobs even when db.add_job(False) made their
        # final boolean result false.
        ok = bool(suppressed)
    return {
        "ok": ok,
        "plugin": plugin,
        "keyword": test_keyword,
        "result": result,
        "suppressed_writes": len(suppressed),
        "logs": logs[-50:],
    }


def test_plugin(plugin_id, profile_id=1, keyword=None, max_pages=1, plugin_root=None):
    """Smoke-test a scraper plugin in a fully isolated subprocess.

    Generated/imported plugin code is untrusted. The test runs in a child
    process whose ``JSE_DATA_DIR`` points at a fresh throwaway database, so even
    if the plugin ignores ``dry_run``, bypasses the in-process ``db.add_job``
    guard, or writes via raw SQLite/filesystem calls, it can only ever touch the
    disposable data dir — the live database is never opened by the child.

    When ``plugin_root`` is supplied, the child also discovers plugins only from
    that directory (via ``JSE_LOCAL_PLUGIN_DIR``), which lets the build/repair
    loop test a candidate from a temp folder without installing it.
    """
    tmp_dir = tempfile.mkdtemp(prefix="jse_scraper_test_")
    result_path = os.path.join(tmp_dir, "result.json")
    request = {
        "plugin_id": plugin_id,
        "profile_id": profile_id,
        "keyword": keyword,
        "max_pages": max_pages,
        "result_path": result_path,
    }
    env = dict(os.environ)
    env["JSE_DATA_DIR"] = tmp_dir
    if plugin_root:
        env["JSE_LOCAL_PLUGIN_DIR"] = str(plugin_root)
    try:
        try:
            proc = subprocess.run(
                [sys.executable or "python", "-c",
                 "import scraper_plugin_builder as b; b._isolated_test_main()"],
                input=json.dumps(request),
                cwd=str(APP_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Scraper plugin test timed out after 180s.")
        if not os.path.exists(result_path):
            detail = (proc.stderr or proc.stdout or "no output").strip()[-1500:]
            raise ValueError(f"Scraper plugin test failed to produce a result:\n{detail}")
        with open(result_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if isinstance(payload, dict) and payload.get("error"):
        raise ValueError(payload["error"])
    return payload


def diagnose_plugin(plugin_id, profile_id=1, keyword=None, max_pages=1):
    """Run a disposable dry-run and update the plugin's structural health."""
    plugin = scraper_plugins.get_plugin(plugin_id, profile_id=profile_id, include_disabled=True)
    if not plugin:
        raise ValueError(f"Unknown scraper plugin: {plugin_id}")
    try:
        test = test_plugin(plugin_id, profile_id=profile_id, keyword=keyword, max_pages=max_pages)
        outcome = "success" if test.get("ok") else "error"
        error = test.get("error") or (None if test.get("ok") else "Dry run did not produce usable listings.")
        health = db.record_scraper_health(plugin_id, outcome, error)
        return {"ok": bool(test.get("ok")), "test": test, "health": health}
    except Exception as exc:  # noqa: BLE001 - diagnosis must return actionable evidence
        health = db.record_scraper_health(plugin_id, "error", exc)
        return {
            "ok": False,
            "test": {"ok": False, "error": f"{type(exc).__name__}: {exc}", "logs": []},
            "health": health,
        }


def _installed_plugin_source(plugin):
    install_path = plugin.get("install_path")
    module_name = (plugin.get("manifest") or {}).get("module")
    if not install_path or not module_name:
        raise ValueError("This scraper has no editable plugin source directory.")
    root = Path(install_path)
    source = root / module_name
    manifest_path = root / "scraper-plugin.json"
    if not source.exists() or not manifest_path.exists():
        raise ValueError("The installed scraper source or manifest is missing.")
    return root, source.read_text(encoding="utf-8-sig")


def _promote_repair(plugin, generated, diagnosis, test):
    """Install a verified repair as a data-directory override, preserving rollback."""
    source_root, _ = _installed_plugin_source(plugin)
    plugin_id = plugin["id"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = REPAIR_BACKUP_ROOT / plugin_id / stamp
    target = REPAIR_ROOT / plugin_id
    staging = REPAIR_ROOT / f".{plugin_id}-repair-{stamp}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    REPAIR_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, backup)
    try:
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        (staging / "scraper-plugin.json").write_text(_json(generated["manifest"]), encoding="utf-8")
        (staging / "scraper.py").write_text(generated["scraper_code"].rstrip() + "\n", encoding="utf-8")
        if generated.get("readme"):
            (staging / "README.md").write_text(generated["readme"].rstrip() + "\n", encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        scraper_plugins.discover_user_plugins()
        repair_id = db.record_scraper_repair(
            plugin_id,
            "applied",
            backup_path=str(backup),
            installed_path=str(target),
            diagnosis=diagnosis,
            test=test,
        )
        db.record_scraper_health(plugin_id, "success")
        return repair_id, target, backup
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(backup, target)
        scraper_plugins.discover_user_plugins()
        raise


def repair_plugin(plugin_id, profile_id=1, keyword=None, max_pages=1,
                  max_attempts=3, log_callback=None):
    """Diagnose an installed plugin, generate candidates, and promote only a verified repair."""
    plugin = scraper_plugins.get_plugin(plugin_id, profile_id=profile_id, include_disabled=True)
    if not plugin:
        raise ValueError(f"Unknown scraper plugin: {plugin_id}")
    _, current_code = _installed_plugin_source(plugin)
    diagnosis = diagnose_plugin(plugin_id, profile_id=profile_id, keyword=keyword, max_pages=max_pages)
    manifest = plugin.get("manifest") or {}
    config = scraper_plugins.build_config(plugin, {})
    answers = {
        "plugin_id": plugin_id,
        "source_name": plugin.get("source_name") or plugin.get("name"),
        "company_name": config.get("company_name") or plugin.get("source_name"),
        "careers_url": config.get("base_url") or "",
        "base_url": config.get("base_url") or "",
        "location": config.get("location") or "",
        "mode": manifest.get("mode") or "keyword",
        "test_keyword": keyword or config.get("test_keyword") or "business analyst",
        "max_pages": min(max(int(max_pages or 1), 1), 2),
        "notes": "Repair an installed scraper. Preserve its source semantics and make the smallest robust correction.",
        "platform_hint": "Existing scraper uses Selenium; preserve browser automation where necessary." if "selenium" in current_code else "",
    }
    recon = _reconnoitre(answers["careers_url"], keyword=answers["test_keyword"])
    prior_test = diagnosis.get("test") or {}
    feedback = _summarise_test_failure({"scraper_code": current_code}, prior_test)
    feedback += "\n\nInstalled manifest:\n" + _json(manifest)
    history = []
    try:
        attempts = max(1, min(int(max_attempts or 3), 4))
    except (TypeError, ValueError):
        attempts = 3

    def log(message):
        if log_callback:
            log_callback(str(message))

    with tempfile.TemporaryDirectory(prefix="jse_scraper_repair_") as tmp_plugins:
        for attempt in range(1, attempts + 1):
            log(f"Repairing {plugin.get('name') or plugin_id} (attempt {attempt}/{attempts})...")
            generated = None
            temperature = 0.10 if attempt == 1 else 0.05
            try:
                generated = _generate_once(answers, recon, feedback, temperature=temperature)
                _write_candidate(generated, tmp_plugins)
                test = test_plugin(
                    plugin_id,
                    profile_id=profile_id,
                    keyword=answers["test_keyword"],
                    max_pages=answers["max_pages"],
                    plugin_root=tmp_plugins,
                )
            except Exception as exc:  # noqa: BLE001 - feed all candidate failures back
                test = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "logs": []}
            history.append({"attempt": attempt, "ok": bool(test.get("ok")), "error": test.get("error")})
            if generated and test.get("ok"):
                repair_id, target, backup = _promote_repair(plugin, generated, diagnosis, test)
                return {
                    "ok": True,
                    "repair_id": repair_id,
                    "plugin_dir": str(target),
                    "backup_dir": str(backup),
                    "diagnosis": diagnosis,
                    "test": test,
                    "attempt_history": history,
                }
            feedback = _summarise_test_failure(generated or {"scraper_code": current_code}, test)
    error = history[-1].get("error") if history else "No repair candidate was produced."
    db.record_scraper_repair(plugin_id, "rejected", diagnosis=diagnosis, test=history, error=error)
    return {"ok": False, "diagnosis": diagnosis, "attempt_history": history, "error": error}


def rollback_plugin_repair(plugin_id):
    repair = db.get_latest_applied_scraper_repair(plugin_id)
    if not repair:
        raise ValueError("No applied repair is available to roll back.")
    backup = Path(repair.get("backup_path") or "")
    target = Path(repair.get("installed_path") or "")
    if not backup.exists() or not target.parent.exists():
        raise ValueError("The repair backup is missing.")
    staging = target.parent / f".{plugin_id}-rollback"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(backup, staging)
    if target.exists():
        shutil.rmtree(target)
    staging.replace(target)
    scraper_plugins.discover_user_plugins()
    db.mark_scraper_repair_rolled_back(repair["id"])
    db.record_scraper_health(plugin_id, "success")
    return {"ok": True, "plugin_id": plugin_id, "restored_from": str(backup)}


def _isolated_test_main():
    """Entry point run inside the sandboxed test subprocess.

    JSE_DATA_DIR is already pointed at a throwaway directory by the parent, so
    database_manager (imported fresh here) targets the disposable DB. The result
    is written to a file rather than stdout to avoid any contamination from
    plugin diagnostics printed to stdout.
    """
    request = json.loads(sys.stdin.read() or "{}")
    result_path = request.get("result_path")
    try:
        import db_setup
        db_setup.setup_database()
        payload = _run_plugin_smoke_test(
            request["plugin_id"],
            profile_id=request.get("profile_id", 1),
            keyword=request.get("keyword"),
            max_pages=request.get("max_pages", 1),
        )
    except Exception as exc:  # noqa: BLE001 - report any failure back to the parent
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    if result_path:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
