"""Discovery, validation, installation, and execution helpers for scrapers."""
import importlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import database_manager as db


APP_ROOT = Path(os.environ.get("JSE_APP_ROOT") or Path(__file__).resolve().parent)
DATA_DIR = Path(os.environ.get("JSE_DATA_DIR") or Path.cwd())
LOCAL_PLUGIN_DIR = Path(os.environ.get("JSE_LOCAL_PLUGIN_DIR") or APP_ROOT / "scraper_plugins")
USER_PLUGIN_DIR = DATA_DIR / "scraper_plugins"


def _json(data):
    return json.dumps(data or {}, separators=(",", ":"), sort_keys=True)


def ensure_registered():
    discover_user_plugins()
    db.disable_removed_builtin_scraper_plugins([])
    migrate_legacy_lane_configs()


def discover_user_plugins():
    plugin_dirs = []
    for root in (LOCAL_PLUGIN_DIR, USER_PLUGIN_DIR):
        if root.exists():
            plugin_dirs.extend(root.glob("*/scraper-plugin.json"))
    for manifest_path in plugin_dirs:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            validate_manifest(manifest)
            plugin_id = str(manifest.get("id") or manifest_path.parent.name).strip()
            if not plugin_id:
                continue
            plugin = {
                "id": plugin_id,
                "name": manifest.get("name") or plugin_id,
                "source_name": manifest.get("source_name") or manifest.get("name") or plugin_id,
                "version": manifest.get("version") or "",
                "enabled": 1,
                "install_type": "user",
                "install_path": str(manifest_path.parent),
                "manifest_json": _json(manifest),
                "config_json": _json(manifest_defaults(manifest)),
            }
            db.upsert_scraper_plugin(plugin, preserve_existing=True)
        except Exception:
            continue


def migrate_legacy_lane_configs():
    plugins = {plugin["id"]: plugin for plugin in db.get_scraper_plugins(include_disabled=True)}
    profiles = db.get_all_profiles()
    for profile in profiles:
        lane_id = profile["id"]
        max_pages = profile["max_pages"] if "max_pages" in profile.keys() else None
        if "seek" in plugins and profile["seek_location"] and not db.get_lane_scraper_setting(lane_id, "seek"):
            config = {"location": profile["seek_location"]}
            if max_pages:
                config["max_pages"] = max_pages
            db.update_lane_scraper_settings(lane_id, "seek", config=config)
        if "linkedin" in plugins and profile["linkedin_location"] and not db.get_lane_scraper_setting(lane_id, "linkedin"):
            config = {"location": profile["linkedin_location"]}
            if max_pages:
                config["max_pages"] = max_pages
            db.update_lane_scraper_settings(lane_id, "linkedin", config=config)


def manifest_defaults(manifest):
    defaults = {}
    for item in manifest.get("config_schema") or []:
        if "key" in item and "default" in item:
            defaults[item["key"]] = item["default"]
    return defaults


def validate_manifest(manifest):
    required = ("id", "name", "source_name", "module")
    missing = [key for key in required if not str(manifest.get(key) or "").strip()]
    if missing:
        raise ValueError(f"Plugin manifest missing required field(s): {', '.join(missing)}.")
    mode = manifest.get("mode") or "keyword"
    if mode not in {"keyword", "sweep"}:
        raise ValueError("Plugin manifest mode must be 'keyword' or 'sweep'.")
    for item in manifest.get("config_schema") or []:
        if not item.get("key"):
            raise ValueError("Every config_schema item needs a key.")
    return True


def all_plugins(include_disabled=True, profile_id=None):
    ensure_registered()
    plugins = db.get_scraper_plugins(include_disabled=include_disabled, profile_id=profile_id)
    return [_hydrate_plugin(plugin) for plugin in plugins]


def enabled_plugins(profile_id=None):
    plugins = all_plugins(include_disabled=False, profile_id=profile_id)
    return [plugin for plugin in plugins if _plugin_available(plugin) and plugin.get("enabled") and plugin.get("lane_enabled", True)]


def get_plugin(identifier, profile_id=None, include_disabled=False):
    key = str(identifier or "").strip()
    if not key:
        return None
    for plugin in all_plugins(include_disabled=include_disabled, profile_id=profile_id):
        if not include_disabled and not _plugin_available(plugin):
            continue
        if not include_disabled and not plugin.get("lane_enabled", True):
            continue
        names = {plugin["id"], plugin["name"], plugin["source_name"]}
        names.update(plugin.get("aliases") or [])
        if key.casefold() in {str(item).casefold() for item in names if item}:
            return plugin
    return None


def plugin_mode(identifier, profile_id=None):
    plugin = get_plugin(identifier, profile_id=profile_id, include_disabled=True)
    return (plugin or {}).get("mode") or "keyword"


def source_names(profile_id=None, include_disabled=False):
    plugins = all_plugins(include_disabled=include_disabled, profile_id=profile_id)
    if not include_disabled:
        plugins = [plugin for plugin in plugins if _plugin_available(plugin) and plugin.get("enabled") and plugin.get("lane_enabled", True)]
    return [plugin["source_name"] for plugin in plugins]


