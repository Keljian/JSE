"""Bridge plumbing: the stdout protocol, workspace paths, and shared row helpers.

Split out of python_bridge.py, which keeps only the entrypoint and the merged
dispatch table. Every other module in this package imports from here.
"""
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import database_manager as db

# parents[1], not parent: this module lives in bridge/, one level below the
# application root. Asserted in tests/test_bridge_package.py.
APP_ROOT = Path(os.environ.get("JSE_APP_ROOT") or Path(__file__).resolve().parents[1])

# The sys.path bootstrap (app root + user site-packages) stays in
# python_bridge.py: it has to run before this package is imported at all.


# Protocol output. In one-shot mode emit() writes JSON lines to stdout exactly as
# before. In --serve (persistent worker) mode, _OUTPUT_STREAM is pinned to the real
# stdout while sys.stdout is redirected to stderr, so stray prints can never corrupt
# the framing, and every line carries the originating request id (thread-local).
_emit_lock = threading.Lock()


_request_ctx = threading.local()


_OUTPUT_STREAM = None


def emit(event_type, **payload):
    message = {"type": event_type, **payload}
    request_id = getattr(_request_ctx, "id", None)
    if request_id is not None:
        message["id"] = request_id
    stream = _OUTPUT_STREAM if _OUTPUT_STREAM is not None else sys.stdout
    line = json.dumps(message, default=str)
    with _emit_lock:
        stream.write(line + "\n")
        stream.flush()


class ProgressReporter:
    """Rate-limited `progress` frame emitter for long task loops.

    Analysis and search call their progress callbacks once per unit of work,
    which on a large board is hundreds of calls a minute. Every frame is a
    JSON line over the task pipe plus a React state update in the renderer, so
    intermediate frames are coalesced to `min_interval`. The first frame, any
    change of `phase`, and the final frame (current == total) always go out:
    those are the ones that change what the UI is showing rather than just
    nudging a number.
    """

    def __init__(self, kind, min_interval=0.4, extra=None):
        self.kind = kind
        self.min_interval = min_interval
        self.extra = dict(extra or {})
        self._last_sent = 0.0
        self._last_phase = None
        self._lock = threading.Lock()

    def __call__(self, current, total, phase=None, detail=None, failed=0, **fields):
        now = time.monotonic()
        with self._lock:
            final = total is not None and current is not None and current >= total
            forced = phase != self._last_phase or self._last_sent == 0.0 or final
            if not forced and now - self._last_sent < self.min_interval:
                return
            self._last_sent = now
            self._last_phase = phase
        payload = dict(self.extra)
        payload.update(fields)
        emit(
            "progress",
            kind=self.kind,
            current=current,
            total=total,
            phase=phase,
            detail=detail,
            failed=failed,
            **payload,
        )


def use_protocol_stream(stream):
    """Pin protocol output to `stream` (the real stdout) for worker mode.

    Set through a function rather than by assigning the module global from
    python_bridge: that would bind the name on *that* module while emit() kept
    reading this one's, so every protocol frame would go to the redirected
    stdout — i.e. stderr — and the worker would hang waiting for replies that
    never arrive.
    """
    global _OUTPUT_STREAM
    _OUTPUT_STREAM = stream


def set_request_id(request_id):
    """Tag emitted frames with the request they belong to (thread-local)."""
    _request_ctx.id = request_id


def bridge_error_message(exc):
    message = str(exc)
    if isinstance(exc, sqlite3.OperationalError) and "readonly database" in message.lower():
        return (
            f"{message} (SQLite database: {db.DB_FILE}; data directory: {db.DATA_DIR}). "
            "The bridge worker will be restarted; retry the action after the app refreshes."
        )
    return message


def row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _configured_folder(setting_key, default_name):
    value = db.get_app_setting(setting_key)
    path = Path(value) if value else APP_ROOT / default_name
    if not path.is_absolute():
        path = APP_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def applications_dir():
    return _configured_folder("applications_dir", "applications")


def older_applications_dir():
    return _configured_folder("older_applications_dir", "older_applications")


def shortlists_dir():
    """Where triage packets land. Configurable so it can be a watched folder."""
    return _configured_folder("shortlists_dir", "shortlists")


def rows_to_dicts(rows):
    return [row_to_dict(row) for row in rows]


