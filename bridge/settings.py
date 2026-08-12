"""App settings, AI credentials, and database maintenance commands.

Split out of python_bridge.py, which re-exports everything here.
"""
import contextlib
import sys
import threading
import time
from pathlib import Path

import database_manager as db
import concurrent.futures
import scraper_plugins
from db_setup import setup_database
from .runtime import (
    _run_startup_maintenance,
    _startup_maintenance_lock,
    _startup_maintenance_started,
    row_to_dict,
)
from .lanes import (
    command_lanes_fragments_list,
    command_profiles_list,
)
from .jobs import (
    command_jobs_list,
)
from .scrapers import (
    command_sources_list,
)
from .insights import (
    command_calendar_get,
    command_dashboard_get,
)
from .corpus import (
    command_memory_status,
)

# Smallest local context window JSE's own prompts fit in comfortably. Below
# this the connection test still passes — a one-line reply fits anywhere — while
# every real analysis comes back truncated.
LOCAL_CONTEXT_ADVISORY_MINIMUM = 32768


def command_app_init(_payload):
    global _startup_maintenance_started
    with contextlib.redirect_stdout(sys.stderr):
        setup_database()
        db.migrate_profile_credentials_to_app_settings()
        scraper_plugins.ensure_registered()
    app_settings = db.get_app_settings()
    with _startup_maintenance_lock:
        if not _startup_maintenance_started:
            _startup_maintenance_started = True
            threading.Thread(target=_run_startup_maintenance, name="startup-maintenance", daemon=True).start()
    profiles = [row_to_dict(row) for row in db.get_all_profiles()]
    has_existing_setup = any(str(profile.get("resume_path") or "").strip() for profile in profiles)
    active_profile_id = profiles[0]["id"] if profiles else 1
    search_sources = scraper_plugins.source_names(include_disabled=False)
    return {
        "profiles": profiles,
        "active_profile_id": active_profile_id,
        "sources": search_sources,
        "search_sources": search_sources,
        "app_settings": app_settings,
        "needs_onboarding": not bool(app_settings.get("onboarding_completed")) and not has_existing_setup,
    }


def command_app_refresh(payload):
    profile_id = payload.get("profile_id", 1)
    include_all_profiles = bool(payload.get("include_all_profiles"))
    fragment_limit = payload.get("fragment_limit") or 12
    scoped = {"profile_id": profile_id, "include_all_profiles": include_all_profiles}

    def _fragments():
        try:
            return command_lanes_fragments_list({"profile_id": profile_id, "limit": fragment_limit})["fragments"]
        except Exception:
            return []

    # The campaign plan/summary is intentionally NOT part of the refresh
    # payload: it regex-scores hundreds of jobs and is only relevant when the
    # Campaign view is open, which loads campaign:plan on demand.
    #
    # The sub-fetches are independent reads (SQLite WAL supports concurrent
    # readers; each database_manager call opens its own connection), so run
    # them in parallel — the sources/scraper-plugin scans and the jobs query
    # were previously serialized on top of each other.
    fetches = {
        "profiles": lambda: command_profiles_list(payload)["profiles"],
        "sources": lambda: command_sources_list(scoped)["sources"],
        "search_sources": lambda: scraper_plugins.source_names(profile_id=profile_id, include_disabled=False),
        "jobs": lambda: command_jobs_list({**payload, "compact": True})["jobs"],
        "dashboard": lambda: command_dashboard_get({**scoped, "compact": True}),
        "calendar": lambda: command_calendar_get(scoped)["items"],
        "memory": lambda: command_memory_status({"profile_id": profile_id}),
        "fragments": _fragments,
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fetches), thread_name_prefix="app-refresh") as executor:
        futures = {name: executor.submit(fn) for name, fn in fetches.items()}
        return {name: future.result() for name, future in futures.items()}


def command_settings_get(payload):
    profile_id = payload.get("lane_id") or payload.get("profile_id", 1)
    return {"settings": db.get_lane_settings(profile_id)}


def command_settings_update(payload):
    profile_id = payload.get("lane_id") or payload.get("profile_id", 1)
    return {"settings": db.update_lane_settings(profile_id, payload.get("settings", {}))}


def command_ai_list_models(payload):
    """List available model ids for a provider so the UI can offer a dropdown.
    Uses the settings currently in the UI (so a freshly typed key/URL works)."""
    provider = str(payload.get("provider") or "").strip().lower()
    supplied = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    settings = {**db.get_app_settings(), **supplied}
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler
    return {"provider": provider, "models": llm_handler.list_models_for_provider(provider, settings)}


def command_settings_global_get(_payload):
    return {"settings": db.get_app_settings()}


def command_settings_global_update(payload):
    settings = db.update_app_settings(payload.get("settings", {}))
    for key in ("applications_dir", "older_applications_dir"):
        value = settings.get(key)
        if value:
            Path(value).mkdir(parents=True, exist_ok=True)
    return {"settings": settings}