def resolve_run_sources(sources, profile_id=None):
    selected = sources or source_names(profile_id=profile_id)
    resolved = []
    seen = set()
    for source in selected:
        plugin = get_plugin(source, profile_id=profile_id, include_disabled=False)
        if not plugin:
            continue
        if not plugin.get("lane_enabled", True):
            continue
        if plugin["id"] in seen:
            continue
        resolved.append(plugin)
        seen.add(plugin["id"])
    return resolved


def build_config(plugin, search_settings=None):
    search_settings = search_settings or {}
    manifest = plugin.get("manifest") or {}
    config = manifest_defaults(manifest)
    config.update(plugin.get("config") or {})
    config.update(plugin.get("lane_config") or {})
    for item in manifest.get("config_schema") or []:
        legacy_key = item.get("legacy_key")
        key = item.get("key")
        if legacy_key and key and search_settings.get(legacy_key) not in (None, ""):
            config[key] = search_settings.get(legacy_key)
    if "max_pages" in config:
        try:
            config["max_pages"] = int(config["max_pages"])
        except (TypeError, ValueError):
            config["max_pages"] = 30
    return config


def load_callable(plugin):
    if not plugin:
        raise ValueError("Unknown scraper plugin.")
    manifest = plugin.get("manifest") or {}
    module_name = manifest.get("module")
    callable_name = manifest.get("callable") or "scrape"
    install_path = plugin.get("install_path")
    if not module_name:
        raise ValueError(f"Scraper plugin {plugin.get('name') or plugin.get('id')} has no module.")

    if install_path and plugin.get("install_type") == "user":
        plugin_path = Path(install_path)
        module_path = plugin_path / module_name
        if module_path.suffix == ".py" and module_path.exists():
            unique_name = f"jse_user_scraper_{plugin['id']}"
            spec = importlib.util.spec_from_file_location(unique_name, module_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[unique_name] = module
            spec.loader.exec_module(module)
        else:
            if str(plugin_path) not in sys.path:
                sys.path.insert(0, str(plugin_path))
            module = importlib.import_module(module_name)
    else:
        module = importlib.import_module(module_name)
    func = getattr(module, callable_name, None)
    if not callable(func):
        raise ValueError(f"Scraper plugin {plugin.get('name') or plugin.get('id')} has no callable {callable_name}.")
    return func


def _hydrate_plugin(plugin):
    manifest = plugin.get("manifest") or {}
    plugin = dict(plugin)
    plugin["aliases"] = manifest.get("aliases") or []
    plugin["mode"] = manifest.get("mode") or "keyword"
    plugin["config_schema"] = manifest.get("config_schema") or []
    if not _plugin_available(plugin):
        plugin["enabled"] = False
        plugin["missing"] = True
    return plugin


def _plugin_available(plugin):
    if (plugin or {}).get("install_type") != "user":
        return True
    path = plugin.get("install_path")
    return bool(path and Path(path).exists())


def install_from_path(source_path):
    source = Path(source_path)
    if source.is_file():
        manifest_path = source
        source_dir = source.parent
    else:
        manifest_path = source / "scraper-plugin.json"
        source_dir = source
    if not manifest_path.exists():
        raise FileNotFoundError("No scraper-plugin.json manifest found.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    validate_manifest(manifest)
    plugin_id = str(manifest.get("id") or "").strip()
    if not plugin_id:
        raise ValueError("Plugin manifest needs an id.")
    LOCAL_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    target = LOCAL_PLUGIN_DIR / plugin_id
    if source_dir.resolve() != target.resolve():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_dir, target)
    manifest = json.loads((target / "scraper-plugin.json").read_text(encoding="utf-8-sig"))
    validate_manifest(manifest)
    plugin = {
        "id": plugin_id,
        "name": manifest.get("name") or plugin_id,
        "source_name": manifest.get("source_name") or manifest.get("name") or plugin_id,
        "version": manifest.get("version") or "",
        "enabled": 1,
        "install_type": "user",
        "install_path": str(target),
        "manifest_json": _json(manifest),
        "config_json": _json(manifest_defaults(manifest)),
    }
    db.upsert_scraper_plugin(plugin, preserve_existing=False)
    return _hydrate_plugin(db.get_scraper_plugin(plugin_id))


def remove_plugin(plugin_id):
    plugin = db.get_scraper_plugin(plugin_id)
    if not plugin:
        return False
    if plugin.get("install_type") == "bundled":
        db.update_scraper_plugin(plugin_id, {"enabled": 0})
        return True
    path = plugin.get("install_path")
    db.delete_scraper_plugin(plugin_id)
    if path:
        target = Path(path)
        if target.exists() and target.parent in {LOCAL_PLUGIN_DIR, USER_PLUGIN_DIR}:
            shutil.rmtree(target)
    return True
