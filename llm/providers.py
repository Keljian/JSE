"""Provider transport: the LLM concurrency gate, HTTP helpers, and the call* family.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
import contextlib
import functools
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from config import MY_INFO
import concurrency
import database_manager as db

# --- Local OpenAI-compatible client defaults ---
UNSLOTH_BASE_URL = MY_INFO.get("unsloth_base_url", "https://api.unloth.studio/v1")


UNSLOTH_API_KEY = MY_INFO.get("unsloth_api_key", "")


UNSLOTH_MODEL = MY_INFO.get("unsloth_model", "unsloth/llama-3-70b-instruct")


UNSLOTH_IS_CONFIGURED = bool(UNSLOTH_API_KEY and UNSLOTH_API_KEY != "YOUR_UNSLOTH_API_KEY")


DEFAULT_LOCAL_BASE_URL = MY_INFO.get("local_base_url") or UNSLOTH_BASE_URL or "http://localhost:1234/v1"


DEFAULT_LOCAL_MODEL = MY_INFO.get("local_model") or MY_INFO.get("unsloth_model", "")


UNSLOTH_MAX_RETRIES = MY_INFO.get("unsloth_max_retries", 3)


UNSLOTH_RETRY_DELAY = MY_INFO.get("unsloth_retry_delay", 5)


# Longest a backoff between attempts is allowed to grow to.
UNSLOTH_RETRY_MAX_DELAY = MY_INFO.get("unsloth_retry_max_delay", 120)


# --- Local request timeout ---------------------------------------------------
# A local endpoint is not a hosted one: it generates at the speed of the machine
# it is on, so the wait scales with how many tokens were asked for. A flat 120s
# was under the honest generation time for anything but the smallest budget, and
# a client-side timeout is the worst possible outcome — the server keeps
# generating the abandoned request, so the retry lands on a still-busy endpoint
# and the run degrades into timeouts and 429s. Budget generously instead.
# Floor covers connect + prompt prefill; the per-token allowance assumes a
# deliberately pessimistic ~8 tokens/sec so a slow or CPU-offloaded runtime is
# still given time to finish rather than being abandoned mid-generation.
LOCAL_TIMEOUT_FLOOR = MY_INFO.get("local_timeout_floor", 90)


LOCAL_TIMEOUT_SECONDS_PER_1K_TOKENS = MY_INFO.get("local_timeout_seconds_per_1k_tokens", 120)


LOCAL_TIMEOUT_CEILING = MY_INFO.get("local_timeout_ceiling", 1200)


# A timed-out request is still running on the server. Retrying on the ordinary
# short backoff is what stacks requests on a single-slot endpoint, so a timeout
# gets its own, much longer cooldown to let the abandoned generation drain.
LOCAL_TIMEOUT_COOLDOWN = MY_INFO.get("local_timeout_cooldown", 45)


# --- Local context window -----------------------------------------------------
# A local server serves whatever window the model was *loaded* with, which is
# often far below the model's native context — Unsloth Studio, for one, will
# happily load a 262K-native Qwen3 at 4096. Nothing in the OpenAI protocol
# announces that: the server silently truncates the generation at the window and
# returns finish_reason="length", which reaches JSE as a half-written JSON object
# and looks like a model that cannot follow instructions. So ask the endpoint how
# much room it actually has, size the output budget to fit, and treat a
# truncated response as the failure it is.
#
# When an endpoint does not report a window — many OpenAI-compatible servers do
# not — nothing is assumed on its behalf: the output cap below still applies and
# the truncation check still catches a window that turns out to be too small.
#
# Output can never exceed this even in a large window; per-call budgets still
# decide the real size.
LOCAL_MAX_OUTPUT_TOKENS = MY_INFO.get("local_max_output_tokens", 16384)


# Leave the window some slack: the prompt estimate below is approximate, and
# chat templates add tokens JSE never sees.
LOCAL_CONTEXT_RESERVE = MY_INFO.get("local_context_reserve", 256)


# Squeezing a structured answer into fewer tokens than this produces truncated
# JSON, not a smaller answer. Below it, fail with an explanation instead.
LOCAL_MIN_OUTPUT_TOKENS = MY_INFO.get("local_min_output_tokens", 512)


# Slightly under English's ~4 chars/token, so the estimate leans towards
# over-counting the prompt — but only slightly. Over-counting refuses a request
# that would have fitted, while under-counting merely lets it through to the
# truncation check below, which reports the same problem from the server's own
# numbers. The cheaper mistake is the second one.
LOCAL_CHARS_PER_TOKEN = 3.8


# The window only changes when a model is loaded or unloaded, so cache it rather
# than paying a /models round trip on every request.
LOCAL_CONTEXT_TTL = 60


# --- Setting the window from JSE ---------------------------------------------
# Unsloth Studio can reload the live model over its own API
# (POST /v1/load with max_seq_length), so JSE does not have to leave the user to
# discover a too-small window by hand. Two things make this worth doing rather
# than only reporting the problem:
#   * The window is usually small by default, not by necessity. A GGUF loaded
#     with max_seq_length=0 takes the file's default — 4096 on the Qwen3 that
#     prompted this, against a 262144 native context.
#   * llama-server splits its KV budget across --parallel slots. JSE holds the
#     local endpoint to one in-flight request by design (see _local_slot), so
#     the other slots cost window for nothing.
# A reload is disruptive: it replaces the running llama-server, takes as long as
# the weights take to page in, and is visible to anything else using the Studio.
# So it is deliberate, never silent, and never cancels someone's in-flight chat
# (force_cancel_active stays False — a 409 is the correct answer to "somebody is
# using this").
LOCAL_CONTEXT_TARGET_DEFAULT = 32768


# Loading tens of GB of weights is not a 120-second operation.
LOCAL_LOAD_TIMEOUT = MY_INFO.get("local_load_timeout", 900)


# Fields describing *how* the current model is loaded, as opposed to what JSE is
# changing. A reload re-sends them so setting the window does not quietly undo
# the user's quantization, offload, or speculative-decoding choices.
LOCAL_LOAD_PASSTHROUGH = (
    "gguf_variant",
    "cache_type_kv",
    "speculative_type",
    "spec_draft_n_max",
    "gpu_memory_mode",
    "gpu_layers",
    "n_cpu_moe",
    "n_batch",
    "n_ubatch",
    "tensor_parallel",
    "tensor_split",
    "gpu_ids",
    "chat_template_override",
    "trust_remote_code",
)


_local_context_cache = {}


_local_context_lock = threading.Lock()


# --- Global LLM concurrency gate --------------------------------------------
# A local inference server typically serves one request at a time and returns
# HTTP 429 when a second arrives mid-flight (its queue depth is often zero).
# The analysis worker pool, the keyword-retry pool, live analysis, and document
# generation can all reach the endpoint at once, which produced "Too Many
# Requests". This gate caps concurrent outbound LLM requests to the configured
# number of slots (the analysis_workers setting, default 1) so callers queue
# instead of overwhelming the server. It spans every provider because scoring,
# documents, research, and memory usually share the same local endpoint; raise
# the setting only when the active endpoint genuinely serves parallel requests.
_llm_gate_lock = threading.Lock()


_llm_gate = None


_llm_gate_size = None


@contextlib.contextmanager
def _llm_slot():
    """Block until an LLM slot is free, honouring runtime cancellation.

    The semaphore is (re)sized lazily from the current setting so a settings
    change takes effect without a restart. A queued caller polls the cancel
    event so a user cancel doesn't leave it wedged behind a long request.
    """
    global _llm_gate, _llm_gate_size
    with _llm_gate_lock:
        limit = _analysis_worker_count()
        if _llm_gate is None or _llm_gate_size != limit:
            _llm_gate = threading.BoundedSemaphore(limit)
            _llm_gate_size = limit
        gate = _llm_gate
    while not gate.acquire(timeout=0.5):
        if concurrency.cancel_event.is_set():
            raise concurrency.OperationCancelledError("Operation cancelled while awaiting an LLM slot.")
    try:
        yield
    finally:
        gate.release()


def _interruptible_sleep(seconds):
    """Sleep in slices so a user cancel is noticed during a long backoff.

    A plain time.sleep here made cancel feel broken: the run kept "hanging" for
    the remainder of a backoff that nobody was waiting on any more.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if concurrency.cancel_event.is_set():
            raise concurrency.OperationCancelledError("Operation cancelled during retry backoff.")
        time.sleep(min(0.5, deadline - time.monotonic()))