JOB_SUMMARY_FIELDS = {
    "id",
    "profile_id",
    "profile_name",
    "title",
    "company",
    "location",
    "source",
    "url",
    "pipeline_stage",
    "status",
    "priority",
    "match_score",
    "composite_score",
    "fragment_score",
    "closing_date",
    "closing_date_source",
    "salary",
    "application_date",
    "application_url",
    "contact_person",
    "contact_email",
    "contact_phone",
    "interview_date",
    "interview_type",
    "interview_people",
    "feedback",
    "notes",
    "next_action",
    "next_action_date",
    "retired_reason",
    "last_interaction_at",
    "date_scraped",
    "updated_at",
    "has_company_research",
    "employer_type",
    "actual_company",
    "advertiser_company",
    "company_confidence",
    "job_flags_types",
    "job_flags_json",
    "channel",
}


def compact_job_dict(row, extra_fields=()):
    data = row_to_dict(row) if row is not None else {}
    allowed = JOB_SUMMARY_FIELDS | set(extra_fields or ())
    return {key: data.get(key) for key in allowed if key in data}


def compact_job_dicts(rows, extra_fields=()):
    return [compact_job_dict(row, extra_fields) for row in rows]


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_json_payload():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def import_app_logic():
    # llm_handler prints configuration during import. Keep the bridge protocol clean.
    with contextlib.redirect_stdout(sys.stderr):
        import app_logic
    return app_logic


def safe_filename(value):
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value or "application").strip()
    return re.sub(r"\s+", "_", cleaned)[:90] or "application"


def copy_into_workspace(source_path, target_dir, prefix=""):
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"File not found: {source_path}")

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_source = source.resolve()
    resolved_target_dir = target_dir.resolve()

    try:
        if resolved_source.parent == resolved_target_dir:
            return str(resolved_source)
    except OSError:
        pass

    stem = safe_filename(f"{prefix}_{source.stem}" if prefix else source.stem)
    suffix = source.suffix.lower()
    candidate = resolved_target_dir / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = resolved_target_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    shutil.copy2(str(resolved_source), str(candidate))
    return str(candidate)


def _json_loads_maybe(value, default=None):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _resolve_existing_path(value):
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.exists() else None


def datetime_timestamp_days_ago(days):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days)).timestamp()


def datetime_from_timestamp(timestamp):
    from datetime import datetime
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _tokenize_for_match(text):
    stop = {
        "and", "the", "for", "with", "that", "this", "from", "role", "job", "application",
        "candidate", "company", "manager", "senior", "lead", "will", "you", "your", "our",
    }
    return {
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9+/#.-]{2,}", str(text or ""))
        if word.lower() not in stop
    }


_startup_maintenance_lock = threading.Lock()


_startup_maintenance_started = False


def _run_startup_maintenance():
    """Idempotent database housekeeping that used to run inline in app:init.

    It blocked the first UI paint for seconds on a large database, so it now
    runs once per worker session on a background thread. WAL keeps the UI's
    concurrent reads safe; anything a sweep changes (dedupe, auto-reject,
    retirement) is picked up by the next app:refresh. Emits from this thread
    carry no request id and are dropped by the invoke path — same visibility
    as before, where these logs had no consumer either.
    """
    log = lambda message: emit("log", message=message)
    steps = (
        ("composite score recalculation", db.recalculate_composite_scores),
        ("dedupe", lambda: db.dedupe_database(log)),
        ("company intelligence backfill", db.backfill_missing_company_intelligence),
        ("closing-date refresh", lambda: db.refresh_closing_date_metadata(log_callback=log)),
        ("low-match auto-reject", lambda: db.reject_low_match_jobs(50, log_callback=log)),
        ("expired pipeline retirement", lambda: db.retire_expired_pipeline_jobs(log)),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:
            log(f"Startup maintenance step '{name}' failed: {bridge_error_message(exc)}")


# job_id -> (fingerprint, text_signals) for ad_signals.derive. Lives for the
# whole persistent-worker session: the regex passes over every ad description
# dominated jobs-list assembly (~1.5s per refresh on a ~5000-job board), and
# only jobs whose text actually changed need re-scanning.
_ad_signals_cache = {}


# The dashboard is refreshed constantly (every filter change debounces into an
# app:refresh), but the retire-expired sweep scans and writes the jobs table.
# In the persistent worker, run it at most once per interval per scope.
_HOUSEKEEPING_INTERVAL_SECONDS = 600


_housekeeping_last_run = {}


_housekeeping_lock = threading.Lock()


def _housekeeping_due(scope_key):
    now = time.monotonic()
    with _housekeeping_lock:
        last = _housekeeping_last_run.get(scope_key, 0)
        if last and now - last < _HOUSEKEEPING_INTERVAL_SECONDS:
            return False
        _housekeeping_last_run[scope_key] = now
        return True


def resolve_workspace_path(value):
    path = Path(value or "")
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


class JobNotLiveError(ValueError):
    """A confident liveness check says document generation should be skipped."""
