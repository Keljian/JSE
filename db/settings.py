"""App settings, credentials, and per-lane preference storage.

Split out of database_manager.py, which re-exports everything here.
"""
import sqlite3
import json
import os
from pathlib import Path
from .connection import (
    APP_ROOT,
    DATA_DIR,
    LOCAL_LLM_SETTINGS_FILE,
    _execute_with_retry,
    _persistent_runtime_path,
    get_db_connection,
)
from .constants import (
    DEFAULT_APP_SETTINGS,
    DEFAULT_PROFILE_SETTINGS,
    GLOBAL_CREDENTIAL_FIELDS,
    RETIRED_CLAUDE_MODELS,
    RETIRED_GEMINI_MODELS,
    RETIRED_GEMINI_MODEL_PREFIXES,
    WORK_MODE_OPTIONS,
)
from .text import (
    _clean,
    _clean_block,
    _split_csv,
)

def sanitize_gemini_model(value):
    model = _clean(str(value or ""))
    lowered = model.lower()
    if not model or lowered in RETIRED_GEMINI_MODELS or lowered.startswith(RETIRED_GEMINI_MODEL_PREFIXES):
        return DEFAULT_PROFILE_SETTINGS["gemini_model"]
    return model


def sanitize_claude_model(value):
    model = _clean(str(value or ""))
    if not model or model.lower() in RETIRED_CLAUDE_MODELS:
        return DEFAULT_PROFILE_SETTINGS["claude_model"]
    return model


SECTOR_CODES = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _screening_values(merged):
    """Normalise the commute and pay settings on the way into the database.

    Kept lenient on purpose. A radius the user cannot express, or a currency
    they leave blank, must degrade to "screen nothing" rather than to a filter
    nobody asked for: a job silently set aside by a mistyped setting is the
    failure this whole feature is meant to avoid.
    """
    def number(key, default, low, high):
        try:
            value = int(float(merged.get(key)))
        except (TypeError, ValueError):
            value = int(default)
        return max(low, min(high, value))

    sectors = ",".join(
        code for code in SECTOR_CODES
        if code in {
            part.strip().upper()
            for part in str(merged.get("accepted_sectors") or "").replace(";", ",").split(",")
        }
    )
    unit = str(merged.get("distance_unit") or "km").strip().lower()
    home = _clean(merged.get("home_location"))
    preferred = number("preferred_commute_km", DEFAULT_PROFILE_SETTINGS["preferred_commute_km"], 1, 20000)
    maximum = number("max_commute_km", DEFAULT_PROFILE_SETTINGS["max_commute_km"], 1, 20000)
    return {
        # Blank means "same as the search location", resolved on read. Storing
        # the fallback here would freeze it, so a later change to the search
        # location would silently stop moving the commute anchor with it.
        "home_location": home,
        "search_radius_km": number("search_radius_km", DEFAULT_PROFILE_SETTINGS["search_radius_km"], 0, 20000),
        "preferred_commute_km": min(preferred, maximum),
        "max_commute_km": maximum,
        "accepted_sectors": sectors,
        "distance_unit": unit if unit in ("km", "mi") else "km",
        "commute_screening_enabled": 1 if merged.get("commute_screening_enabled", True) else 0,
        "salary_floor": number("salary_floor", 0, 0, 100000000),
        "salary_currency": _clean(merged.get("salary_currency")).upper()[:3],
    }


def _settings_from_profile(row):
    if not row:
        return dict(DEFAULT_PROFILE_SETTINGS)
    settings = dict(DEFAULT_PROFILE_SETTINGS)
    for key in ("preferred_location", "seek_location", "linkedin_location", "doc_ai_provider",
                "doc_ai_model", "openai_api_key", "openai_base_url", "claude_api_key",
                "claude_model", "gemini_api_key", "gemini_model", "local_model",
                "resume_template_path", "cover_letter_template_path", "lane_intent", "target_titles",
                "target_domains", "seniority", "must_have_terms", "avoid_terms", "document_strategy",
                "positioning_doctrine"):
        if key in row.keys():
            settings[key] = row[key] or settings[key]
    if "active" in row.keys():
        settings["active"] = 1 if row["active"] is None else int(row["active"])
    settings["work_modes"] = _split_csv(row["work_modes"]) or list(DEFAULT_PROFILE_SETTINGS["work_modes"])
    settings["max_pages"] = int(row["max_pages"] or DEFAULT_PROFILE_SETTINGS["max_pages"])
    settings["default_min_score"] = int(row["default_min_score"] or DEFAULT_PROFILE_SETTINGS["default_min_score"])
    settings["boost_terms"] = row["boost_terms"] or ""
    settings["penalty_terms"] = row["penalty_terms"] or ""
    for key in ("home_location", "accepted_sectors", "distance_unit",
                "geocode_provider", "salary_currency"):
        if key in row.keys() and row[key] is not None:
            settings[key] = row[key]
    for key in ("preferred_commute_km", "max_commute_km", "salary_floor", "search_radius_km"):
        if key in row.keys() and row[key] is not None:
            try:
                settings[key] = int(row[key])
            except (TypeError, ValueError):
                pass
    if "commute_screening_enabled" in row.keys() and row["commute_screening_enabled"] is not None:
        settings["commute_screening_enabled"] = bool(int(row["commute_screening_enabled"]))
    # An existing profile has preferred_location set but no home_location, and
    # the search location is the right default anchor rather than making
    # everyone re-enter where they live. The fallback deliberately lives in
    # screening.Screener rather than here: resolving it on read would send it
    # straight back through update_profile_settings on the next save and freeze
    # a copy, so a later change to the search location would stop moving the
    # commute anchor with it. The settings UI shows it as a placeholder.
    settings["gemini_model"] = sanitize_gemini_model(settings.get("gemini_model"))
    settings["claude_model"] = sanitize_claude_model(settings.get("claude_model"))
    return settings


