"""Guided local-LLM builder and smoke tester for scraper plugins."""
import ast
import inspect
import json
import re
from pathlib import Path

import database_manager as db
import llm_handler
import scraper_plugins


APP_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(scraper_plugins.LOCAL_PLUGIN_DIR)
ALLOWED_IMPORT_ROOTS = {
    "concurrency",
    "database_manager",
    "datetime",
    "html",
    "json",
    "math",
    "re",
    "requests",
    "scraping_helpers",
    "time",
    "urllib",
}
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
    start = str(text or "").find("{")
    end = str(text or "").rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The local LLM did not return a JSON object.")
    return json.loads(str(text)[start:end + 1])


def _builder_prompt(answers):
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
- Use requests and standard library parsing. Avoid brittle sleeps.
- Make selectors and parsing resilient. If structure is uncertain, include clear warnings in dry_run.
- Never include personal information.

Testing assumptions:
- default keyword: "{default_keyword}"
- page limit: {max_pages}

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
    namespace = {}
    exec(compile(tree, "scraper.py", "exec"), namespace)  # noqa: S102 - validated plugin code contract.
    scrape = namespace.get("scrape")
    if not callable(scrape):
        raise ValueError("Generated scraper.py must define a callable scrape function.")
    signature = inspect.signature(scrape)
    for required in ("keyword", "profile_id"):
        if required not in signature.parameters:
            raise ValueError(f"Generated scrape function must accept {required}.")
    return True


def generate_plugin(answers):
    answers = dict(answers or {})
    response = llm_handler._call_unsloth(  # pylint: disable=protected-access
        _builder_prompt(answers),
        temperature=0.15,
        max_tokens=12000,
        json_mode=True,
    )
    data = _normalise_generation(_extract_json_object(response), answers)
    scraper_plugins.validate_manifest(data["manifest"])
    _validate_code(data["scraper_code"])
    return data


def save_generated_plugin(generated):
    manifest = generated["manifest"]
    plugin_id = _slug(manifest["id"])
    PLUGIN_ROOT.mkdir(parents=True, exist_ok=True)
    target = PLUGIN_ROOT / plugin_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "scraper-plugin.json").write_text(_json(manifest), encoding="utf-8")
    (target / "scraper.py").write_text(generated["scraper_code"].rstrip() + "\n", encoding="utf-8")
    if generated.get("readme"):
        (target / "README.md").write_text(generated["readme"].rstrip() + "\n", encoding="utf-8")
    plugin = scraper_plugins.install_from_path(target)
    return plugin, target


def build_and_install(answers):
    generated = generate_plugin(answers)
    plugin, target = save_generated_plugin(generated)
    return {
        "plugin": plugin,
        "plugin_dir": str(target),
        "manifest": generated["manifest"],
        "notes": generated.get("notes") or [],
        "test_plan": generated.get("test_plan") or [],
        "readme": generated.get("readme") or "",
    }


def test_plugin(plugin_id, profile_id=1, keyword=None, max_pages=1):
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

    result = func(
        keyword=test_keyword,
        status_callback=log,
        log_callback=log,
        profile_id=profile_id,
        **config,
    )
    ok = bool(result is True or (isinstance(result, dict) and result.get("ok") is not False))
    return {
        "ok": ok,
        "plugin": plugin,
        "keyword": test_keyword,
        "result": result,
        "logs": logs[-50:],
    }
