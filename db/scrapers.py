"""Scraper plugin registry, health, repairs, and run records.

Split out of database_manager.py, which re-exports everything here.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .connection import (
    get_db_connection,
)
from .text import (
    _json_loads_maybe,
    normalize_source,
)

def _scraper_plugin_row(row, lane_row=None):
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["manifest"] = _json_loads_maybe(data.pop("manifest_json", None), {})
    data["config"] = _json_loads_maybe(data.pop("config_json", None), {})
    if lane_row:
        data["lane_enabled"] = bool(lane_row["enabled"])
        data["lane_config"] = _json_loads_maybe(lane_row["config_json"], {})
    else:
        data["lane_enabled"] = True
        data["lane_config"] = {}
    return data


# A single keyword with no vacancies is normal, so one empty run says nothing.
# A source that has run this many times in a row without returning a single job,
# and has not succeeded for this many days, has almost certainly stopped working
# (a changed API, a blocked request) even though nothing raised. HiringCafe sat
# at "healthy" through 70 empty runs and a month of silence before this existed.
STALE_MIN_EMPTY_RUNS = 10
STALE_AFTER_DAYS = 7


def _parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "").replace("T", " "))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _apply_staleness(data, now=None):
    """Mark a quietly dead source as `stale`, with the days since it last worked.

    Errors already move a source to degraded/broken. This covers the other way a
    scraper dies: every call succeeds and returns nothing.
    """
    now = now or datetime.now(timezone.utc)
    last_success = _parse_utc(data.get("last_success_at"))
    data["days_since_success"] = (now - last_success).days if last_success else None
    empty = int(data.get("consecutive_empty") or 0)
    quiet_for = (now - last_success) if last_success else None
    long_quiet = quiet_for is None or quiet_for >= timedelta(days=STALE_AFTER_DAYS)
    if data.get("status") in ("healthy", "unknown", "stale", None) and empty >= STALE_MIN_EMPTY_RUNS and long_quiet:
        data["status"] = "stale"
        if last_success:
            data["stale_reason"] = (
                f"No results in {data['days_since_success']} days "
                f"({empty} empty runs in a row)."
            )
        else:
            data["stale_reason"] = f"Has never returned a result ({empty} empty runs)."
    elif data.get("status") == "stale":
        data["status"] = "healthy"
    return data


def get_scraper_health(scraper_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM scraper_health WHERE scraper_id = ?",
            (scraper_id,),
        ).fetchone()
    if row:
        return _apply_staleness(dict(row))
    return {
        "scraper_id": scraper_id,
        "status": "unknown",
        "consecutive_errors": 0,
        "consecutive_empty": 0,
        "successful_runs": 0,
        "empty_runs": 0,
        "error_runs": 0,
        "days_since_success": None,
    }


def get_all_scraper_health():
    """Every source's health, staleness applied, most worrying first."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM scraper_health ORDER BY scraper_id").fetchall()
    order = {"broken": 0, "stale": 1, "degraded": 2, "unknown": 3, "healthy": 4}
    health = [_apply_staleness(dict(row)) for row in rows]
    return sorted(health, key=lambda h: (order.get(h.get("status"), 5), h["scraper_id"]))


