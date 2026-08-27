"""JSE MCP server - control the local Job Search Engine desktop app over MCP.

JSE (C:\\JSE) is an Electron + Python desktop application. All of its business
logic sits behind one JSON command bridge (`python_bridge.py`), which the
Electron renderer drives and which exposes ~117 commands grouped by prefix
(jobs:, analysis:, scrape:, docs:, campaign:, corpus:, settings: ...).

This server puts that same bridge behind MCP tools, so an agent can query the
job database, move pipeline stages, export and read triage packets, and kick off
scrapes and analysis runs without going through the UI.

Design notes
------------
* Short commands go to one persistent `python_bridge.py --serve` worker, which
  pays the import and SQLite warmup cost once. Frames are newline-delimited JSON
  tagged with the request id.
* Long, cancellable commands (scrape:run, analysis:run, docs:*) get their own
  one-shot process, matching how Electron runs them, and are exposed as
  background tasks with a pollable status.
* Triage packets are read straight off disk. A packet is 4-10 MB of JSON, far
  past any sane tool response, so the packet tools return an audit and paged
  rows rather than the file.
* Writes are limited to the safe set by default. Destructive commands are
  refused unless JSE_MCP_ALLOW_DESTRUCTIVE=1 is set in the server config.

Environment
-----------
JSE_ROOT                    application root (default C:\\JSE)
JSE_PYTHON                  interpreter (default <root>\\.venv\\Scripts\\python.exe)
JSE_SHORTLISTS_DIR          packet folder (default <root>\\shortlists)
JSE_MCP_ALLOW_DESTRUCTIVE   "1" to unlock delete/compact/clear commands
JSE_MCP_MAX_CHARS           response cap in characters (default 60000)
JSE_MCP_TIMEOUT             default per-call timeout in seconds (default 180)
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
except ModuleNotFoundError:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer

try:
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover - older SDKs
    ToolAnnotations = None

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_ROOT = Path(os.environ.get("JSE_ROOT") or r"C:\JSE")
PYTHON = Path(
    os.environ.get("JSE_PYTHON") or (APP_ROOT / ".venv" / "Scripts" / "python.exe")
)
SHORTLISTS_DIR = Path(os.environ.get("JSE_SHORTLISTS_DIR") or (APP_ROOT / "shortlists"))
BRIDGE = APP_ROOT / "python_bridge.py"
ALLOW_DESTRUCTIVE = os.environ.get("JSE_MCP_ALLOW_DESTRUCTIVE", "") == "1"
MAX_CHARS = int(os.environ.get("JSE_MCP_MAX_CHARS") or 60000)
DEFAULT_TIMEOUT = float(os.environ.get("JSE_MCP_TIMEOUT") or 180)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Commands that remove data or rewrite the database file. Callable only when
# JSE_MCP_ALLOW_DESTRUCTIVE=1, so an agent cannot delete rows from a 19k-job
# database on a misread instruction.
DESTRUCTIVE = {
    "jobs:delete",
    "jobs:cleanupArchive",
    "jobs:resetRejected",
    "jobs:clearFlags",
    "database:compact",
    "corpus:clearDocs",
    "corpus:clearFragments",
    "corpus:removeDoc",
    "corpus:reindex",
    "scrapers:remove",
    "scrapers:rollback",
    "lanes:delete",
    "profiles:delete",
    "warmContacts:delete",
    "hiddenMarket:leadDelete",
}

# Commands that run for minutes and are cancellable in the app. These get their
# own process instead of sharing the persistent worker.
LONG_RUNNING = {
    "scrape:run",
    "analysis:run",
    "analysis:job",
    "docs:generate",
    "docs:generateRich",
    "docs:generateInterestedBatch",
    "company:researchBatch",
    "corpus:mine",
    "corpus:reindex",
    "enrichment:process",
    "memory:scan",
    "memory:remineDue",
    "scrapers:build",
    "scrapers:repair",
    "campaign:plan",
}

PIPELINE_STAGES = [
    "new",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "rejected_by_company",
    "archived",
]

# Fields that carry kilobytes of ad text or JSON blobs. Stripped from responses
# unless a tool argument asks for them, because one unstripped job row can be
# larger than a whole page of results.
HEAVY_FIELDS = (
    "description",
    "pdf_text",
    "position_description_text",
    "ad_text",
    "company_intelligence",
    "resume_text",
    "cover_letter_text",
    "fragment_alignment_json",
    "job_intelligence_json",
    "contact_records_json",
    "blocker_json",
    "job_flags_json",
)

# Job columns db.update_job_application will actually write. Mirrored here so a
# rejected key is reported to the caller instead of silently dropped.
UPDATABLE_FIELDS = {
    "pipeline_stage", "status", "closing_date", "closing_date_source",
    "next_action", "next_action_date", "priority", "application_date",
    "application_url", "contact_person", "contact_email", "contact_phone",
    "resume_used", "resume_text", "cover_letter_path", "cover_letter_text",
    "position_description_path", "position_description_text",
    "additional_candidate_context", "interview_date", "interview_type",
    "interview_people", "feedback", "salary", "notes", "advertiser_company",
    "actual_company", "employer_type", "company_confidence",
    "company_intelligence", "company_research_updated_at", "retired_reason",
}

server = MCPServer("jse")


def log(message: str) -> None:
    """Diagnostics to stderr. stdout is the MCP protocol stream."""
    print(f"[jse-mcp] {message}", file=sys.stderr, flush=True)


class BridgeError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Persistent bridge worker
# --------------------------------------------------------------------------


class BridgeWorker:
    """One `python_bridge.py --serve` process, shared by all short commands.

    The worker speaks newline-delimited JSON: requests are {id, command,
    payload}; replies are frames {type, id, ...} where type is result, error,
    log, status or progress. Frames are routed back to the waiting caller by id
    from a single reader thread.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._counter = 0
        self.started_at: Optional[float] = None

    def _spawn(self) -> subprocess.Popen:
        if not BRIDGE.exists():
            raise BridgeError(
                f"JSE bridge not found at {BRIDGE}. Set JSE_ROOT to the folder "
                "containing python_bridge.py."
            )
        if not Path(PYTHON).exists():
            raise BridgeError(
                f"Python interpreter not found at {PYTHON}. Set JSE_PYTHON to "
                "the interpreter that runs JSE (usually <root>\\.venv\\Scripts\\python.exe)."
            )
        env = dict(os.environ)
        env["JSE_APP_ROOT"] = str(APP_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [str(PYTHON), str(BRIDGE), "--serve"],
            cwd=str(APP_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=NO_WINDOW,
        )
        self.started_at = time.time()
        threading.Thread(target=self._read_loop, args=(proc,), daemon=True).start()
        log(f"bridge worker started (pid {proc.pid})")
        return proc

    def _read_loop(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except Exception:
                continue  # stray output, not a protocol frame
            slot = self._pending.get(str(frame.get("id")))
            if slot is not None:
                slot.put(frame)
        # stdout closed: the worker died. Unblock everyone waiting on it.
        for slot in list(self._pending.values()):
            slot.put({"type": "error", "message": "bridge worker exited"})
        with self._lock:
            if self._proc is proc:
                self._proc = None

    def _ensure(self) -> subprocess.Popen:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._spawn()
            return self._proc

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def call(self, command: str, payload: dict, timeout: float) -> dict:
        """Run one command, returning {data, logs}. Raises BridgeError on failure."""
        proc = self._ensure()
        with self._lock:
            self._counter += 1
            request_id = f"mcp-{self._counter}"
        slot: queue.Queue = queue.Queue()
        self._pending[request_id] = slot
        try:
            assert proc.stdin is not None
            frame = json.dumps(
                {"id": request_id, "command": command, "payload": payload or {}}
            )
            try:
                proc.stdin.write(frame + "\n")
                proc.stdin.flush()
            except Exception as exc:
                raise BridgeError(f"could not reach the JSE bridge worker: {exc}")

            logs: list[str] = []
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise BridgeError(
                        f"'{command}' did not finish within {timeout:.0f}s. Long "
                        "commands (scrape:run, analysis:run, docs:*) should be "
                        "started with jse_start_task instead, which polls."
                    )
                try:
                    reply = slot.get(timeout=min(remaining, 5))
                except queue.Empty:
                    if not self.alive():
                        raise BridgeError("the JSE bridge worker exited unexpectedly")
                    continue
                kind = reply.get("type")
                if kind == "result":
                    return {"data": reply.get("data"), "logs": logs[-40:]}
                if kind == "error":
                    raise BridgeError(reply.get("message") or "unknown bridge error")
                if kind in ("log", "status"):
                    text = reply.get("message")
                    if text:
                        logs.append(str(text))
        finally:
            self._pending.pop(request_id, None)


WORKER = BridgeWorker()


# --------------------------------------------------------------------------
# Background (one-shot process) tasks
# --------------------------------------------------------------------------

TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()


def _start_task(command: str, payload: dict) -> str:
    task_id = f"{command.replace(':', '_')}-{uuid.uuid4().hex[:8]}"
    record = {
        "task_id": task_id,
        "command": command,
        "payload": payload,
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "logs": [],
        "progress": None,
        "result": None,
        "error": None,
    }
    with TASKS_LOCK:
        TASKS[task_id] = record

    def run() -> None:
        env = dict(os.environ)
        env["JSE_APP_ROOT"] = str(APP_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                [str(PYTHON), str(BRIDGE), command],
                cwd=str(APP_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=NO_WINDOW,
            )
        except Exception as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            record["finished_at"] = datetime.now().isoformat(timespec="seconds")
            return
        record["pid"] = proc.pid
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(json.dumps(payload or {}))
            proc.stdin.close()
        except Exception:
            pass
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except Exception:
                continue
            kind = frame.get("type")
            if kind in ("log", "status"):
                text = frame.get("message")
                if text:
                    record["logs"].append(str(text))
                    del record["logs"][:-200]  # keep the tail only
            elif kind == "progress":
                record["progress"] = {
                    "current": frame.get("current"),
                    "total": frame.get("total"),
                    "phase": frame.get("phase"),
                    "detail": frame.get("detail"),
                    "failed": frame.get("failed"),
                    "lane": frame.get("lane"),
                }
            elif kind == "result":
                record["result"] = frame.get("data")
                record["status"] = "completed"
            elif kind == "error":
                record["error"] = frame.get("message")
                record["status"] = "error"
        proc.wait()
        if record["status"] == "running":
            record["status"] = "completed" if proc.returncode == 0 else "error"
            if record["error"] is None and proc.returncode != 0:
                record["error"] = f"process exited with code {proc.returncode}"
        record["finished_at"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=run, daemon=True).start()
    return task_id


# --------------------------------------------------------------------------
# Response shaping
# --------------------------------------------------------------------------


def _strip_heavy(value: Any, keep: tuple[str, ...] = (), analysis_chars: int = 900) -> Any:
    """Drop multi-KB ad text and JSON blobs, and cap the analysis narrative."""
    if isinstance(value, list):
        return [_strip_heavy(item, keep, analysis_chars) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in HEAVY_FIELDS and key not in keep:
            if isinstance(item, str) and item:
                out[key + "_chars"] = len(item)
            continue
        if key in ("analysis", "ai_analysis") and isinstance(item, str):
            if analysis_chars <= 0 and key not in keep:
                continue
            out[key] = item if len(item) <= analysis_chars else item[:analysis_chars] + " ...[truncated]"
            continue
        out[key] = _strip_heavy(item, keep, analysis_chars)
    return out


def _respond(payload: Any) -> str:
    """Serialize, and refuse to blow the context window open silently."""
    text = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    if len(text) <= MAX_CHARS:
        return text
    # Stay parseable when over the cap: a half-serialized object is worse than
    # an explicit refusal, because the caller cannot tell what it lost.
    return json.dumps(
        {
            "error": f"response is {len(text)} characters, over the {MAX_CHARS} cap",
            "hint": "Narrow it: a smaller limit, more filters, or analysis_chars=0.",
            "preview": text[: max(0, MAX_CHARS - 2000)],
        },
        indent=2,
    )


def _error(message: str, hint: str = "") -> str:
    return _respond({"error": message, "hint": hint} if hint else {"error": message})


def _call(command: str, payload: dict | None = None, timeout: float | None = None) -> dict:
    if command in DESTRUCTIVE and not ALLOW_DESTRUCTIVE:
        raise BridgeError(
            f"'{command}' is destructive and this server is running in safe mode. "
            "Set JSE_MCP_ALLOW_DESTRUCTIVE=1 in the server's env in "
            "claude_desktop_config.json and restart Claude to enable it."
        )
    return WORKER.call(command, payload or {}, timeout or DEFAULT_TIMEOUT)


def _clean(payload: dict) -> dict:
    """Drop None values so JSE's own defaults apply instead of being overwritten."""
    return {k: v for k, v in payload.items() if v is not None}


def _tool(fn=None, **kwargs):
    """Register a tool, passing annotations only if this SDK version takes them."""
    import inspect

    def deco(func):
        try:
            params = inspect.signature(server.tool).parameters
        except (TypeError, ValueError):
            params = {}
        opts = dict(kwargs)
        hint = opts.get("annotations")
        if isinstance(hint, dict) and ToolAnnotations is not None:
            opts["annotations"] = ToolAnnotations(**hint)
        if "annotations" in opts and "annotations" not in params:
            opts.pop("annotations")
        return server.tool(**opts)(func)

    return deco(fn) if fn is not None else deco


RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
RW = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


# --------------------------------------------------------------------------
# Packet helpers
# --------------------------------------------------------------------------


def _packet_files() -> list[Path]:
    if not SHORTLISTS_DIR.exists():
        return []
    return sorted(
        SHORTLISTS_DIR.glob("shortlist_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _resolve_packet(path: Optional[str]) -> Path:
    if path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = SHORTLISTS_DIR / candidate
        if not candidate.exists():
            raise BridgeError(f"packet not found: {candidate}")
        return candidate
    files = _packet_files()
    if not files:
        raise BridgeError(
            f"no triage packets in {SHORTLISTS_DIR}. Run jse_export_packet to write one."
        )
    return files[0]


_PACKET_CACHE: dict[str, Any] = {}


def _load_packet(path: Path) -> dict:
    key = f"{path}:{path.stat().st_mtime_ns}"
    cached = _PACKET_CACHE.get(key)
    if cached is None:
        _PACKET_CACHE.clear()  # one packet in memory at a time; they are large
        with path.open(encoding="utf-8") as handle:
            cached = json.load(handle)
        _PACKET_CACHE[key] = cached
    return cached


_FIT_RE = re.compile(r"Fit Level:\s*([A-Za-z_ -]+)")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _fit_level(job: dict) -> Optional[str]:
    match = _FIT_RE.search(job.get("analysis") or "")
    return match.group(1).strip().lower() if match else None


def _norm(text: Any) -> str:
    return " ".join(_WORD_RE.findall(str(text or "").lower()))


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _packet_row(job: dict, analysis_chars: int = 0) -> dict:
    row = {
        "id": job.get("id"),
        "title": job.get("title"),
        "company": job.get("company"),
        "advertiser": job.get("advertiser"),
        "location": job.get("location"),
        "source": job.get("source"),
        "stage": job.get("pipeline_stage"),
        "match": job.get("match_score"),
        "fragment": job.get("fragment_score"),
        "composite": job.get("composite_score"),
        "fit": _fit_level(job),
        "closing_date": job.get("closing_date"),
        "salary": job.get("salary"),
        "salary_min": job.get("salary_min"),
        "commute_km": job.get("commute_km"),
        "commute_verdict": job.get("commute_verdict"),
        "warmth": job.get("warmth"),
        "warm_path": len(job.get("warm_path") or []),
        "flags": [f.get("type") for f in (job.get("flags") or []) if isinstance(f, dict)],
        "flag_summary": job.get("flag_summary"),
        "seniority": job.get("seniority_direction"),
        "url": job.get("url"),
    }
    if analysis_chars > 0:
        text = job.get("analysis") or ""
        row["analysis"] = text[:analysis_chars] + (" ...[truncated]" if len(text) > analysis_chars else "")
    return row


# --------------------------------------------------------------------------
# Tools: health and discovery
# --------------------------------------------------------------------------


@_tool(name="jse_health", annotations=RO)
def jse_health() -> str:
    """Check that JSE is reachable and summarise the state of its database.

    Returns the resolved paths, database size and age, pipeline stage counts,
    the most recent triage packets, and whether destructive commands are
    unlocked. Call this first when something is not behaving as expected.
    """
    db_path = APP_ROOT / "settings" / "job_applications.db"
    info: dict[str, Any] = {
        "app_root": str(APP_ROOT),
        "python": str(PYTHON),
        "bridge": str(BRIDGE),
        "bridge_exists": BRIDGE.exists(),
        "destructive_commands_unlocked": ALLOW_DESTRUCTIVE,
        "shortlists_dir": str(SHORTLISTS_DIR),
    }
    if db_path.exists():
        stat = db_path.stat()
        info["database"] = {
            "path": str(db_path),
            "size_mb": round(stat.st_size / 1048576, 1),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }
    packets = _packet_files()[:5]
    info["recent_packets"] = [
        {
            "file": p.name,
            "size_mb": round(p.stat().st_size / 1048576, 1),
            "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for p in packets
    ]
    try:
        dash = _call("dashboard:get", {"include_all_profiles": True, "compact": True}, 240)["data"]
        info["stage_counts"] = dash.get("stage_counts")
        info["last_scrape"] = dash.get("last_scrape")
        info["bridge_status"] = "ok"
    except Exception as exc:
        info["bridge_status"] = "unreachable"
        info["bridge_error"] = str(exc)
    # Read after the call, not before: the worker starts lazily on first use.
    info["worker_alive"] = WORKER.alive()
    return _respond(info)


@_tool(name="jse_list_commands", annotations=RO)
def jse_list_commands(prefix: str = "") -> str:
    """List the raw JSE bridge commands available to jse_command.

    Args:
        prefix: optional filter, e.g. "jobs" or "campaign" or "corpus".

    Most work is covered by the dedicated tools; this is for reaching the parts
    of JSE that have no dedicated tool yet (hidden market, lanes, warm contacts,
    scraper plugins, settings).
    """
    try:
        import ast
    except Exception as exc:
        return _error(f"could not load the parser: {exc}")
    names: list[str] = []
    for module in ("documents", "lanes", "jobs", "scrapers", "intel", "insights", "corpus", "settings"):
        path = APP_ROOT / "bridge" / f"{module}.py"
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "COMMANDS" for t in node.targets
            ):
                if isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            names.append(key.value)
    names = sorted(set(names))
    if prefix:
        names = [n for n in names if n.lower().startswith(prefix.lower().rstrip(":"))]
    return _respond(
        {
            "count": len(names),
            "commands": names,
            "destructive": sorted(DESTRUCTIVE & set(names)),
            "long_running": sorted(LONG_RUNNING & set(names)),
            "destructive_unlocked": ALLOW_DESTRUCTIVE,
        }
    )


# --------------------------------------------------------------------------
# Tools: reading the board
# --------------------------------------------------------------------------


@_tool(name="jse_search_jobs", annotations=RO)
def jse_search_jobs(
    query: str = "",
    stage: str = "",
    company: str = "",
    location: str = "",
    source: str = "",
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    work_modes: str = "",
    date_from: str = "",
    has_interview: bool = False,
    profile_id: Optional[int] = None,
    include_all_profiles: bool = True,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Search the JSE job database and return a compact ranked list.

    Args:
        query: free text matched against title, company, location, ad text, analysis and notes.
        stage: one of new, interested, applied, interviewing, offer, rejected, rejected_by_company, archived.
        company: substring match on the employer.
        location: suburb or city; JSE expands known aliases.
        source: Seek, LinkedIn, HiringCafe, Manual, and so on.
        min_score / max_score: match_score bounds (0-100).
        work_modes: comma separated subset of hybrid, remote, wfh, onsite.
        date_from: ISO date; only jobs seen or updated on or after it.
        has_interview: only jobs with an interview recorded.
        profile_id: restrict to one lane (ignored when include_all_profiles is true).
        limit: rows returned, capped at 200. Use offset to page.

    Rows come back in JSE's own board order: priority, then due date, then
    warmth, then composite score. Ad text and analysis are stripped; use
    jse_get_job for one role in full.
    """
    limit = max(1, min(int(limit), 200))
    filters = _clean(
        {
            "query": query or None,
            "stage": stage or None,
            "company": company or None,
            "location": location or None,
            "source": source or None,
            "min_score": min_score,
            "max_score": max_score,
            "work_modes": work_modes or None,
            "date_from": date_from or None,
            "has_interview": True if has_interview else None,
            "profile_id": profile_id,
            "include_all_profiles": bool(include_all_profiles),
            "compact": True,
        }
    )
    try:
        data = _call("jobs:list", filters, 240)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    jobs = data.get("jobs") or []
    total = len(jobs)
    page = jobs[offset : offset + limit]
    keep = (
        "id", "title", "company", "actual_company", "advertiser_company", "location",
        "source", "pipeline_stage", "priority", "match_score", "composite_score",
        "fragment_score", "closing_date", "salary", "next_action", "next_action_date",
        "channel", "job_flags_types", "url", "profile_name", "warmth", "warmth_label",
    )
    rows = [{k: job.get(k) for k in keep if k in job} for job in page]
    return _respond(
        {
            "total_matching": total,
            "returned": len(rows),
            "offset": offset,
            "filters": filters,
            "jobs": rows,
        }
    )


@_tool(name="jse_get_job", annotations=RO)
def jse_get_job(
    job_id: int,
    include_description: bool = False,
    include_position_description: bool = False,
    analysis_chars: int = 4000,
) -> str:
    """Get one job in full: record, flags, events, interviews and application kits.

    Args:
        job_id: the JSE job id.
        include_description: include the full scraped ad text (can be many KB).
        include_position_description: include extracted PD text if JSE has it.
        analysis_chars: how much of the stored AI analysis to return; 0 to omit.

    Use this before writing an application: it carries the flags, the gate
    verdicts and the analysis JSE already produced for the role.
    """
    try:
        data = _call("jobs:detail", {"job_id": int(job_id)}, 240)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    if not data.get("job"):
        return _error(f"no job with id {job_id}", "Use jse_search_jobs to find the id.")
    keep: tuple[str, ...] = ()
    if include_description:
        keep += ("description",)
    if include_position_description:
        keep += ("position_description_text",)
    return _respond(_strip_heavy(data, keep=keep, analysis_chars=max(0, int(analysis_chars))))


@_tool(name="jse_dashboard", annotations=RO)
def jse_dashboard(profile_id: Optional[int] = None, include_all_profiles: bool = True) -> str:
    """Get the JSE dashboard: stage counts, due actions, top matches, awaiting feedback, cleanup due, last scrape."""
    try:
        data = _call(
            "dashboard:get",
            _clean({"profile_id": profile_id, "include_all_profiles": include_all_profiles, "compact": True}),
            240,
        )["data"]
    except BridgeError as exc:
        return _error(str(exc))
    return _respond(_strip_heavy(data, analysis_chars=0))


@_tool(name="jse_stats", annotations=RO)
def jse_stats(days: int = 7, profile_id: Optional[int] = None, include_all_profiles: bool = True) -> str:
    """Get activity statistics for the last N days, with the conversion band funnel and JSE's own recommendations."""
    try:
        data = _call(
            "stats:summary",
            _clean({"days": int(days), "profile_id": profile_id, "include_all_profiles": include_all_profiles}),
            240,
        )["data"]
    except BridgeError as exc:
        return _error(str(exc))
    return _respond(_strip_heavy(data, analysis_chars=0))


@_tool(name="jse_lanes", annotations=RO)
def jse_lanes() -> str:
    """List the search lanes/profiles: their ids, names, resumes and search settings.

    Every other tool's profile_id argument refers to these.
    """
    out: dict[str, Any] = {}
    for label, command in (("profiles", "profiles:list"), ("lanes", "lanes:list")):
        try:
            out[label] = _strip_heavy(_call(command, {}, 120)["data"], analysis_chars=0)
        except BridgeError as exc:
            out[label + "_error"] = str(exc)
    return _respond(out)


# --------------------------------------------------------------------------
# Tools: triage packets
# --------------------------------------------------------------------------


@_tool(name="jse_list_packets", annotations=RO)
def jse_list_packets(limit: int = 12) -> str:
    """List triage packets on disk, newest first, with sizes and row counts."""
    rows = []
    for path in _packet_files()[: max(1, int(limit))]:
        stat = path.stat()
        rows.append(
            {
                "file": path.name,
                "path": str(path),
                "size_mb": round(stat.st_size / 1048576, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "markdown": path.with_suffix(".md").name if path.with_suffix(".md").exists() else None,
            }
        )
    return _respond({"folder": str(SHORTLISTS_DIR), "count": len(rows), "packets": rows})


@_tool(name="jse_packet_summary", annotations=RO)
def jse_packet_summary(path: str = "", top: int = 20, deadline_days: int = 21) -> str:
    """Audit a triage packet without loading it: coverage, defects, deadlines, top roles.

    Args:
        path: packet filename or full path. Defaults to the newest packet.
        top: how many highest-composite roles to list.
        deadline_days: window for the deadline table.

    A packet is several MB of JSON and cannot be returned directly. This reads
    it on disk and returns: row and coverage counts, fit distribution, stage
    mix, the data-quality audit (commute nulls and placeholders, salary parse
    failures, fabricated warmth, placeholder closing dates, duplicate rows,
    junk company values), a deadline table using only dates that look genuine,
    and the top roles by composite score. Use jse_packet_jobs to page through
    rows and jse_get_job for any one role in full.
    """
    try:
        packet_path = _resolve_packet(path)
        packet = _load_packet(packet_path)
    except BridgeError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"could not read packet: {exc}")

    jobs = packet.get("jobs") or []
    n = len(jobs)
    if not n:
        return _respond({"file": packet_path.name, "count": 0, "note": "packet is empty"})

    scored = [j for j in jobs if j.get("match_score") is not None]
    composites = [j for j in jobs if j.get("composite_score") is not None]
    fragments = [j for j in jobs if j.get("fragment_score") is not None]

    # Closing dates: a date carried by a large share of rows is a default, not a
    # deadline. Flag those and exclude them from the deadline table.
    date_counts = Counter(str(j.get("closing_date"))[:10] for j in jobs if j.get("closing_date"))
    placeholder_dates = {d for d, c in date_counts.items() if c >= max(15, int(n * 0.04))}

    # Commute: distinguish missing from the two known stuck placeholder values.
    commute_values = [j.get("commute_km") for j in jobs]
    commute_null = sum(1 for v in commute_values if v in (None, ""))
    commute_placeholder = sum(1 for v in commute_values if str(v) in ("28.7", "0.0", "0"))
    commute_real = n - commute_null - commute_placeholder

    salary_string = sum(1 for j in jobs if j.get("salary"))
    salary_parsed = [j for j in jobs if j.get("salary_min") not in (None, "")]
    implausible = [
        {"id": j["id"], "company": j.get("company"), "salary": j.get("salary"), "salary_min": j.get("salary_min")}
        for j in salary_parsed
        if float(j.get("salary_min") or 0) < 40000
    ]

    warm_labelled = sum(1 for j in jobs if (j.get("warmth") or 0) > 0)
    warm_backed = sum(1 for j in jobs if j.get("warm_path"))

    unknown_company = sum(1 for j in jobs if str(j.get("company") or "").strip().lower() in ("", "unknown", "none"))
    company_is_advertiser = sum(1 for j in jobs if _norm(j.get("company")) and _norm(j.get("company")) == _norm(j.get("advertiser")))

    # Duplicates: normalised title against either employer field, because the
    # same ad is routinely scraped twice under "Coles" and "Coles Group".
    seen: dict[tuple, list] = {}
    for j in jobs:
        employer = _norm(j.get("company")) or _norm(j.get("advertiser"))
        seen.setdefault((_norm(j.get("title")), employer), []).append(j.get("id"))
    duplicates = {k: v for k, v in seen.items() if len(v) > 1}

    today = date.today()
    horizon = today + timedelta(days=int(deadline_days))
    deadlines = []
    for j in jobs:
        raw = str(j.get("closing_date") or "")[:10]
        parsed = _parse_date(raw)
        if not parsed or raw in placeholder_dates:
            continue
        if today <= parsed <= horizon:
            deadlines.append(
                {
                    "closes": raw,
                    "days_left": (parsed - today).days,
                    "id": j.get("id"),
                    "title": j.get("title"),
                    "company": j.get("company") or j.get("advertiser"),
                    "location": j.get("location"),
                    "composite": j.get("composite_score"),
                    "match": j.get("match_score"),
                    "stage": j.get("pipeline_stage"),
                }
            )
    deadlines.sort(key=lambda r: (r["closes"], -(r["composite"] or 0)))

    ranked = sorted(
        jobs,
        key=lambda j: (-(j.get("composite_score") or j.get("match_score") or 0), str(j.get("closing_date") or "9999")),
    )[: max(1, int(top))]

    return _respond(
        {
            "file": packet_path.name,
            "path": str(packet_path),
            "generated_at": packet.get("generated_at"),
            "profile_id": packet.get("profile_id"),
            "include_all_profiles": packet.get("include_all_profiles"),
            "coverage": {
                "rows": n,
                "scored": len(scored),
                "scored_pct": round(100 * len(scored) / n),
                "unscored": n - len(scored),
                "has_composite": len(composites),
                "has_fragment_score": len(fragments),
                "fit_levels": dict(Counter(_fit_level(j) or "unscored" for j in jobs).most_common()),
                "stages": dict(Counter(j.get("pipeline_stage") or "unknown" for j in jobs).most_common()),
                "sources": dict(Counter(j.get("source") or "unknown" for j in jobs).most_common(12)),
                "match_score_distinct_values": len({j.get("match_score") for j in scored}),
            },
            "data_quality": {
                "commute": {
                    "missing": commute_null,
                    "missing_pct": round(100 * commute_null / n),
                    "placeholder_28_7_or_0": commute_placeholder,
                    "plausible_measurements": commute_real,
                    "verdicts": dict(Counter(j.get("commute_verdict") or "none" for j in jobs).most_common()),
                },
                "salary": {
                    "rows_with_salary_text": salary_string,
                    "rows_with_parsed_min": len(salary_parsed),
                    "parsed_min_below_40k": len(implausible),
                    "examples": implausible[:10],
                },
                "warmth": {
                    "rows_labelled_warm": warm_labelled,
                    "rows_with_an_actual_contact": warm_backed,
                    "fabricated": warm_labelled - warm_backed,
                },
                "closing_dates": {
                    "rows_with_a_date": sum(date_counts.values()),
                    "placeholder_dates": {d: date_counts[d] for d in sorted(placeholder_dates)},
                    "placeholder_share_pct": round(100 * sum(date_counts[d] for d in placeholder_dates) / n),
                    "genuine_dates": sum(c for d, c in date_counts.items() if d not in placeholder_dates),
                },
                "company_field": {
                    "unknown_or_blank": unknown_company,
                    "equals_advertiser": company_is_advertiser,
                },
                "duplicates": {
                    "duplicate_groups": len(duplicates),
                    "extra_rows": sum(len(v) - 1 for v in duplicates.values()),
                    "examples": [
                        {"title": k[0], "employer": k[1], "ids": v}
                        for k, v in list(duplicates.items())[:8]
                    ],
                },
            },
            "deadlines_within_days": int(deadline_days),
            "deadlines": deadlines[:60],
            "top_by_composite": [_packet_row(j) for j in ranked],
        }
    )


@_tool(name="jse_packet_jobs", annotations=RO)
def jse_packet_jobs(
    path: str = "",
    query: str = "",
    stage: str = "",
    min_composite: Optional[int] = None,
    max_commute_km: Optional[float] = None,
    closes_before: str = "",
    unscored_only: bool = False,
    exclude_placeholder_dates: bool = True,
    sort: str = "composite",
    limit: int = 30,
    offset: int = 0,
    analysis_chars: int = 0,
) -> str:
    """Page through the rows of a triage packet with filters.

    Args:
        path: packet file; defaults to the newest.
        query: substring matched against title, company, advertiser and location.
        stage: pipeline stage filter.
        min_composite: composite score floor.
        max_commute_km: only rows with a measured commute at or under this.
        closes_before: ISO date; only rows closing on or before it.
        unscored_only: only rows JSE never analysed. These sort nowhere in a
            ranked view and are where genuine misses hide.
        exclude_placeholder_dates: ignore default closing dates when filtering by date.
        sort: composite, match, closing, or title.
        analysis_chars: characters of stored analysis per row; 0 to omit.
    """
    try:
        packet_path = _resolve_packet(path)
        packet = _load_packet(packet_path)
    except BridgeError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"could not read packet: {exc}")

    jobs = list(packet.get("jobs") or [])
    n_all = len(jobs)
    date_counts = Counter(str(j.get("closing_date"))[:10] for j in jobs if j.get("closing_date"))
    placeholder_dates = {d for d, c in date_counts.items() if c >= max(15, int(n_all * 0.04))}

    if query:
        needle = query.lower()
        jobs = [
            j
            for j in jobs
            if needle in " ".join(
                str(j.get(k) or "") for k in ("title", "company", "advertiser", "location")
            ).lower()
        ]
    if stage:
        jobs = [j for j in jobs if (j.get("pipeline_stage") or "") == stage]
    if min_composite is not None:
        jobs = [j for j in jobs if (j.get("composite_score") or j.get("match_score") or 0) >= min_composite]
    if max_commute_km is not None:
        jobs = [
            j
            for j in jobs
            if j.get("commute_km") not in (None, "") and float(j["commute_km"]) <= float(max_commute_km)
        ]
    if unscored_only:
        jobs = [j for j in jobs if j.get("match_score") is None]
    if closes_before:
        cutoff = _parse_date(closes_before)
        if cutoff:
            filtered = []
            for j in jobs:
                raw = str(j.get("closing_date") or "")[:10]
                if exclude_placeholder_dates and raw in placeholder_dates:
                    continue
                parsed = _parse_date(raw)
                if parsed and parsed <= cutoff:
                    filtered.append(j)
            jobs = filtered

    keys = {
        "composite": lambda j: -(j.get("composite_score") or j.get("match_score") or 0),
        "match": lambda j: -(j.get("match_score") or 0),
        "closing": lambda j: str(j.get("closing_date") or "9999-12-31"),
        "title": lambda j: str(j.get("title") or ""),
    }
    jobs.sort(key=keys.get(sort, keys["composite"]))

    limit = max(1, min(int(limit), 150))
    page = jobs[offset : offset + limit]
    return _respond(
        {
            "file": packet_path.name,
            "packet_rows": n_all,
            "matching": len(jobs),
            "returned": len(page),
            "offset": offset,
            "placeholder_dates_ignored": sorted(placeholder_dates) if exclude_placeholder_dates else [],
            "jobs": [_packet_row(j, max(0, int(analysis_chars))) for j in page],
        }
    )


@_tool(name="jse_export_packet", annotations=RW)
def jse_export_packet(
    profile_id: int = 1,
    include_all_profiles: bool = True,
    stages: str = "new,interested",
    min_score: Optional[int] = None,
    limit: Optional[int] = None,
    include_screened_out: bool = False,
    exclude_flags: str = "",
    output_dir: str = "",
    format: str = "both",
) -> str:
    """Export a fresh triage packet from the current database state.

    Args:
        stages: comma separated pipeline stages to include (default new,interested).
        min_score: match score floor; omit for no floor.
        limit: cap the packet; omit for the whole sweep.
        include_screened_out: include roles the commute gate blocked before analysis.
        exclude_flags: comma separated flag types to drop.
        output_dir: write elsewhere than the configured shortlists folder.
        format: markdown, json, or both.

    Writes the file(s) and returns the paths and row count. Read the result with
    jse_packet_summary, not by opening the file.
    """
    payload = _clean(
        {
            "profile_id": profile_id,
            "include_all_profiles": include_all_profiles,
            "stages": [s.strip() for s in stages.split(",") if s.strip()] or None,
            "min_score": min_score,
            "limit": limit,
            "include_screened_out": include_screened_out or None,
            "exclude_flags": [s.strip() for s in exclude_flags.split(",") if s.strip()] or None,
            "output_dir": output_dir or None,
            "format": format,
        }
    )
    try:
        data = _call("jobs:exportShortlist", payload, 900)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    data.pop("job_ids", None)  # a packet can carry thousands; the files have them
    return _respond(data)


# --------------------------------------------------------------------------
# Tools: writing to the board
# --------------------------------------------------------------------------


@_tool(name="jse_set_stage", annotations=RW)
def jse_set_stage(job_id: int, stage: str, note: str = "") -> str:
    """Move a job to a pipeline stage, optionally recording a note against it.

    Args:
        job_id: the JSE job id.
        stage: new, interested, applied, interviewing, offer, rejected, rejected_by_company, archived.
        note: free text stored as an application event.

    JSE records the transition as an event and captures an outcome snapshot, so
    this is the right way to mark something applied rather than editing the row.
    """
    if stage not in PIPELINE_STAGES:
        return _error(
            f"'{stage}' is not a pipeline stage",
            "Valid stages: " + ", ".join(PIPELINE_STAGES),
        )
    try:
        data = _call("jobs:updateStatus", {"job_id": int(job_id), "status": stage}, 120)["data"]
        if note:
            _call(
                "events:add",
                {"job_id": int(job_id), "event_type": "note", "title": "Note", "details": note},
                120,
            )
    except BridgeError as exc:
        return _error(str(exc))
    job = _strip_heavy(data.get("job") or {}, analysis_chars=0)
    return _respond({"ok": True, "job_id": job_id, "stage": stage, "note_recorded": bool(note), "job": job})


@_tool(name="jse_update_job", annotations=RW)
def jse_update_job(job_id: int, updates: dict) -> str:
    """Update fields on a job record.

    Args:
        job_id: the JSE job id.
        updates: field/value map. Writable fields include pipeline_stage,
            priority, notes, next_action, next_action_date, application_date,
            application_url, contact_person, contact_email, contact_phone,
            resume_used, cover_letter_path, position_description_path,
            interview_date, interview_type, interview_people, feedback, salary,
            closing_date, actual_company, advertiser_company, employer_type,
            retired_reason.

    Anything outside that set is reported back as rejected rather than silently
    dropped. Use jse_set_stage for stage moves so the event and outcome snapshot
    are written.
    """
    if not isinstance(updates, dict) or not updates:
        return _error("updates must be a non-empty object of field/value pairs")
    rejected = sorted(set(updates) - UPDATABLE_FIELDS)
    accepted = {k: v for k, v in updates.items() if k in UPDATABLE_FIELDS}
    if not accepted:
        return _error(
            "none of those fields are writable",
            "Writable: " + ", ".join(sorted(UPDATABLE_FIELDS)),
        )
    try:
        data = _call("jobs:update", {"job_id": int(job_id), "updates": accepted}, 120)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    return _respond(
        {
            "ok": True,
            "job_id": job_id,
            "applied": sorted(accepted),
            "rejected_fields": rejected,
            "job": _strip_heavy(data.get("job") or {}, analysis_chars=0),
        }
    )


@_tool(name="jse_add_note", annotations=RW)
def jse_add_note(
    job_id: int,
    details: str,
    title: str = "Note",
    event_type: str = "note",
    event_date: str = "",
    due_date: str = "",
) -> str:
    """Record an event against a job: a note, a call, a follow-up with a due date.

    Args:
        event_type: note, call, email, interview, follow_up.
        event_date / due_date: ISO dates, optional.
    """
    payload = _clean(
        {
            "job_id": int(job_id),
            "event_type": event_type,
            "title": title,
            "details": details,
            "event_date": event_date or None,
            "due_date": due_date or None,
        }
    )
    try:
        data = _call("events:add", payload, 120)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    events = (data.get("events") or [])[-5:]
    return _respond({"ok": True, "job_id": job_id, "recent_events": events})


@_tool(name="jse_add_flag", annotations=RW)
def jse_add_flag(
    job_id: int,
    type: str,
    requirement: str,
    detail: str = "",
    confidence: str = "high",
) -> str:
    """Flag a requirement on a job by hand.

    Args:
        type: the flag type, e.g. evidence_gap, credential, seniority, location, salary.
        requirement: the requirement text the flag is about.
        detail: why it matters.
        confidence: high, medium or low.

    Manual flags survive re-analysis, unlike the ones the scorer writes.
    """
    payload = {
        "job_id": int(job_id),
        "type": type,
        "requirement": requirement,
        "detail": detail,
        "confidence": confidence,
    }
    try:
        data = _call("jobs:addFlag", payload, 120)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    return _respond({"ok": True, **_strip_heavy(data, analysis_chars=0)})


@_tool(name="jse_set_channel", annotations=RW)
def jse_set_channel(job_id: int, channel: str = "") -> str:
    """Set how an application reaches the employer (board, direct, referral, recruiter). Empty clears it back to derived."""
    try:
        data = _call("jobs:setChannel", _clean({"job_id": int(job_id), "channel": channel or None}), 120)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    return _respond({"ok": True, **_strip_heavy({k: v for k, v in data.items() if k != "job"}, analysis_chars=0)})


@_tool(name="jse_log_external_application", annotations=RW)
def jse_log_external_application(
    title: str,
    company: str,
    url: str = "",
    location: str = "",
    salary: str = "",
    notes: str = "",
    doc_used: str = "",
    profile_id: int = 1,
) -> str:
    """Log an application made outside JSE so the pipeline and outcome stats stay honest.

    Creates the job at the applied stage and records the document used. Use this
    for anything applied for by email or on an employer portal that JSE never
    scraped.
    """
    payload = _clean(
        {
            "title": title,
            "company": company,
            "url": url or None,
            "location": location or None,
            "salary": salary or None,
            "notes": notes or None,
            "doc_used": doc_used or None,
            "profile_id": profile_id,
        }
    )
    try:
        data = _call("jobs:logExternal", payload, 180)["data"]
    except BridgeError as exc:
        return _error(str(exc))
    return _respond({"ok": True, **_strip_heavy(data, analysis_chars=0)})


# --------------------------------------------------------------------------
# Tools: long-running work
# --------------------------------------------------------------------------


@_tool(name="jse_start_task", annotations=RW)
def jse_start_task(command: str, payload: Optional[dict] = None) -> str:
    """Start a long-running JSE command in its own process and return a task id.

    Args:
        command: scrape:run, analysis:run, docs:generate, company:researchBatch,
            corpus:mine, enrichment:process, memory:scan, campaign:plan ...
        payload: the command's arguments. Common ones:
            scrape:run    {"profile_id": 1, "include_all_profiles": true, "sources": ["Seek"]}
            analysis:run  {"profile_id": 1, "stage": "new", "include_all_profiles": true}
            docs:generate {"job_id": 12345, "profile_id": 1}

    These take minutes, so they run detached. Poll with jse_task_status. This is
    the same one-process-per-task model the app uses so a run can be cancelled
    without taking anything else down.
    """
    if command in DESTRUCTIVE and not ALLOW_DESTRUCTIVE:
        return _error(
            f"'{command}' is destructive and this server is in safe mode",
            "Set JSE_MCP_ALLOW_DESTRUCTIVE=1 in the server env and restart Claude.",
        )
    if not BRIDGE.exists():
        return _error(f"JSE bridge not found at {BRIDGE}")
    task_id = _start_task(command, payload or {})
    return _respond(
        {
            "ok": True,
            "task_id": task_id,
            "command": command,
            "note": "Running detached. Poll jse_task_status for progress; a full "
            "scrape or analysis pass can take many minutes.",
        }
    )


@_tool(name="jse_task_status", annotations=RO)
def jse_task_status(task_id: str = "", log_tail: int = 25) -> str:
    """Check background tasks started with jse_start_task. Omit task_id to list them all."""
    with TASKS_LOCK:
        records = dict(TASKS)
    if not task_id:
        return _respond(
            {
                "count": len(records),
                "tasks": [
                    {
                        "task_id": r["task_id"],
                        "command": r["command"],
                        "status": r["status"],
                        "started_at": r["started_at"],
                        "finished_at": r["finished_at"],
                        "progress": r["progress"],
                    }
                    for r in sorted(records.values(), key=lambda r: r["started_at"], reverse=True)
                ],
            }
        )
    record = records.get(task_id)
    if not record:
        return _error(f"no task {task_id}", "Call jse_task_status with no arguments to list tasks.")
    tail = max(0, int(log_tail))
    return _respond(
        {
            "task_id": record["task_id"],
            "command": record["command"],
            "status": record["status"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "progress": record["progress"],
            "error": record["error"],
            "result": _strip_heavy(record["result"], analysis_chars=0),
            "log_tail": record["logs"][-tail:] if tail else [],
        }
    )


# --------------------------------------------------------------------------
# Tool: raw bridge access
# --------------------------------------------------------------------------


@_tool(name="jse_command", annotations=RW)
def jse_command(command: str, payload: Optional[dict] = None, timeout_seconds: int = 180) -> str:
    """Call any JSE bridge command directly. The escape hatch for everything without a dedicated tool.

    Args:
        command: e.g. campaign:summary, hiddenMarket:get, warmContacts:list,
            targeting:summary, scrapers:list, settings:get, corpus:stats.
        payload: the command's arguments.
        timeout_seconds: give up after this long.

    Use jse_list_commands to see what exists. Destructive commands are refused
    unless the server was started with JSE_MCP_ALLOW_DESTRUCTIVE=1. Long-running
    commands should go through jse_start_task instead so they do not time out.
    """
    if command in LONG_RUNNING:
        return _error(
            f"'{command}' is long-running",
            "Start it with jse_start_task and poll jse_task_status.",
        )
    try:
        result = _call(command, payload or {}, float(timeout_seconds))
    except BridgeError as exc:
        return _error(str(exc))
    return _respond(
        {
            "command": command,
            "data": _strip_heavy(result["data"], analysis_chars=1200),
            "logs": result["logs"][-10:],
        }
    )


if __name__ == "__main__":
    log(f"serving JSE at {APP_ROOT} via {PYTHON}")
    server.run()
