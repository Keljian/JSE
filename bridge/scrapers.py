"""Scraper plugin and search-run commands.

Split out of python_bridge.py, which re-exports everything here.
"""
import json

import database_manager as db
import scraper_plugins
from .runtime import (
    ProgressReporter,
    emit,
    import_app_logic,
)
from .documents import (
    read_resume_text,
)

def command_sources_list(payload):
    scraper_plugins.ensure_registered()
    stored_sources = db.get_all_sources(None if payload.get("include_all_profiles") else payload.get("profile_id"))
    plugin_sources = scraper_plugins.source_names(profile_id=payload.get("profile_id"), include_disabled=False)
    return {"sources": list(dict.fromkeys(plugin_sources + stored_sources))}


def command_scrapers_list(payload):
    # Force a full disk re-scan: this backs the Searchers settings view and is
    # refetched after every plugin mutation, so it is the freshness point for
    # the once-per-session registration cache.
    scraper_plugins.ensure_registered(force=True)
    profile_id = payload.get("profile_id")
    return {"scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=profile_id)}


def command_scrapers_import(payload):
    path = payload.get("path")
    if not path:
        raise ValueError("Missing plugin path.")
    plugin = scraper_plugins.install_from_path(path)
    return {"plugin": plugin, "scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))}


def command_scrapers_remove(payload):
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    scraper_plugins.remove_plugin(plugin_id)
    return {"ok": True, "scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))}


def command_scrapers_update(payload):
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    updates = {}
    if "enabled" in payload:
        updates["enabled"] = 1 if payload.get("enabled") else 0
    if "config" in payload:
        updates["config_json"] = json.dumps(payload.get("config") or {}, separators=(",", ":"), sort_keys=True)
    plugin = db.update_scraper_plugin(plugin_id, updates)
    return {"plugin": plugin, "scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))}


def command_scrapers_lane_update(payload):
    profile_id = payload.get("profile_id") or payload.get("lane_id") or 1
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    db.update_lane_scraper_settings(
        profile_id,
        plugin_id,
        enabled=payload.get("enabled") if "enabled" in payload else None,
        config=payload.get("config") if "config" in payload else None,
    )
    return {"scrapers": scraper_plugins.all_plugins(include_disabled=True, profile_id=profile_id)}


def command_scrapers_build(payload):
    import scraper_plugin_builder

    answers = payload.get("answers") or payload
    result = scraper_plugin_builder.build_and_install(
        answers,
        log_callback=lambda message: emit("log", message=message),
    )
    profile_id = payload.get("profile_id")
    result["scrapers"] = scraper_plugins.all_plugins(include_disabled=True, profile_id=profile_id)
    return result


def command_scrapers_test(payload):
    import scraper_plugin_builder

    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    return scraper_plugin_builder.test_plugin(
        plugin_id,
        profile_id=payload.get("profile_id") or 1,
        keyword=payload.get("keyword"),
        max_pages=payload.get("max_pages") or 1,
    )


def command_scrapers_diagnose(payload):
    import scraper_plugin_builder
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    result = scraper_plugin_builder.diagnose_plugin(
        plugin_id,
        profile_id=payload.get("profile_id") or 1,
        keyword=payload.get("keyword"),
        max_pages=payload.get("max_pages") or 1,
    )
    result["scrapers"] = scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))
    return result


def command_scrapers_repair(payload):
    import scraper_plugin_builder
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    result = scraper_plugin_builder.repair_plugin(
        plugin_id,
        profile_id=payload.get("profile_id") or 1,
        keyword=payload.get("keyword"),
        max_pages=payload.get("max_pages") or 1,
        max_attempts=payload.get("max_attempts") or 3,
        log_callback=lambda message: emit("log", message=message),
    )
    result["scrapers"] = scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))
    return result


def command_scrapers_rollback(payload):
    import scraper_plugin_builder
    plugin_id = payload.get("id") or payload.get("plugin_id")
    if not plugin_id:
        raise ValueError("Missing scraper plugin id.")
    result = scraper_plugin_builder.rollback_plugin_repair(plugin_id)
    result["scrapers"] = scraper_plugins.all_plugins(include_disabled=True, profile_id=payload.get("profile_id"))
    return result


def command_scrape_run(payload):
    app_logic = import_app_logic()
    sources = payload.get("sources") or scraper_plugins.source_names(profile_id=payload.get("profile_id"), include_disabled=False)
    if not sources:
        raise ValueError("No scraper plugins are available. Import a plugin or create one in Settings > Searchers.")
    include_all = bool(payload.get("include_all_profiles"))
    profiles = [profile for profile in (db.get_all_profiles() if include_all else [db.get_profile_by_id(payload.get("profile_id", 1))]) if profile]
    if not profiles:
        raise ValueError("No active lane is available for search. Add or select a lane before running search.")
    run_id = db.record_scraper_run(payload.get("profile_id"), "all_profiles" if include_all else "profile", sources, "running")
    try:
        for index, profile in enumerate(profiles, start=1):
            profile_id = profile["id"]
            emit("status", message=f"Scraping profile: {profile['name']}")
            reporter = ProgressReporter("search", extra={
                "lane": profile["name"],
                "lane_index": index,
                "lane_count": len(profiles),
            })
            terms = db.get_profile_terms(profile_id)
            resume_text = read_resume_text(profile_id)
            if not terms:
                emit("log", message=f"No saved terms for {profile['name']}. Generating terms first.")
                reporter(0, None, phase="terms", detail="Generating search terms…")
                terms = app_logic.execute_keyword_generation(
                    payload.get("optimism", 3),
                    resume_text,
                    lambda message: emit("log", message=message),
                    profile_id,
                )
            app_logic.execute_scraping_and_analysis(
                terms,
                sources,
                resume_text,
                lambda message, _progress=False: emit("status", message=message),
                lambda message: emit("log", message=f"[{profile['name']}] {message}"),
                lambda updated_terms: emit("log", message=f"[{profile['name']}] Search terms now: {', '.join(updated_terms)}"),
                None,
                profile_id,
                db.get_lane_settings(profile_id),
                progress_callback=reporter,
            )
        db.dedupe_database(lambda message: emit("log", message=message))
        db.record_scraper_run(status="complete", summary="Scrape completed.", run_id=run_id)
    except Exception as exc:
        db.record_scraper_run(status="failed", summary=str(exc), run_id=run_id)
        raise
    return {"ok": True}


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "sources:list": command_sources_list,
    "scrapers:list": command_scrapers_list,
    "scrapers:import": command_scrapers_import,
    "scrapers:remove": command_scrapers_remove,
    "scrapers:update": command_scrapers_update,
    "scrapers:laneUpdate": command_scrapers_lane_update,
    "scrapers:build": command_scrapers_build,
    "scrapers:test": command_scrapers_test,
    "scrapers:diagnose": command_scrapers_diagnose,
    "scrapers:repair": command_scrapers_repair,
    "scrapers:rollback": command_scrapers_rollback,
    "scrape:run": command_scrape_run,
}