def _backoff_delay(attempt, base=None, cap=None):
    """Exponential backoff. Linear backoff retried too fast to let a busy local
    endpoint actually drain, which turned one slow request into a queue of them."""
    base = UNSLOTH_RETRY_DELAY if base is None else base
    cap = UNSLOTH_RETRY_MAX_DELAY if cap is None else cap
    return min(cap, base * (2 ** max(0, attempt - 1)))


# --- Local endpoint serialization (cross-process) ----------------------------
# The semaphore above is per-process, and JSE runs several: the persistent
# bridge:invoke worker plus one fresh process per long-running task. A sweep
# analysing jobs in its own process and a company-research or document task
# started from the UI therefore each believed they held the only slot, and both
# hit the single-slot local endpoint at once. A lock file next to the database
# gives every JSE process one shared slot.
#
# It fails open: if the lock cannot be created or refreshed for any reason, the
# call proceeds ungated. Losing serialization degrades to today's behaviour,
# whereas a lock that can wedge would take the whole app down with it.
LOCAL_LOCK_HEARTBEAT = 5


# A holder refreshes the lock's mtime while it works, so "not refreshed
# recently" is a reliable stale signal — including for a task process that was
# killed mid-request by a cancel, which is the common case.
LOCAL_LOCK_STALE_AFTER = 30


def _local_lock_path():
    try:
        db_file = getattr(db, "DB_FILE", None)
        if not db_file:
            return None
        return os.path.join(os.path.dirname(os.path.abspath(db_file)), ".jse_local_llm.lock")
    except Exception:
        return None


