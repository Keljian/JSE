"""Guided local-LLM builder and smoke tester for scraper plugins."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import database_manager as db
import llm_handler
import scraper_plugins


APP_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(scraper_plugins.LOCAL_PLUGIN_DIR)
ALLOWED_IMPORT_ROOTS = {
    "bs4",
    "concurrency",
    "database_manager",
    "datetime",
    "html",
    "json",
    "lxml",
    "math",
    "re",
    "requests",
    "scraping_helpers",
    "time",
    "urllib",
}
# Built-in scraper ids that a generated plugin must never overwrite.
RESERVED_PLUGIN_IDS = {"seek", "linkedin"}
BLOCKED_CALLS = {"eval", "exec", "compile", "open", "__import__", "input"}


def _slug(value):
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text or "custom_scraper"


def _json(data):
    return json.dumps(data or {}, indent=2, sort_keys=True)


def _extract_json_object(text):
    if hasattr(llm_handler, "_extract_json"):
        data = llm_handler._extract_json(text)  # pylint: disable=protected-access
        if isinstance(data, dict):
            return data
    raw = str(text or "")
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The local LLM did not return a JSON object.")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(
            "The local LLM returned malformed or truncated JSON for the scraper plugin. "
            "Try again, or simplify the scraper's scope."
        ) from exc


RECON_TIMEOUT = 20
RECON_MAX_BYTES = 2_000_000
RECON_USER_AGENT = "Mozilla/5.0 (compatible; JSE-ScraperBuilder/1.0)"
_JOB_LINK_HINTS = ("job", "career", "vacanc", "position", "requisition", "/jobs/", "jobid", "job-id", "jr-")


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
        links, class_hints = _candidate_job_links(soup, findings.get("final_url") or fetch_url)
        findings["candidate_links"] = links
        findings["container_class_hints"] = class_hints
    return findings


def _recon_section(recon):
    if not recon or not recon.get("fetched"):
        reason = (recon or {}).get("error") or "no URL supplied"
        return (f"SITE RECONNAISSANCE: unavailable ({reason}). "
                "Generate defensively, try multiple selector strategies, and add clear dry_run warnings.")
    lines = [f"SITE RECONNAISSANCE for {recon.get('final_url') or recon.get('url')} (use this real evidence, not assumptions):"]
    if recon.get("page_title"):
        lines.append(f"- Page title: {recon['page_title']}")
    lines.append(f"- Render type: {recon.get('render_hint')}")
    jsonld = recon.get("jsonld") or {}
    if jsonld.get("job_posting_found"):
        lines.append("- JSON-LD JobPosting FOUND -> STRONGLY PREFER parsing <script type=\"application/ld+json\"> "
                     f"JobPosting objects. Available fields: {', '.join(jsonld.get('sample_fields', []))}")
        if jsonld.get("example"):
            lines.append(f"  Example JobPosting values: {jsonld['example']}")
    else:
        lines.append("- No JSON-LD JobPosting detected in static HTML.")
    if recon.get("embedded_state"):
        lines.append(f"- Embedded client-state detected: {', '.join(recon['embedded_state'])}. "
                     "The job data is likely inside this JSON blob, not the rendered HTML — parse it directly.")
    links = recon.get("candidate_links") or []
    if links:
        lines.append(f"- {len(links)} candidate job links observed in static HTML (sample):")
        for link in links[:10]:
            lines.append(f"    text={link['text']!r} href={link['href']} container_class={link['container']!r}")
    else:
        lines.append("- No obvious job links found in static HTML (the site may need a search query in the URL or be JS-rendered).")
    if recon.get("container_class_hints"):
        lines.append(f"- Most common job-card container classes: {', '.join(recon['container_class_hints'])}")
    return "\n".join(lines)


def _recon_public_summary(recon):
    recon = recon or {}
    return {
        "fetched": recon.get("fetched", False),
        "url": recon.get("final_url") or recon.get("url"),
        "render_hint": recon.get("render_hint"),
        "jsonld_jobposting": bool((recon.get("jsonld") or {}).get("job_posting_found")),
        "embedded_state": recon.get("embedded_state") or [],
        "candidate_links": len(recon.get("candidate_links") or []),
        "container_class_hints": recon.get("container_class_hints") or [],
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


def _builder_prompt(answers, recon=None, feedback=None):
    plugin_id = _slug(answers.get("plugin_id") or answers.get("source_name") or answers.get("name"))
    source_name = answers.get("source_name") or answers.get("name") or plugin_id.replace("_", " ").title()
    mode = answers.get("mode") if answers.get("mode") in {"keyword", "sweep"} else "keyword"
    default_keyword = answers.get("test_keyword") or "business analyst"
    max_pages = int(answers.get("max_pages") or 3)
    return [
        {
            "role": "system",
            "content": (
                "You write safe JSE scraper plugins. Return only JSON. Do not use markdown. "
                "The JSON must contain manifest, scraper_code, readme, notes, and test_plan. "
                "The scraper must be conservative, cancellable, and must not perform filesystem writes, subprocess calls, "
                "shell calls, credential handling, or browser automation unless explicitly requested."
            ),
        },
        {
            "role": "user",
            "content": f"""