def _get_global_credentials():
    """Returns account-level API keys from app_settings."""
    settings = get_app_settings()
    return {field: str(settings.get(field) or "").strip() for field in GLOBAL_CREDENTIAL_FIELDS}


GLOBAL_AI_SETTING_FIELDS = (
    "doc_ai_provider",
    "document_ai_provider",
    "research_ai_provider",
    "memory_ai_provider",
    "doc_ai_model",
    "openai_api_key",
    "openai_base_url",
    "claude_api_key",
    "claude_model",
    "gemini_api_key",
    "gemini_model",
    "local_base_url",
    "local_api_key",
    "local_model",
    "scoring_ai_provider",
    "scoring_model",
    "compat_base_url",
    "compat_api_key",
    "compat_model",
    "analysis_workers",
    "local_context_target",
    "local_context_autoload",
)


LOCAL_LLM_SETTING_FIELDS = ("local_base_url", "local_api_key", "local_model")


def _normalize_local_base_url(value):
    text = str(value or "").strip().rstrip("/")
    if text.lower() in {"http://localhost:8888/api", "http://127.0.0.1:8888/api"}:
        return f"{text[:-4]}/v1"
    return text


def _get_global_ai_settings():
    settings = get_app_settings()
    return {field: settings.get(field, DEFAULT_APP_SETTINGS.get(field, "")) for field in GLOBAL_AI_SETTING_FIELDS}


def _load_local_llm_settings():
    try:
        if not LOCAL_LLM_SETTINGS_FILE.exists():
            return {}
        data = json.loads(LOCAL_LLM_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: _normalize_local_base_url(data.get(key)) if key == "local_base_url" else str(data.get(key) or "").strip()
        for key in LOCAL_LLM_SETTING_FIELDS
        if key in data
    }