@contextlib.contextmanager
def _local_endpoint_lock():
    """One in-flight local request across every JSE process."""
    path = _local_lock_path()
    handle = None
    if path:
        try:
            while handle is None:
                if concurrency.cancel_event.is_set():
                    raise concurrency.OperationCancelledError(
                        "Operation cancelled while awaiting the local endpoint."
                    )
                try:
                    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                except FileExistsError:
                    try:
                        stale = (time.time() - os.path.getmtime(path)) > LOCAL_LOCK_STALE_AFTER
                    except OSError:
                        continue  # holder released it between the two calls
                    if stale:
                        # Two waiters can race here and both proceed. That is no
                        # worse than the ungated behaviour this replaces, and
                        # far better than a dead process locking out the app.
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                        continue
                    time.sleep(0.25)
        except concurrency.OperationCancelledError:
            raise
        except OSError:
            handle = None  # fail open

    if handle is None:
        yield
        return

    try:
        os.write(handle, str(os.getpid()).encode("ascii", errors="ignore"))
    except OSError:
        pass

    stop = threading.Event()

    def _heartbeat():
        while not stop.wait(LOCAL_LOCK_HEARTBEAT):
            try:
                os.utime(path, None)
            except OSError:
                return

    beat = threading.Thread(target=_heartbeat, name="local-llm-lock", daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        for close in (lambda: os.close(handle), lambda: os.unlink(path)):
            try:
                close()
            except OSError:
                pass


# The local endpoint is single-slot regardless of what the general gate allows.
# The general gate is sized from the *scoring* provider, so a lane scoring on a
# hosted provider with analysis_workers=8 opened 8 slots — and any call routed
# to local (documents, research, memory, keyword retry) could then go 8-wide
# against a server that answers one request at a time.
_local_gate = threading.Semaphore(1)


@contextlib.contextmanager
def _local_slot():
    """The single local slot: one per process, then one across processes.

    Always acquired in that order (and always inside the general gate) so the
    three locks can never be taken in a cycle.
    """
    while not _local_gate.acquire(timeout=0.5):
        if concurrency.cancel_event.is_set():
            raise concurrency.OperationCancelledError("Operation cancelled while awaiting the local endpoint.")
    try:
        with _local_endpoint_lock():
            yield
    finally:
        _local_gate.release()


def _serialized_llm_call(fn):
    """Route a provider entry point through the shared concurrency gate.

    Applied to the four provider functions (the single points every outbound
    LLM request passes through). The dispatcher itself is intentionally NOT
    gated, so a dispatched call acquires exactly one slot — no re-entrant
    deadlock against the non-reentrant semaphore.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _llm_slot():
            return fn(*args, **kwargs)
    return wrapper


def _serialized_local_call(fn):
    """As above, plus the single local slot — in-process and across processes."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _llm_slot(), _local_slot():
            return fn(*args, **kwargs)
    return wrapper


class LLMHTTPError(Exception):
    def __init__(self, status_code, message, body="", retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


class LLMRequestError(Exception):
    pass


class LLMTruncatedError(Exception):
    """The server stopped generating at a token limit, mid-answer."""


def _post_json(url, headers, payload, timeout=120):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        error = LLMHTTPError(exc.code, f"{exc.code} {exc.reason}", raw)
        # A server that says when to come back knows better than our backoff.
        try:
            retry_after = (exc.headers or {}).get("Retry-After")
            error.retry_after = float(str(retry_after).strip()) if retry_after else None
        except (AttributeError, TypeError, ValueError):
            error.retry_after = None
        raise error from exc
    except TimeoutError as exc:
        raise TimeoutError(str(exc)) from exc
    except (URLError, OSError, json.JSONDecodeError) as exc:
        # urllib raises URLError(socket.timeout) rather than TimeoutError when
        # the read stalls, so an honest timeout was being reported as a generic
        # request error and retried on the short backoff.
        if isinstance(getattr(exc, "reason", None), (TimeoutError, OSError)) and "timed out" in str(exc).lower():
            raise TimeoutError(str(exc)) from exc
        raise LLMRequestError(str(exc)) from exc


def _get_json(url, headers, timeout=15):
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise LLMHTTPError(exc.code, f"{exc.code} {exc.reason}", raw) from exc
    except TimeoutError as exc:
        raise TimeoutError(str(exc)) from exc
    except (URLError, OSError, json.JSONDecodeError) as exc:
        raise LLMRequestError(str(exc)) from exc


def _local_ai_settings(overrides=None):
    try:
        settings = db.get_app_settings()
    except Exception:
        settings = {}
    settings = {**settings, **(overrides or {})}
    base_url = (settings.get("local_base_url") or DEFAULT_LOCAL_BASE_URL or "http://localhost:1234/v1").rstrip("/")
    if base_url.lower() in {"http://localhost:8888/api", "http://127.0.0.1:8888/api"}:
        base_url = f"{base_url[:-4]}/v1"
    try:
        context_target = int(str(settings.get("local_context_target") or LOCAL_CONTEXT_TARGET_DEFAULT).strip())
    except (TypeError, ValueError):
        context_target = LOCAL_CONTEXT_TARGET_DEFAULT
    return {
        "base_url": base_url,
        "api_key": str(settings.get("local_api_key") or UNSLOTH_API_KEY or "").strip(),
        "model": str(settings.get("local_model") or DEFAULT_LOCAL_MODEL or "").strip(),
        "context_target": max(0, context_target),
        "auto_reload": str(settings.get("local_context_autoload", "1")).strip().lower() in {"1", "true", "yes", "on"},
    }


def _local_auth_headers(local):
    headers = {"Content-Type": "application/json"}
    if local.get("api_key"):
        headers["Authorization"] = f"Bearer {local['api_key']}"
    return headers


def _local_model_rows(local):
    """The /models catalogue, as a list of dicts. Empty when unavailable."""
    data = _get_json(f"{local['base_url']}/models", _local_auth_headers(local), timeout=15)
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [row for row in models if isinstance(row, dict) and str(row.get("id") or "").strip()]


def _loaded_model_row(rows, model_id=""):
    """The catalogue entry serving requests right now.

    Unsloth Studio lists every model it *could* load alongside the one it has
    loaded, so the first entry is not reliably the live one. Prefer the
    configured id, then anything flagged loaded, then the first entry.
    """
    wanted = str(model_id or "").strip().lower()
    if wanted:
        # The configured value may be a folder path whose last segment is the
        # API id — the two disagree on this user's setup.
        tail = wanted.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        for row in rows:
            candidate = str(row.get("id") or "").strip().lower()
            if candidate in (wanted, tail) or candidate.rsplit("/", 1)[-1] == tail:
                return row
    for row in rows:
        if row.get("loaded"):
            return row
    return rows[0] if rows else None


def _discover_local_model(local):
    rows = _local_model_rows(local)
    row = _loaded_model_row(rows)
    if row:
        return str(row["id"]).strip()
    raise ValueError("Local model is not configured and the endpoint did not return a model from /models.")


def _local_context_length(local):
    """Tokens the endpoint will actually serve, or None if it doesn't say.

    This is the *loaded* window, not the model's native one — a model with a
    262K native context loaded at 4096 reports 4096 here, and 4096 is the number
    that governs whether a request survives.
    """
    key = (local.get("base_url") or "", local.get("model") or "")
    now = time.monotonic()
    with _local_context_lock:
        cached = _local_context_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    context = None
    try:
        row = _loaded_model_row(_local_model_rows(local), local.get("model"))
        for field in ("context_length", "max_context_length", "max_model_len", "n_ctx"):
            value = (row or {}).get(field)
            if isinstance(value, (int, float)) and value > 0:
                context = int(value)
                break
    except Exception:
        # An endpoint that won't describe itself is not a reason to refuse to
        # call it — fall back to the assumed window.
        context = None

    with _local_context_lock:
        _local_context_cache[key] = (now + LOCAL_CONTEXT_TTL, context)
    return context


def _local_root(local):
    """The server root, for the endpoints that sit outside /v1."""
    base = (local.get("base_url") or "").rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def local_status(local=None):
    """The live view of what the endpoint has loaded, or {} if it won't say.

    Richer than /models: it reports the quantization, offload and slot settings
    a reload has to preserve, and `requested_context_length` — 0 meaning nobody
    ever asked for a window, so the small one in force is a default rather than
    a hardware limit.
    """
    local = local or _local_ai_settings()
    try:
        data = _get_json(f"{_local_root(local)}/v1/status", _local_auth_headers(local), timeout=20)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_local_context_window(target, settings=None, n_parallel=1, local=None):
    """Reload the live model with a specific context window.

    Returns the status after the reload. The window it reports is the one that
    matters: Unsloth's fitter caps the request to what the hardware can hold, so
    what was asked for and what is being served can differ, and only the second
    is worth acting on.
    """
    local = local or _local_ai_settings(settings)
    status = local_status(local)
    model_path = str(status.get("model_identifier") or local.get("model") or "").strip()
    if not model_path:
        raise ValueError("No model is loaded, so there is nothing to reload. Load one in Unsloth Studio first.")
    target = int(target or LOCAL_CONTEXT_TARGET_DEFAULT)

    payload = {"model_path": model_path, "max_seq_length": target}
    for field in LOCAL_LOAD_PASSTHROUGH:
        value = status.get(field)
        if value not in (None, ""):
            payload[field] = value
    if n_parallel:
        # JSE serializes to one in-flight local request, so slots beyond the
        # first only divide the KV budget that the context window comes out of.
        payload["n_parallel"] = int(n_parallel)

    try:
        _post_json(
            f"{_local_root(local)}/v1/load",
            _local_auth_headers(local),
            payload,
            timeout=LOCAL_LOAD_TIMEOUT,
        )
    except LLMHTTPError as exc:
        if exc.status_code == 409:
            raise Exception(
                "Unsloth Studio is mid-generation, so the model cannot be reloaded right now. "
                "Finish or stop that request and try again."
            ) from exc
        raise Exception(f"Unsloth Studio refused the reload (HTTP {exc.status_code}): {str(exc.body)[:300]}") from exc

    with _local_context_lock:
        _local_context_cache.clear()
    return local_status(local)


_local_reload_attempted = set()


def _ensure_local_context(local, needed):
    """Grow the served window to `context_target` when a request will not fit.

    Attempted once per endpoint+target per process. A reload that comes back
    still too small is a hardware answer, not a transient one, and retrying it
    before every request would turn a slow run into an unusable one.
    """
    context = _local_context_length(local)
    target = local.get("context_target") or 0
    if not context or context >= needed or not local.get("auto_reload") or target <= context:
        return context

    key = (local.get("base_url") or "", target)
    if key in _local_reload_attempted:
        return context
    _local_reload_attempted.add(key)

    print(
        f"Local endpoint is serving {context} tokens but this request needs {needed}. "
        f"Reloading the model at {target} tokens — this takes as long as the weights take to load."
    )
    try:
        status = set_local_context_window(target, local=local)
    except Exception as exc:
        print(f"Could not reload the local model: {exc}")
        return _local_context_length(local)

    grown = status.get("context_length") or _local_context_length(local)
    print(f"Local endpoint reloaded; it is now serving {grown} tokens.")
    return grown


def _count_local_prompt_tokens(local, messages):
    """Exact prompt size from the server's own tokenizer, or None.

    Worth the round trip: it decides whether a request fits, and the character
    heuristic below can be out by a factor of two on repetitive text.
    """
    try:
        data = _post_json(
            f"{_local_root(local)}/v1/chat/count_tokens",
            _local_auth_headers(local),
            {"model": local.get("model") or "default", "messages": messages},
            timeout=30,
        )
    except Exception:
        return None
    tokens = (data or {}).get("input_tokens")
    return int(tokens) if isinstance(tokens, (int, float)) and tokens > 0 else None


def _estimate_prompt_tokens(messages):
    """Rough prompt size in tokens, biased to over-count.

    No tokenizer is available client-side, and pulling one in for a sanity check
    would be a heavy dependency for a number that only needs to be in the right
    neighbourhood.
    """
    chars = 0
    for message in messages or []:
        chars += len(str(message.get("content") or "")) + len(str(message.get("role") or "")) + 8
    return int(chars / LOCAL_CHARS_PER_TOKEN) + 8


def _fit_output_budget(max_tokens, context, prompt_tokens):
    """Shrink the output budget to what the loaded window can actually serve.

    Raises when the prompt alone leaves no usable room: JSE cannot make the
    window bigger, and a request that cannot fit is better reported than sent
    and silently cut in half.
    """
    if not context:
        return max_tokens
    room = context - prompt_tokens - LOCAL_CONTEXT_RESERVE
    if room < LOCAL_MIN_OUTPUT_TOKENS:
        raise Exception(
            f"The local endpoint is serving a {context}-token context window, but this request needs "
            f"about {prompt_tokens} tokens of prompt plus room to answer. Reload the model in Unsloth "
            f"Studio with a larger context (32768 or more) and try again."
        )
    return max(LOCAL_MIN_OUTPUT_TOKENS, min(max_tokens, room))


def _local_is_configured():
    local = _local_ai_settings()
    if not local["base_url"]:
        return False
    if local["model"]:
        return True
    try:
        return bool(_discover_local_model(local))
    except Exception:
        return False


def _report_truncation(data, json_mode, max_tokens, context):
    """Handle a generation the server cut short at the token limit.

    Truncated JSON is not a smaller answer, it is an unparseable one, and the
    JSON-repair path can only invent the missing half — so structured calls
    raise and let the caller retry or skip. Free text keeps what arrived, with a
    warning, because a shortened paragraph is still usable.
    """
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    detail = f"{completion} completion tokens" if completion else f"the {max_tokens}-token budget"
    window = ""
    if context and total and total >= context - LOCAL_CONTEXT_RESERVE:
        # The generation stopped at the window, not at the requested budget: the
        # loaded context is the real constraint.
        window = (
            f" The endpoint's loaded context window ({context} tokens) is the limit here — reload the "
            f"model in Unsloth Studio with a larger context."
        )
    message = f"The local endpoint truncated its response after {detail}.{window}"
    if json_mode:
        raise LLMTruncatedError(message + " A truncated JSON response cannot be used.")
    print(f"Warning: {message}")


@_serialized_local_call
def _call_unsloth(messages, temperature=0.2, max_tokens=2048, json_mode=False, settings=None):
    """Core local OpenAI-compatible chat-completions call with retry logic.

    json_mode=True requests OpenAI-compatible JSON response_format so the
    serving runtime (vLLM/llama.cpp/Ollama) constrains the model to valid JSON.
    """
    from .parsing import _strip_reasoning_blocks
    # Imported here rather than at module scope: _call_unsloth needs a
    # module that imports this one back.
    max_tokens = min(int(max_tokens or 2048), LOCAL_MAX_OUTPUT_TOKENS)
    local = _local_ai_settings(settings)
    if not local["model"]:
        local["model"] = _discover_local_model(local)
    headers = _local_auth_headers(local)
    # Size the request to the window the model was loaded with rather than to an
    # assumption about the model. Asking for more than fits does not fail — the
    # server truncates mid-answer — so the check has to happen here.
    prompt_tokens = _count_local_prompt_tokens(local, messages) or _estimate_prompt_tokens(messages)
    context = _ensure_local_context(local, prompt_tokens + max_tokens + LOCAL_CONTEXT_RESERVE)
    requested_tokens = max_tokens
    max_tokens = _fit_output_budget(max_tokens, context, prompt_tokens)
    if max_tokens < requested_tokens:
        print(
            f"Local endpoint context is {context} tokens; trimming this request's output budget "
            f"from {requested_tokens} to {max_tokens} (prompt ~{prompt_tokens} tokens)."
        )
    payload = {
        "model": local["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    json_response_formats = []
    if json_mode:
        # OpenAI-compatible servers disagree on the supported JSON mode. Older
        # runtimes accept json_object, while newer llama.cpp/LM Studio-style
        # endpoints may require json_schema (or explicitly allow only text).
        # Start with the least restrictive structured mode and negotiate only
        # when the endpoint rejects response_format itself.
        json_response_formats = [
            {"type": "json_object"},
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "jse_json_response",
                    "schema": {"type": "object", "additionalProperties": True},
                },
            },
            {"type": "text"},
        ]
        payload["response_format"] = json_response_formats[0]
        # Hint Qwen3 to skip its thinking mode for structured-output tasks.
        # The /no_think token is honoured by Qwen3 chat templates; servers that
        # ignore it simply pass the literal token through harmlessly.
        if messages and messages[-1].get("role") == "user":
            content = messages[-1].get("content", "")
            if "/no_think" not in content and "/think" not in content:
                messages = list(messages)
                messages[-1] = {**messages[-1], "content": f"{content}\n\n/no_think"}
                payload["messages"] = messages

    # Scaled to the output budget: a 16K-token generation cannot honestly finish
    # in the time a 512-token one needs, and abandoning it early is what puts a
    # second request on a server still working through the first.
    timeout = min(
        LOCAL_TIMEOUT_CEILING,
        LOCAL_TIMEOUT_FLOOR + (max_tokens / 1000.0) * LOCAL_TIMEOUT_SECONDS_PER_1K_TOKENS,
    )

    response_format_index = 0
    transient_attempt = 0
    while True:
        if concurrency.cancel_event.is_set():
            raise concurrency.OperationCancelledError("Operation cancelled.")

        try:
            data = _post_json(f"{local['base_url']}/chat/completions", headers, payload, timeout=timeout)
            choice = data["choices"][0]
            msg = choice["message"]
            text = (msg.get("content") or "").strip()
            if not text:
                # Thinking-mode models (qwythos, some Qwen3 configs) route all
                # output to reasoning_content; content is always empty string.
                text = (msg.get("reasoning_content") or "").strip()
            if choice.get("finish_reason") == "length":
                _report_truncation(data, json_mode, max_tokens, context)
            return _strip_reasoning_blocks(text)
        except LLMHTTPError as e:
            status_code = e.status_code
            response_format_rejected = (
                json_mode
                and status_code == 400
                and "response_format" in str(e.body or "").lower()
            )
            if response_format_rejected and response_format_index < len(json_response_formats) - 1:
                response_format_index += 1
                payload["response_format"] = json_response_formats[response_format_index]
                continue
            if status_code in (429, 503):
                if transient_attempt < UNSLOTH_MAX_RETRIES - 1:
                    transient_attempt += 1
                    delay = e.retry_after if e.retry_after else _backoff_delay(transient_attempt)
                    print(f"Rate limited / server busy. Retrying in {delay:g}s... (attempt {transient_attempt}/{UNSLOTH_MAX_RETRIES})")
                    _interruptible_sleep(delay)
                    continue
                else:
                    raise Exception(f"Local endpoint failed after {UNSLOTH_MAX_RETRIES} attempts (HTTP {status_code}).")
            elif status_code == 401:
                raise Exception("Local endpoint authentication failed. Check the Local API key in Settings.")
            else:
                raise Exception(f"Local endpoint HTTP error: {e}. Response: {e.body[:500]}")
        except TimeoutError:
            if transient_attempt < UNSLOTH_MAX_RETRIES - 1:
                transient_attempt += 1
                # The abandoned generation is still occupying the server. Wait
                # long enough for it to finish before adding another.
                delay = _backoff_delay(transient_attempt, base=LOCAL_TIMEOUT_COOLDOWN)
                print(f"Local endpoint timed out after {timeout:g}s. Cooling down {delay:g}s before retrying... "
                      f"(attempt {transient_attempt}/{UNSLOTH_MAX_RETRIES})")
                _interruptible_sleep(delay)
                continue
            else:
                raise Exception(
                    f"Local endpoint timed out after {UNSLOTH_MAX_RETRIES} attempts "
                    f"({timeout:g}s each). The endpoint may be overloaded, or the model too slow "
                    f"for a {max_tokens}-token response."
                )
        except LLMRequestError as e:
            if transient_attempt < UNSLOTH_MAX_RETRIES - 1:
                transient_attempt += 1
                delay = _backoff_delay(transient_attempt)
                print(f"Local endpoint request error: {e}. Retrying in {delay:g}s...")
                _interruptible_sleep(delay)
                continue
            else:
                raise Exception(f"Local endpoint request failed after {UNSLOTH_MAX_RETRIES} attempts: {e}")
        except (LLMTruncatedError, concurrency.OperationCancelledError):
            # Both already say exactly what happened; wrapping them as an
            # "unexpected error" only buries the explanation.
            raise
        except Exception as e:
            raise Exception(f"Unexpected error calling local endpoint: {e}")

    raise Exception("Local endpoint call failed unexpectedly.")


def _model_name(settings, provider):
    explicit = (settings or {}).get("doc_ai_model") or ""
    if explicit.strip():
        return explicit.strip()
    if provider == "chatgpt":
        return "gpt-4o"
    if provider == "claude":
        return (settings or {}).get("claude_model") or "claude-sonnet-4-6"
    if provider == "gemini":
        return (settings or {}).get("gemini_model") or "gemini-2.5-pro"
    if provider == "compat":
        return (settings or {}).get("compat_model") or ""
    return (settings or {}).get("local_model") or DEFAULT_LOCAL_MODEL


def _messages_to_text(messages):
    parts = []
    for message in messages:
        role = message.get("role", "user").upper()
        parts.append(f"{role}:\n{message.get('content', '')}")
    return "\n\n".join(parts)


@_serialized_llm_call
def _call_openai_compatible(base_url, api_key, model, messages, temperature=0.2, max_tokens=4096, json_mode=False, require_key=True):
    from .parsing import _strip_reasoning_blocks
    # Imported here rather than at module scope: _call_openai_compatible needs a
    # module that imports this one back.
    if require_key and not api_key:
        raise ValueError("OpenAI / ChatGPT API key is not configured in Settings.")
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # Free / self-hosted OpenAI-compatible endpoints may not require a key.
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _post_json(
        f"{str(base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
        headers,
        payload,
        timeout=180,
    )
    return _strip_reasoning_blocks(data["choices"][0]["message"]["content"])


@_serialized_llm_call
def _call_claude(api_key, model, messages, temperature=0.2, max_tokens=4096):
    from .parsing import _strip_reasoning_blocks
    # Imported here rather than at module scope: _call_claude needs a
    # module that imports this one back.
    if not api_key:
        raise ValueError("Claude API key is not configured in Settings.")
    system = "\n\n".join(message.get("content", "") for message in messages if message.get("role") == "system")
    user_messages = [
        {"role": "assistant" if message.get("role") == "assistant" else "user", "content": message.get("content", "")}
        for message in messages
        if message.get("role") != "system"
    ]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": user_messages,
    }
    if system:
        payload["system"] = system
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        payload,
        timeout=180,
    )
    return _strip_reasoning_blocks(
        "\n".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
    )