def record_scraper_health(scraper_id, outcome, error=None):
    """Record a structural scraper outcome without treating a quiet search as an error."""
    outcome = outcome if outcome in {"success", "empty", "error"} else "error"
    current = get_scraper_health(scraper_id)
    errors = int(current.get("consecutive_errors") or 0)
    empty = int(current.get("consecutive_empty") or 0)
    if outcome == "success":
        errors, empty, status = 0, 0, "healthy"
    elif outcome == "empty":
        # A keyword can legitimately have no matching vacancies, so one empty
        # run neither improves nor degrades health. A long streak of them does,
        # via _apply_staleness on read.
        empty += 1
        status = current.get("status") or "unknown"
        if status == "stale":
            status = "healthy"  # stored as the structural state; stale is derived
    else:
        errors, empty = errors + 1, 0
        status = "broken" if errors >= 2 else "degraded"
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO scraper_health
                (scraper_id, status, consecutive_errors, consecutive_empty,
                 successful_runs, empty_runs, error_runs, last_outcome,
                 last_error, last_checked_at, last_success_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'),
                    CASE WHEN ? = 'success' THEN datetime('now') ELSE NULL END)
            ON CONFLICT(scraper_id) DO UPDATE SET
                status = excluded.status,
                consecutive_errors = excluded.consecutive_errors,
                consecutive_empty = excluded.consecutive_empty,
                successful_runs = scraper_health.successful_runs + CASE WHEN excluded.last_outcome = 'success' THEN 1 ELSE 0 END,
                empty_runs = scraper_health.empty_runs + CASE WHEN excluded.last_outcome = 'empty' THEN 1 ELSE 0 END,
                error_runs = scraper_health.error_runs + CASE WHEN excluded.last_outcome = 'error' THEN 1 ELSE 0 END,
                last_outcome = excluded.last_outcome,
                last_error = CASE
                    WHEN excluded.last_outcome = 'success' THEN NULL
                    WHEN excluded.last_outcome = 'error' THEN excluded.last_error
                    ELSE scraper_health.last_error
                END,
                last_checked_at = datetime('now'),
                last_success_at = CASE WHEN excluded.last_outcome = 'success' THEN datetime('now') ELSE scraper_health.last_success_at END
            """,
            (
                scraper_id, status, errors, empty,
                1 if outcome == "success" else 0,
                1 if outcome == "empty" else 0,
                1 if outcome == "error" else 0,
                outcome, str(error or "")[-4000:] or None, outcome,
            ),
        )
        conn.commit()
    return get_scraper_health(scraper_id)


def record_scraper_repair(scraper_id, status, backup_path=None, installed_path=None,
                          diagnosis=None, test=None, error=None):
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO scraper_repairs
                (scraper_id, status, backup_path, installed_path, diagnosis_json, test_json, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scraper_id, status, backup_path, installed_path,
                json.dumps(diagnosis or {}, default=str),
                json.dumps(test or {}, default=str),
                str(error or "")[-4000:] or None,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def get_latest_applied_scraper_repair(scraper_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM scraper_repairs
            WHERE scraper_id = ? AND status = 'applied' AND rolled_back_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (scraper_id,),
        ).fetchone()
    return dict(row) if row else None


def mark_scraper_repair_rolled_back(repair_id):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE scraper_repairs SET status = 'rolled_back', rolled_back_at = datetime('now') WHERE id = ?",
            (repair_id,),
        )
        conn.commit()


def ensure_builtin_scraper_plugins(plugins):
    with get_db_connection() as conn:
        for plugin in plugins:
            existing = conn.execute("SELECT id FROM scraper_plugins WHERE id = ?", (plugin["id"],)).fetchone()
            manifest = dict(plugin)
            config = {}
            for item in manifest.get("config_schema") or []:
                if "key" in item and "default" in item:
                    config[item["key"]] = item["default"]
            if existing:
                conn.execute(
                    """
                    UPDATE scraper_plugins
                    SET name = ?,
                        source_name = ?,
                        version = ?,
                        install_type = 'bundled',
                        install_path = NULL,
                        manifest_json = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        plugin.get("name") or plugin["id"],
                        plugin.get("source_name") or plugin.get("name") or plugin["id"],
                        plugin.get("version") or "",
                        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                        plugin["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO scraper_plugins
                        (id, name, source_name, version, enabled, install_type, install_path, manifest_json, config_json)
                    VALUES (?, ?, ?, ?, 1, 'bundled', NULL, ?, ?)
                    """,
                    (
                        plugin["id"],
                        plugin.get("name") or plugin["id"],
                        plugin.get("source_name") or plugin.get("name") or plugin["id"],
                        plugin.get("version") or "",
                        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
                        json.dumps(config, separators=(",", ":"), sort_keys=True),
                    ),
                )
        conn.commit()


def disable_removed_builtin_scraper_plugins(active_builtin_ids):
    active = set(active_builtin_ids or [])
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM scraper_plugins WHERE install_type = 'bundled'"
        ).fetchall()
        removed = [row["id"] for row in rows if row["id"] not in active]
        if removed:
            placeholders = ",".join("?" for _ in removed)
            conn.execute(
                f"""
                UPDATE scraper_plugins
                SET enabled = 0,
                    updated_at = datetime('now')
                WHERE install_type = 'bundled'
                  AND id IN ({placeholders})
                """,
                removed,
            )
            conn.commit()


def disable_missing_user_scraper_plugins(valid_install_paths):
    valid = {str(Path(path).resolve()).casefold() for path in (valid_install_paths or []) if path}
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, install_path FROM scraper_plugins WHERE install_type = 'user'"
        ).fetchall()
        missing = []
        for row in rows:
            path = row["install_path"] or ""
            try:
                resolved = str(Path(path).resolve()).casefold()
            except Exception:
                resolved = path.casefold()
            if resolved not in valid or not Path(path).exists():
                missing.append(row["id"])
        if missing:
            placeholders = ",".join("?" for _ in missing)
            conn.execute(
                f"""
                UPDATE scraper_plugins
                SET enabled = 0,
                    updated_at = datetime('now')
                WHERE install_type = 'user'
                  AND id IN ({placeholders})
                """,
                missing,
            )
            conn.commit()


def get_scraper_plugin(plugin_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM scraper_plugins WHERE id = ?", (plugin_id,)).fetchone()
    return _scraper_plugin_row(row) if row else None


def get_scraper_plugins(include_disabled=True, profile_id=None):
    with get_db_connection() as conn:
        query = "SELECT * FROM scraper_plugins"
        params = []
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY install_type, name"
        rows = conn.execute(query, params).fetchall()
        lane_rows = {}
        if profile_id:
            lane_rows = {
                row["scraper_id"]: row
                for row in conn.execute(
                    "SELECT * FROM lane_scraper_settings WHERE lane_id = ?",
                    (profile_id,),
                ).fetchall()
            }
    plugins = [_scraper_plugin_row(row, lane_rows.get(row["id"])) for row in rows]
    for plugin in plugins:
        plugin["health"] = get_scraper_health(plugin["id"])
        plugin["can_rollback"] = bool(get_latest_applied_scraper_repair(plugin["id"]))
    return plugins


def upsert_scraper_plugin(plugin, preserve_existing=True):
    existing = get_scraper_plugin(plugin["id"]) if preserve_existing else None
    enabled = int(existing["enabled"]) if existing and "enabled" in existing else int(plugin.get("enabled", 1))
    config_json = (
        json.dumps(existing.get("config") or {}, separators=(",", ":"), sort_keys=True)
        if existing
        else plugin.get("config_json") or "{}"
    )
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO scraper_plugins
                (id, name, source_name, version, enabled, install_type, install_path, manifest_json, config_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                source_name = excluded.source_name,
                version = excluded.version,
                enabled = excluded.enabled,
                install_type = excluded.install_type,
                install_path = excluded.install_path,
                manifest_json = excluded.manifest_json,
                config_json = excluded.config_json,
                updated_at = datetime('now')
            """,
            (
                plugin["id"],
                plugin["name"],
                plugin["source_name"],
                plugin.get("version") or "",
                enabled,
                plugin.get("install_type") or "user",
                plugin.get("install_path"),
                plugin["manifest_json"],
                config_json,
            ),
        )
        conn.commit()