Build a JSE scraper plugin from these answers:
{_json(answers)}

Plugin contract:
- Folder contains scraper-plugin.json and scraper.py.
- manifest.id must be "{plugin_id}".
- manifest.name/source_name should be "{source_name}".
- manifest.module must be "scraper.py".
- manifest.callable must be "scrape".
- manifest.mode must be "{mode}".
- config_schema should include base_url, company_name, location, max_pages, and test_keyword when useful.
- scraper.py must define:
  def scrape(keyword, status_callback=None, log_callback=None, profile_id=1, base_url="", company_name="", location="", max_pages={max_pages}, dry_run=False, **config):
- It must import database_manager as db and store jobs with db.add_job(job_data, source_name, profile_id=profile_id, log_callback=log_callback) unless dry_run is True.
- In dry_run mode it must fetch/parse at most one or two pages and return a dict with ok, found, sample_jobs, warnings.
- In normal mode it should return True when at least one job was stored, otherwise False.
- It must check concurrency.cancel_event and concurrency.paused around page loops.
- Use requests for HTTP. For HTML parsing you may use BeautifulSoup (bs4) or the standard-library html.parser. Avoid brittle sleeps.
- Prefer structured data over brittle HTML selectors. If the reconnaissance below shows JSON-LD JobPosting, parse <script type="application/ld+json"> and read fields from it. If the page exposes embedded JSON state (e.g. __NEXT_DATA__) or a JSON API, target that instead of scraping rendered HTML.
- Base selectors and parsing on the reconnaissance evidence below, not on assumptions. If reconnaissance shows the page is client-side rendered with no static job links, say so plainly in dry_run warnings and attempt the embedded JSON / API path.
- Make selectors and parsing resilient (try a couple of fallbacks). If structure is uncertain, include clear warnings in dry_run.
- Never include personal information.

Testing assumptions:
- default keyword: "{default_keyword}"
- page limit: {max_pages}

{_recon_section(recon)}{_feedback_section(feedback)}

Return valid JSON exactly in this shape:
{{
  "manifest": {{...}},
  "scraper_code": "complete Python source",
  "readme": "short markdown instructions",
  "notes": ["..."],
  "test_plan": ["..."]
}}
""".strip(),
        },
    ]


def _normalise_generation(data, answers):
    plugin_id = _slug(answers.get("plugin_id") or data.get("manifest", {}).get("id") or answers.get("source_name"))
    manifest = dict(data.get("manifest") or {})
    manifest.update({
        "id": plugin_id,
        "module": "scraper.py",
        "callable": manifest.get("callable") or "scrape",
        "mode": manifest.get("mode") if manifest.get("mode") in {"keyword", "sweep"} else (answers.get("mode") or "keyword"),
    })
    manifest["name"] = manifest.get("name") or answers.get("source_name") or plugin_id.replace("_", " ").title()
    manifest["source_name"] = manifest.get("source_name") or manifest["name"]
    manifest["version"] = manifest.get("version") or "0.1.0"
    schema = manifest.get("config_schema") or []
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
    scrape_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "scrape"
        ),
        None,
    )
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


def _generate_once(answers, recon, feedback=None):
    """Single generation pass: LLM call (optionally with repair feedback),
    normalisation, and static validation. Reconnaissance is computed once by the
    caller and reused across repair attempts."""
    response = llm_handler._call_unsloth(  # pylint: disable=protected-access
        _builder_prompt(answers, recon, feedback),
        temperature=0.15,
        max_tokens=16000,
        json_mode=True,
    )
    data = _normalise_generation(_extract_json_object(response), answers)
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
    if plugin_id in RESERVED_PLUGIN_IDS:
        raise ValueError(
            f"'{plugin_id}' is a reserved built-in scraper id. "
            "Choose a different name for the generated scraper."
        )
    PLUGIN_ROOT.mkdir(parents=True, exist_ok=True)
    target = PLUGIN_ROOT / plugin_id
    if target.exists():
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
    if intended_id in RESERVED_PLUGIN_IDS:
        raise ValueError(
            f"'{intended_id}' is a reserved built-in scraper id. Choose a different name."
        )
    if (PLUGIN_ROOT / intended_id).exists():
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
            log(f"Generating scraper (attempt {attempt}/{max_attempts})...")
            try:
                generated = _generate_once(answers, recon, feedback)
            except Exception as exc:  # noqa: BLE001 - feed generation errors back in
                last_error = exc
                feedback = f"The previous output failed generation/validation: {type(exc).__name__}: {exc}"
                history.append({"attempt": attempt, "ok": False, "stage": "generate", "error": str(exc)})
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
        ok = False
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