@_serialized_llm_call
def _call_gemini(api_key, model, messages, temperature=0.2, max_tokens=4096):
    from .parsing import _strip_reasoning_blocks
    # Imported here rather than at module scope: _call_gemini needs a
    # module that imports this one back.
    if not api_key:
        raise ValueError("Gemini API key is not configured in Settings.")
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    generation_config = {"temperature": temperature, "max_output_tokens": max_tokens}
    gemini_model = genai.GenerativeModel(model, generation_config=generation_config)
    response = gemini_model.generate_content(_messages_to_text(messages))
    return _strip_reasoning_blocks(response.text or "")


def _call_document_ai(settings, messages, temperature=0.2, max_tokens=4096, json_mode=False):
    provider = ((settings or {}).get("doc_ai_provider") or "local").lower()
    model = _model_name(settings, provider)
    # 16K matches the new Qwen3 hard ceiling in _call_unsloth.
    if provider == "local":
        return _call_unsloth(
            messages, temperature=temperature, max_tokens=min(max_tokens, 16384), json_mode=json_mode,
            settings=settings,
        ), f"Local ({model})"
    if provider == "chatgpt":
        return _call_openai_compatible(
            (settings or {}).get("openai_base_url") or "https://api.openai.com/v1",
            (settings or {}).get("openai_api_key") or "",
            model,
            messages,
            temperature,
            max_tokens,
            json_mode=json_mode,
        ), f"ChatGPT / OpenAI ({model})"
    if provider == "claude":
        # Claude has no response_format=json — the system-prompt contract handles it.
        return _call_claude(
            (settings or {}).get("claude_api_key") or "",
            model,
            messages,
            temperature,
            max_tokens,
        ), f"Claude ({model})"
    if provider == "gemini":
        return _call_gemini(
            (settings or {}).get("gemini_api_key") or "",
            model,
            messages,
            temperature,
            max_tokens,
        ), f"Gemini ({model})"
    if provider == "compat":
        if not model:
            raise ValueError("Set a model name for the free / OpenAI-compatible endpoint in Settings.")
        return _call_openai_compatible(
            (settings or {}).get("compat_base_url") or "",
            (settings or {}).get("compat_api_key") or "",
            model,
            messages,
            temperature,
            max_tokens,
            json_mode=json_mode,
            require_key=False,
        ), f"Free endpoint ({model})"
    raise ValueError(f"Unknown document AI provider: {provider}")