def update_scraper_plugin(plugin_id, updates):
    allowed = {"enabled", "config_json", "name", "source_name", "version"}
    assignments = []
    params = []
    for key, value in (updates or {}).items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        params.append(value)
    if not assignments:
        return get_scraper_plugin(plugin_id)
    params.append(plugin_id)
    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE scraper_plugins SET {', '.join(assignments)}, updated_at = datetime('now') WHERE id = ?",
            params,
        )
        conn.commit()
    return get_scraper_plugin(plugin_id)


def delete_scraper_plugin(plugin_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM lane_scraper_settings WHERE scraper_id = ?", (plugin_id,))
        conn.execute("DELETE FROM scraper_plugins WHERE id = ?", (plugin_id,))
        conn.commit()


def update_lane_scraper_settings(lane_id, scraper_id, enabled=None, config=None):
    current = {}
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM lane_scraper_settings WHERE lane_id = ? AND scraper_id = ?",
            (lane_id, scraper_id),
        ).fetchone()
        if row:
            current = _json_loads_maybe(row["config_json"], {})
            if enabled is None:
                enabled = row["enabled"]
        if config is not None:
            current = {**current, **config}
        if enabled is None:
            enabled = 1
        conn.execute(
            """
            INSERT INTO lane_scraper_settings (lane_id, scraper_id, enabled, config_json, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(lane_id, scraper_id) DO UPDATE SET
                enabled = excluded.enabled,
                config_json = excluded.config_json,
                updated_at = datetime('now')
            """,
            (lane_id, scraper_id, 1 if enabled else 0, json.dumps(current, separators=(",", ":"), sort_keys=True)),
        )
        conn.commit()
    return True