def command_ai_test_provider(payload):
    """Make a minimal real request with the provider settings currently in the UI."""
    provider = str(payload.get("provider") or "").strip().lower()
    if provider not in {"local", "chatgpt", "claude", "gemini", "compat"}:
        raise ValueError(f"Unsupported AI provider: {provider or '(blank)'}")

    supplied = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    settings = {**db.get_app_settings(), **supplied, "doc_ai_provider": provider}
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    started = time.monotonic()
    discovered_model = ""
    context_length = None
    if provider == "local":
        local = llm_handler._local_ai_settings(settings)
        try:
            model_data = llm_handler._get_json(
                f"{local['base_url']}/models",
                llm_handler._local_auth_headers(local),
                timeout=15,
            )
            model_rows = model_data.get("data") if isinstance(model_data, dict) else None
            model_ids = [
                str(row.get("id") or "").strip()
                for row in (model_rows or [])
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            ]
            if not model_ids:
                return {
                    "ok": False,
                    "reachable": True,
                    "provider": provider,
                    "label": "Local endpoint",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "message": (
                        "Endpoint reachable, but no model is loaded. Load a model in Unsloth Studio "
                        "(Inference > Load), then test again. The Model field is the API model ID, not a folder path."
                    ),
                }
            # The catalogue lists every model the server *could* load, so the
            # first entry is not necessarily the one answering requests.
            loaded = llm_handler._loaded_model_row(model_rows or [], local.get("model"))
            discovered_model = str((loaded or {}).get("id") or model_ids[0]).strip()
            if local.get("model") not in model_ids:
                settings["local_model"] = discovered_model
            context_length = llm_handler._local_context_length(local)
        except Exception:
            # Some OpenAI-compatible servers do not expose /models. In that
            # case, fall through to the chat-completions health check.
            pass
    # Reasoning-capable Gemini/local models may spend the first several hundred
    # tokens internally even for a one-line answer. A 64-token ceiling can yield
    # finishReason=MAX_TOKENS with no response Part, which looks like a broken
    # connection despite successful authentication.
    test_token_budget = 4096 if provider == "gemini" else (1024 if provider == "local" else 256)
    response, label = llm_handler._call_document_ai(
        settings,
        [
            {"role": "system", "content": "You are testing an AI connection. Follow the user's response format exactly."},
            {"role": "user", "content": "Reply with exactly: JSE provider test OK"},
        ],
        temperature=0,
        max_tokens=test_token_budget,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not str(response or "").strip():
        raise RuntimeError(f"{label} returned an empty response.")
    result = {
        "ok": True,
        "provider": provider,
        "label": label,
        "elapsed_ms": elapsed_ms,
        "model": discovered_model,
    }
    if context_length:
        # A connection that works for a one-line test still fails every real
        # analysis if the model was loaded with a small window, and the server
        # reports that as a truncated answer rather than an error. Say so here,
        # where the user is already looking at the endpoint.
        result["context_length"] = context_length
        try:
            wanted = int(str(settings.get("local_context_target") or LOCAL_CONTEXT_ADVISORY_MINIMUM).strip())
        except (TypeError, ValueError):
            wanted = LOCAL_CONTEXT_ADVISORY_MINIMUM
        wanted = wanted or LOCAL_CONTEXT_ADVISORY_MINIMUM
        if context_length < min(wanted, LOCAL_CONTEXT_ADVISORY_MINIMUM):
            result["warning"] = (
                f"The loaded model is serving only a {context_length}-token context window. JSE's "
                f"analysis and document prompts are larger than that, so answers will be cut short. "
                f"Use “Apply context window” to reload it at {wanted} tokens."
            )
    return result


def command_ai_local_context_set(payload):
    """Reload the local model so it serves a given context window.

    Unsloth Studio's own API takes the window as a load parameter, so the fix
    for a too-small window does not have to be a trip to another application.
    The reload is heavy — it replaces the running server and re-pages the
    weights — so it only ever happens because someone asked for it, either
    through this command or through the auto-reload setting.
    """
    with contextlib.redirect_stdout(sys.stderr):
        import llm_handler

    supplied = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    settings = {**db.get_app_settings(), **supplied}
    target = payload.get("context_target") or settings.get("local_context_target")
    before = llm_handler.local_status(llm_handler._local_ai_settings(settings))
    started = time.monotonic()
    status = llm_handler.set_local_context_window(target, settings=settings)
    served = status.get("context_length")
    asked = int(target or 0)
    return {
        "ok": True,
        "model": status.get("active_model") or status.get("model_identifier") or "",
        "context_length": served,
        "previous_context_length": before.get("context_length"),
        "native_context_length": status.get("native_context_length"),
        "requested": asked,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        # The fitter caps the request to what the hardware holds, so asked-for
        # and served can differ and only the second one is worth acting on.
        "capped": bool(served and asked and served < asked),
    }


def command_database_compact(_payload):
    return db.compact_database()


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "app:init": command_app_init,
    "app:refresh": command_app_refresh,
    "settings:get": command_settings_get,
    "settings:update": command_settings_update,
    "settings:globalGet": command_settings_global_get,
    "settings:globalUpdate": command_settings_global_update,
    "ai:testProvider": command_ai_test_provider,
    "ai:localContextSet": command_ai_local_context_set,
    "ai:listModels": command_ai_list_models,
    "database:compact": command_database_compact,
}