def _settings_for_ai_task(settings, provider_field):
    """Resolve one workflow's provider while retaining shared keys/models."""
    resolved = dict(settings or {})
    resolved["doc_ai_provider"] = (
        resolved.get(provider_field)
        or resolved.get("doc_ai_provider")
        or "local"
    ).lower()
    return resolved


def _scoring_settings():
    """Settings for the triage/scoring ("Job matching") workflow.

    Defaults to local so behaviour is unchanged unless the user opts in. The
    scoring_model field is an independent model override for this workflow (so
    triage can run, e.g., a cheaper/faster Gemini model than document work); a
    blank value falls back to the provider's default model.
    """
    try:
        base = db.get_app_settings()
    except Exception:
        base = {}
    resolved = dict(base)
    resolved["doc_ai_provider"] = (base.get("scoring_ai_provider") or "local").lower()
    resolved["doc_ai_model"] = str(base.get("scoring_model") or "").strip()
    return resolved


def _call_scoring_ai(messages, temperature=0.2, max_tokens=2048, json_mode=False):
    """Provider-aware call for triage/scoring/analysis. Routes through the
    selected Job-matching provider (local by default, or Gemini / a free
    OpenAI-compatible endpoint)."""
    text, _label = _call_document_ai(
        _scoring_settings(), messages,
        temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
    )
    return text