def _save_local_llm_settings(updates):
    current = {
        "local_base_url": DEFAULT_APP_SETTINGS.get("local_base_url", ""),
        "local_api_key": "",
        "local_model": "",
        **_load_local_llm_settings(),
    }
    for key in LOCAL_LLM_SETTING_FIELDS:
        if key not in (updates or {}):
            continue
        value = str(updates.get(key) or "").strip()
        if key == "local_base_url" and not value:
            value = DEFAULT_APP_SETTINGS.get("local_base_url", "")
        if key == "local_base_url":
            value = _normalize_local_base_url(value)
        current[key] = value
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_LLM_SETTINGS_FILE.write_text(
        json.dumps(current, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return current


def get_profile_settings(profile_id):
    """Returns search and filtering preferences for a profile."""
    from .lanes import get_profile_by_id
    # Imported here rather than at module scope: get_profile_settings needs a
    # module that imports this one back.
    settings = _settings_from_profile(get_profile_by_id(profile_id))
    # Overlay account-level AI settings so document generation is independent of
    # the active lane and credentials are not duplicated across profile rows.
    settings.update(_get_global_ai_settings())
    settings["gemini_model"] = sanitize_gemini_model(settings.get("gemini_model"))
    settings["claude_model"] = sanitize_claude_model(settings.get("claude_model"))
    return settings


def get_lane_settings(lane_id):
    return get_profile_settings(lane_id)


def update_profile_settings(profile_id, settings):
    """Updates profile-level search preferences."""
    ai_updates = {
        field: settings[field]
        for field in GLOBAL_AI_SETTING_FIELDS
        if isinstance(settings, dict) and field in settings
    }
    if ai_updates:
        update_app_settings(ai_updates)
    current = get_profile_settings(profile_id)
    merged = {**current, **(settings or {})}
    work_modes = [mode for mode in _split_csv(merged.get("work_modes")) if mode in WORK_MODE_OPTIONS]
    if not work_modes:
        work_modes = list(DEFAULT_PROFILE_SETTINGS["work_modes"])
    max_pages = max(1, min(100, int(merged.get("max_pages") or DEFAULT_PROFILE_SETTINGS["max_pages"])))
    default_min_score = max(0, min(100, int(merged.get("default_min_score") or 0)))
    screening = _screening_values(merged)
    with get_db_connection() as conn:
        _execute_with_retry(
            conn,
            """
            UPDATE profiles
            SET preferred_location = ?,
                seek_location = ?,
                linkedin_location = ?,
                work_modes = ?,
                max_pages = ?,
                default_min_score = ?,
                boost_terms = ?,
                penalty_terms = ?,
                doc_ai_provider = ?,
                doc_ai_model = ?,
                openai_api_key = ?,
                openai_base_url = ?,
                claude_api_key = ?,
                claude_model = ?,
                gemini_api_key = ?,
                gemini_model = ?,
                local_model = ?,
                resume_template_path = ?,
                cover_letter_template_path = ?,
                lane_intent = ?,
                target_titles = ?,
                target_domains = ?,
                seniority = ?,
                must_have_terms = ?,
                avoid_terms = ?,
                document_strategy = ?,
                positioning_doctrine = ?,
                home_location = ?,
                search_radius_km = ?,
                preferred_commute_km = ?,
                max_commute_km = ?,
                accepted_sectors = ?,
                distance_unit = ?,
                commute_screening_enabled = ?,
                salary_floor = ?,
                salary_currency = ?,
                active = ?
            WHERE id = ?
            """,
            (
                _clean(merged.get("preferred_location")) or DEFAULT_PROFILE_SETTINGS["preferred_location"],
                _clean(merged.get("seek_location")) or DEFAULT_PROFILE_SETTINGS["seek_location"],
                _clean(merged.get("linkedin_location")) or DEFAULT_PROFILE_SETTINGS["linkedin_location"],
                ",".join(work_modes),
                max_pages,
                default_min_score,
                _clean(merged.get("boost_terms")),
                _clean(merged.get("penalty_terms")),
                _clean(merged.get("doc_ai_provider")) or DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
                _clean(merged.get("doc_ai_model")),
                "",
                _clean(merged.get("openai_base_url")) or DEFAULT_PROFILE_SETTINGS["openai_base_url"],
                "",
                _clean(merged.get("claude_model")) or DEFAULT_PROFILE_SETTINGS["claude_model"],
                "",
                _clean(merged.get("gemini_model")) or DEFAULT_PROFILE_SETTINGS["gemini_model"],
                _clean(merged.get("local_model")),
                _clean(merged.get("resume_template_path")) or DEFAULT_PROFILE_SETTINGS["resume_template_path"],
                _clean(merged.get("cover_letter_template_path")) or DEFAULT_PROFILE_SETTINGS["cover_letter_template_path"],
                _clean(merged.get("lane_intent")),
                _clean(merged.get("target_titles")),
                _clean(merged.get("target_domains")),
                _clean(merged.get("seniority")),
                _clean(merged.get("must_have_terms")),
                _clean(merged.get("avoid_terms")),
                _clean(merged.get("document_strategy")),
                _clean_block(merged.get("positioning_doctrine")),
                screening["home_location"],
                screening["search_radius_km"],
                screening["preferred_commute_km"],
                screening["max_commute_km"],
                screening["accepted_sectors"],
                screening["distance_unit"],
                screening["commute_screening_enabled"],
                screening["salary_floor"],
                screening["salary_currency"],
                1 if merged.get("active", 1) else 0,
                profile_id,
            ),
            is_commit=True,
        )
    return get_profile_settings(profile_id)


def update_lane_settings(lane_id, settings):
    return update_profile_settings(lane_id, settings)


def _app_setting_defaults():
    runtime_root = Path(os.environ.get("JSE_RUNTIME_ROOT") or os.environ.get("JSE_APP_ROOT") or APP_ROOT)
    return {
        **DEFAULT_APP_SETTINGS,
        "settings_dir": str(DATA_DIR),
        "applications_dir": str(runtime_root / "applications"),
        "older_applications_dir": str(runtime_root / "older_applications"),
    }


def get_app_settings():
    settings = _app_setting_defaults()
    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
    except sqlite3.OperationalError:
        return settings
    for row in rows:
        try:
            settings[row["key"]] = json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            settings[row["key"]] = row["value_json"]
    for key in ("applications_dir", "older_applications_dir"):
        settings[key] = _persistent_runtime_path(key, settings.get(key))
    local_file_settings = _load_local_llm_settings()
    if local_file_settings:
        settings.update(local_file_settings)
    elif (
        settings.get("local_model")
        or settings.get("local_api_key")
        or settings.get("local_base_url") != DEFAULT_APP_SETTINGS.get("local_base_url")
    ):
        settings.update(_save_local_llm_settings({key: settings.get(key, "") for key in LOCAL_LLM_SETTING_FIELDS}))
    settings["claude_model"] = sanitize_claude_model(settings.get("claude_model"))
    return settings


def get_app_setting(key, default=None):
    return get_app_settings().get(key, default)


def update_app_settings(settings):
    allowed = {
        "applications_dir", "older_applications_dir",
        "onboarding_completed", "onboarding_version",
        *GLOBAL_AI_SETTING_FIELDS,
    }
    defaults = _app_setting_defaults()
    sanitized = {}
    local_llm_updates = {}
    for key, value in (settings or {}).items():
        if key not in allowed:
            continue
        if key == "onboarding_completed":
            sanitized[key] = bool(value)
            continue
        if key == "onboarding_version":
            try:
                sanitized[key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                sanitized[key] = 0
            continue
        if key == "local_context_autoload":
            # A checkbox sends a boolean, and the blank-falls-back-to-default
            # rule below would turn False back into the default "1".
            sanitized[key] = "1" if value in (True, 1) or str(value).strip().lower() in {"1", "true", "yes", "on"} else "0"
            continue
        if key == "local_context_target":
            try:
                # 0 means "leave the window alone"; anything else is clamped to
                # a window a local runtime could plausibly serve.
                target = int(str(value or "0").strip())
            except (TypeError, ValueError):
                target = int(defaults.get(key, "0") or 0)
            sanitized[key] = str(0 if target <= 0 else min(max(target, 2048), 1048576))
            continue
        text = str(value or "").strip()
        if not text:
            text = defaults.get(key, "")
        if key == "local_base_url":
            text = _normalize_local_base_url(text)
        if key in LOCAL_LLM_SETTING_FIELDS:
            local_llm_updates[key] = text
        elif key in {"applications_dir", "older_applications_dir"}:
            try:
                path = Path(text).expanduser()
                if not path.is_absolute():
                    runtime_root = Path(os.environ.get("JSE_RUNTIME_ROOT") or os.environ.get("JSE_APP_ROOT") or APP_ROOT)
                    path = (runtime_root / path).resolve()
                sanitized[key] = str(path)
            except Exception:
                sanitized[key] = text
        elif key == "gemini_model":
            sanitized[key] = sanitize_gemini_model(text)
        elif key == "claude_model":
            sanitized[key] = sanitize_claude_model(text)
        else:
            sanitized[key] = text
    if local_llm_updates:
        _save_local_llm_settings(local_llm_updates)
    with get_db_connection() as conn:
        for key, value in sanitized.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value_json, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value)),
            )
        conn.commit()
    return get_app_settings()


