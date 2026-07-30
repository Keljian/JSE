"""SQLite connection handling: paths, WAL, retry, and the keepalive pin.

Split out of database_manager.py, which re-exports everything here.
"""
import sqlite3
import time
import os
import threading
from pathlib import Path
from contextlib import contextmanager

# parents[1], not parent: this module lives in db/, one level below the
# application root. Getting this wrong silently points the app at a different
# database directory, so it is asserted in tests/test_db_package.py.
APP_ROOT = Path(__file__).resolve().parents[1]


DATA_DIR = Path(os.environ.get("JSE_DATA_DIR") or APP_ROOT / "settings")


DATA_DIR.mkdir(parents=True, exist_ok=True)


DB_FILE = str(DATA_DIR / "job_applications.db")


LOCAL_LLM_SETTINGS_FILE = DATA_DIR / "local_llm_settings.json"


# journal_mode=WAL is persisted in the database header, so it only needs to be
# confirmed once per process rather than on every connection open. The lock
# serializes that one-time confirmation: with the parallel refresh/analysis
# fan-out, multiple threads open connections at once, and an unguarded WAL
# switch racing sibling connections (or the separate scraper process's write
# lock) surfaced "attempt to write a readonly database" and cycled the worker.
_wal_enabled = False


_wal_lock = threading.Lock()


# One idle connection held open for the whole process lifetime. The parallel
# refresh/analysis fan-out opens and closes many short-lived connections in
# bursts; when a burst's connections all close at once the process's open-
# connection count can hit zero, which tears down and rebuilds the WAL index
# (the -shm file). On Windows that rebuild intermittently raced the separate
# scraper process's write lock and surfaced as "attempt to write a readonly
# database", cycling the bridge worker. Pinning one connection open keeps the
# WAL index alive for the session so the rebuild never happens mid-flight.
#
# Safe against the file-swapping paths: database:restore kills every bridge/
# task process before replacing the file, and compact_database VACUUMs in
# place. The connection is only ever held idle (never used for a query), so it
# holds no lock and does not block checkpoints or VACUUM.
_keepalive_conn = None


_keepalive_lock = threading.Lock()


def _ensure_keepalive_connection():
    global _keepalive_conn
    if _keepalive_conn is not None:
        return
    with _keepalive_lock:
        if _keepalive_conn is not None:
            return
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=30000")
            _keepalive_conn = conn
        except sqlite3.Error:
            _keepalive_conn = None


@contextmanager
def get_db_connection():
    """Context manager for SQLite connections tuned for a large local database.

    WAL is confirmed once per process (it persists in the DB header, and
    setup_database() sets it at startup, so every connection already operates
    in WAL from the header regardless). The other PRAGMAs are per-connection
    and applied on every open:
      - busy_timeout lets SQLite wait on locks instead of raising immediately
        (the concurrent scrape / refresh / analysis paths hit contention). It
        is set FIRST so every later statement — including the WAL confirmation
        — respects it instead of failing fast under a momentary lock.
      - synchronous=NORMAL is safe under WAL and is the biggest write speedup;
        only a power loss (not an app crash) risks the last commit.
      - temp_store=MEMORY keeps sorts/temp tables off disk.
      - cache_size (~64MB) and mmap_size (256MB) cut I/O against the ~200MB DB.
    """
    global _wal_enabled
    _ensure_keepalive_connection()
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if not _wal_enabled:
            with _wal_lock:
                if not _wal_enabled:
                    # Redundant when the header already declares WAL (the normal
                    # case). Kept as a safety net for a brand-new DB, but never
                    # allowed to escalate a lost lock race into a fatal error:
                    # attempt it once, then trust the header either way.
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                    except sqlite3.OperationalError:
                        pass
                    _wal_enabled = True
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("PRAGMA mmap_size=268435456")
        yield conn
    finally:
        conn.close()


def ensure_application_context_schema():
    """Add optional application-evidence columns for hot-reloaded workers.

    Normal startup runs db_setup, but development UI reloads and fresh long-task
    processes can briefly run newer code against a still-open older database.
    Keep this targeted migration cheap and idempotent so an optional blank field
    can never block saving or document generation.
    """
    required = {
        "jobs": "additional_candidate_context TEXT",
        "application_kits": "additional_candidate_context TEXT",
    }
    with get_db_connection() as conn:
        for table, definition in required.items():
            columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns or "additional_candidate_context" in columns:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            except sqlite3.OperationalError as exc:
                # Another bridge/task process may have won the same migration.
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()


def compact_database():
    """Checkpoint WAL and VACUUM the SQLite database, returning size stats."""
    db_path = Path(DB_FILE)

    def file_size(path):
        return path.stat().st_size if path.exists() else 0

    def sizes():
        wal_path = db_path.with_name(db_path.name + "-wal")
        shm_path = db_path.with_name(db_path.name + "-shm")
        main = file_size(db_path)
        wal = file_size(wal_path)
        shm = file_size(shm_path)
        return {"main_bytes": main, "wal_bytes": wal, "shm_bytes": shm, "total_bytes": main + wal + shm}

    before = sizes()
    with sqlite3.connect(DB_FILE, timeout=30) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
    after = sizes()
    reclaimed = before["total_bytes"] - after["total_bytes"]
    return {
        "before_bytes": before["total_bytes"],
        "after_bytes": after["total_bytes"],
        "before_main_bytes": before["main_bytes"],
        "after_main_bytes": after["main_bytes"],
        "before_wal_bytes": before["wal_bytes"],
        "after_wal_bytes": after["wal_bytes"],
        "before_shm_bytes": before["shm_bytes"],
        "after_shm_bytes": after["shm_bytes"],
        "reclaimed_bytes": max(0, reclaimed),
        "delta_bytes": after["total_bytes"] - before["total_bytes"],
    }


def _execute_with_retry(conn, query, params, is_commit=False):
    """Executes a query with a retry mechanism for locked databases."""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if is_commit:
                conn.commit()
            return cursor
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 0.1  # Incremental backoff
                    time.sleep(wait_time)
                else:
                    raise e # Re-raise the exception after the last attempt
            else:
                raise e # Re-raise other operational errors immediately


def _persistent_runtime_path(key, value):
    """Remap only JSE's old default install-tree folders, never custom paths."""
    legacy_root = os.environ.get("JSE_LEGACY_RUNTIME_ROOT")
    runtime_root = os.environ.get("JSE_RUNTIME_ROOT")
    if not legacy_root or not runtime_root or key not in {"applications_dir", "older_applications_dir"}:
        return value
    try:
        if Path(str(value)).resolve() == (Path(legacy_root) / key.removesuffix("_dir")).resolve():
            return str(Path(runtime_root) / key.removesuffix("_dir"))
    except (OSError, TypeError, ValueError):
        pass
    return value