def get_lane_scraper_setting(lane_id, scraper_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM lane_scraper_settings WHERE lane_id = ? AND scraper_id = ?",
            (lane_id, scraper_id),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["config"] = _json_loads_maybe(data.pop("config_json", None), {})
    return data


def record_scraper_run(profile_id=None, scope="profile", sources=None, status="running", summary=None, run_id=None):
    with get_db_connection() as conn:
        if run_id:
            conn.execute(
                "UPDATE scraper_runs SET finished_at = datetime('now'), status = ?, summary = ? WHERE id = ?",
                (status, summary, run_id),
            )
            conn.commit()
            return run_id
        cursor = conn.execute(
            "INSERT INTO scraper_runs (profile_id, scope, sources, status, summary, pid) VALUES (?, ?, ?, ?, ?, ?)",
            (profile_id, scope, ",".join(sources or []), status, summary, os.getpid()),
        )
        conn.commit()
        return cursor.lastrowid


# No scrape legitimately runs this long (the nightly loop kills one at 4 hours),
# so a row still `running` past it is dead whatever its PID says. This also
# covers PID reuse, and rows written before the pid column existed.
ORPHANED_RUN_MAX_HOURS = 8


def _process_alive(pid):
    """True when a process with this PID is still running. Never signals it.

    os.kill(pid, 0) is a liveness probe on POSIX but TerminateProcess on
    Windows, so Windows asks the kernel for the exit code instead.
    """
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reconcile_orphaned_scraper_runs(now=None, process_alive=_process_alive):
    """Close out scrape runs whose process died without reporting.

    A sleep, a crash or a killed window leaves the row at `running` forever,
    which makes the last-scrape line lie and once needed fixing by hand in SQL.
    Returns the ids marked `interrupted`.
    """
    now = now or datetime.now(timezone.utc)
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, started_at, pid FROM scraper_runs WHERE status = 'running'"
        ).fetchall()
        closed = []
        for row in rows:
            started = _parse_utc(row["started_at"])
            too_old = started is None or now - started > timedelta(hours=ORPHANED_RUN_MAX_HOURS)
            dead = row["pid"] is not None and not process_alive(row["pid"])
            if not (too_old or dead):
                continue
            reason = (
                "Interrupted: the scrape process exited without reporting a result."
                if dead else
                f"Interrupted: still marked running after {ORPHANED_RUN_MAX_HOURS} hours."
            )
            conn.execute(
                "UPDATE scraper_runs SET status = 'interrupted', summary = ?, "
                "finished_at = COALESCE(finished_at, datetime('now')) WHERE id = ? AND status = 'running'",
                (reason, row["id"]),
            )
            closed.append(row["id"])
        if closed:
            conn.commit()
    return closed


def get_all_sources(profile_id=None):
    """Returns a list of all unique scraper sources in the database."""
    query = "SELECT DISTINCT source FROM jobs"
    params = []
    if profile_id:
        query += " WHERE profile_id = ?"
        params.append(profile_id)
    query += " ORDER BY source"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return sorted({normalize_source(row[0]) for row in cursor.fetchall() if row[0]})
