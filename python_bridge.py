"""JSON command bridge between Electron and the Python business logic.

One-shot UI calls can run in persistent worker mode via newline-delimited JSON
frames. Long-running cancellable tasks are still launched as fresh processes by
Electron so cancellation can terminate the whole task safely.

This file is the entrypoint and the dispatch table only; the ~170 command
implementations live in the `bridge/` package, grouped by command prefix. Each
module there declares its own COMMANDS mapping and this file merges them, so
adding a command does not mean editing this file.

Electron spawns this path directly (`python_bridge.py --serve`), so the sys.path
bootstrap below has to run before anything else is imported.
"""
import contextlib
import json
import os
import site
import sys
import threading
from pathlib import Path

APP_ROOT = Path(os.environ.get("JSE_APP_ROOT") or Path(__file__).resolve().parent)
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

sys.path.append(site.getusersitepackages())

import concurrency  # noqa: E402
from db_setup import setup_database  # noqa: E402

from bridge import runtime  # noqa: E402
from bridge.runtime import (  # noqa: E402,F401
    JobNotLiveError,
    BlockerGateError,
    bridge_error_message,
    emit,
    load_json_payload,
)
from bridge import (  # noqa: E402
    documents,
    lanes,
    jobs,
    scrapers,
    intel,
    insights,
    corpus,
    settings,
)


# One dispatch table, merged from the per-module ones. A duplicate key would
# mean two handlers silently competing for the same command, so it is refused.
COMMANDS = {}
for _module in (documents, lanes, jobs, scrapers, intel, insights, corpus, settings):
    for _name, _handler in _module.COMMANDS.items():
        if _name in COMMANDS:
            raise ImportError(f"duplicate bridge command {_name!r} in {_module.__name__}")
        COMMANDS[_name] = _handler


def main():
    if len(sys.argv) < 2:
        raise ValueError("Missing bridge command.")

    command = sys.argv[1]
    handler = COMMANDS.get(command)
    if handler is None:
        raise ValueError(f"Unknown bridge command: {command}")

    payload = load_json_payload()
    concurrency.cancel_event.clear()
    concurrency.paused.set()
    result = handler(payload)
    emit("result", data=result)


def _handle_serve_request(request_id, command, payload):
    runtime.set_request_id(request_id)
    try:
        handler = COMMANDS.get(command)
        if handler is None:
            emit("error", message=f"Unknown bridge command: {command}")
            return
        result = handler(payload or {})
        emit("result", data=result)
    except Exception as exc:
        emit("error", message=bridge_error_message(exc))
    finally:
        runtime.set_request_id(None)


def serve():
    """Persistent worker: handle newline-framed {id, command, payload} requests, one
    thread per request, so imports and the SQLite warmup are paid once for the whole
    session instead of per call. Used for the one-shot bridge:invoke path; long-running
    cancellable tasks still spawn their own process."""
    # Pin protocol output to the real stdout, then send everything else to stderr so no
    # stray print can corrupt the JSON framing the Electron main process parses.
    runtime.use_protocol_stream(sys.stdout)
    sys.stdout = sys.stderr

    concurrency.cancel_event.clear()
    concurrency.paused.set()

    with contextlib.redirect_stdout(sys.stderr):
        try:
            setup_database()
        except Exception as exc:
            emit("log", message=f"Worker warmup failed: {bridge_error_message(exc)}")

    while True:
        raw = sys.stdin.readline()
        if not raw:
            break  # stdin closed -> Electron is shutting the worker down
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        thread = threading.Thread(
            target=_handle_serve_request,
            args=(request.get("id"), request.get("command"), request.get("payload") or {}),
            daemon=True,
        )
        thread.start()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        serve()
    else:
        try:
            main()
        except Exception as exc:
            emit("error", message=bridge_error_message(exc))
            sys.exit(1)