def migrate_profile_credentials_to_app_settings():
    """Move legacy per-lane API keys into app_settings and clear profile copies."""
    migrated = {}
    try:
        current_app = get_app_settings()
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT doc_ai_provider, doc_ai_model, openai_api_key, openai_base_url,
                       claude_api_key, claude_model, gemini_api_key, gemini_model, local_model
                FROM profiles
                """
            ).fetchall()
        for row in rows:
            for field in GLOBAL_AI_SETTING_FIELDS:
                value = str(row[field] or "").strip() if field in row.keys() else ""
                if value and not str(current_app.get(field) or "").strip() and field not in migrated:
                    migrated[field] = value
        if migrated:
            update_app_settings(migrated)
        with get_db_connection() as conn:
            conn.execute("UPDATE profiles SET openai_api_key = '', claude_api_key = '', gemini_api_key = ''")
            conn.commit()
    except sqlite3.OperationalError:
        return {"migrated_fields": [], "cleared_profile_credentials": False}
    return {
        "migrated_fields": sorted(migrated.keys()),
        "cleared_profile_credentials": True,
    }


def get_kv_setting(key, default=None):
    """Read an internal JSON blob from app_settings (bypasses the user-facing
    update_app_settings allow-list). Used for the funnel insights cache and
    conversion priors."""
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.OperationalError:
        return default
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return default


def set_kv_setting(key, value):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (key, json.dumps(value)),
        )
        conn.commit()