def list_models_for_provider(provider, settings=None):
    """Discover available model ids for a provider so the UI can offer a
    dropdown instead of free-text. Returns a sorted list; never raises (returns
    [] when credentials are missing or the endpoint is unreachable)."""
    provider = str(provider or "").lower()
    settings = settings or {}
    try:
        if provider == "gemini":
            api_key = settings.get("gemini_api_key") or ""
            if not api_key:
                return []
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            names = []
            for model in genai.list_models():
                methods = getattr(model, "supported_generation_methods", None) or []
                if "generateContent" in methods:
                    names.append(str(getattr(model, "name", "")).replace("models/", ""))
            return sorted({name for name in names if name})
        if provider == "claude":
            api_key = settings.get("claude_api_key") or ""
            if not api_key:
                return []
            data = _get_json(
                "https://api.anthropic.com/v1/models",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=15,
            )
            rows = data.get("data") if isinstance(data, dict) else None
            return sorted({str(row.get("id")) for row in (rows or []) if isinstance(row, dict) and row.get("id")})

        # OpenAI-compatible endpoints expose GET /models.
        if provider == "local":
            local = _local_ai_settings(settings)
            base_url, api_key = local["base_url"], local["api_key"]
        elif provider == "compat":
            base_url, api_key = (settings.get("compat_base_url") or ""), (settings.get("compat_api_key") or "")
        elif provider == "chatgpt":
            base_url, api_key = (settings.get("openai_base_url") or "https://api.openai.com/v1"), (settings.get("openai_api_key") or "")
        else:
            return []
        if not base_url:
            return []
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = _get_json(f"{base_url.rstrip('/')}/models", headers, timeout=15)
        rows = data.get("data") if isinstance(data, dict) else None
        return sorted({str(row.get("id")) for row in (rows or []) if isinstance(row, dict) and row.get("id")})
    except Exception:
        return []


def _analysis_worker_count():
    """Max simultaneous LLM requests (analysis_workers app setting).

    Sizes both the analysis worker pool and the shared LLM concurrency gate.
    Clamped to 1-8, default 1.

    The local endpoint is treated as single-slot: it returns HTTP 429 the
    moment a second request arrives, and there is no reliable signal that a
    given local runtime serves parallel requests. So whenever the matching
    workflow targets the local provider, concurrency is forced to 1 regardless
    of the stored setting. The setting only takes effect for hosted / free
    OpenAI-compatible matching providers, which comfortably run 4-8.

    This is NOT the only thing holding the local endpoint to one request. It is
    sized from the scoring provider but gates every provider, so with scoring on
    a hosted provider it opens several slots that a local-routed call (documents,
    research, memory) could otherwise use all at once. `_local_slot` is what
    actually keeps local single-file, and it spans processes as well as threads.
    """
    try:
        settings = db.get_app_settings()
    except Exception:
        settings = {}
    provider = str(settings.get("scoring_ai_provider") or "local").lower()
    if provider == "local":
        return 1
    try:
        value = int(str(settings.get("analysis_workers", "") or "").strip() or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(8, value))
