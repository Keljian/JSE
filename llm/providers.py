"""Provider transport: the LLM concurrency gate, HTTP helpers, and the call* family.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
import contextlib
import functools
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


class LLMHTTPError(Exception):
    def __init__(self, status_code, message, body=""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class LLMRequestError(Exception):
    pass


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
        raise LLMHTTPError(exc.code, f"{exc.code} {exc.reason}", raw) from exc
    except TimeoutError as exc:
        raise TimeoutError(str(exc)) from exc
    except (URLError, OSError, json.JSONDecodeError) as exc:
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
    return {
        "base_url": base_url,
        "api_key": str(settings.get("local_api_key") or UNSLOTH_API_KEY or "").strip(),
        "model": str(settings.get("local_model") or DEFAULT_LOCAL_MODEL or "").strip(),
    }


def _local_auth_headers(local):
    headers = {"Content-Type": "application/json"}
    if local.get("api_key"):
        headers["Authorization"] = f"Bearer {local['api_key']}"
    return headers


def _discover_local_model(local):
    data = _get_json(f"{local['base_url']}/models", _local_auth_headers(local), timeout=15)
    models = data.get("data") if isinstance(data, dict) else None
    if isinstance(models, list):
        for model in models:
            if isinstance(model, dict) and str(model.get("id") or "").strip():
                return str(model["id"]).strip()
    raise ValueError("Local model is not configured and the endpoint did not return a model from /models.")


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


@_serialized_llm_call
def _call_unsloth(messages, temperature=0.2, max_tokens=2048, json_mode=False, settings=None):
    """Core local OpenAI-compatible chat-completions call with retry logic.

    json_mode=True requests OpenAI-compatible JSON response_format so the
    serving runtime (vLLM/llama.cpp/Ollama) constrains the model to valid JSON.
    """
    from .parsing import _strip_reasoning_blocks
    # Imported here rather than at module scope: _call_unsloth needs a
    # module that imports this one back.
    # Qwen3 runs with a 32K context window. Cap output at 16K so there is
    # always headroom for the prompt; per-call budgets still control cost,
    # but evidence-anchored prompts can request more when it genuinely helps.
    max_tokens = min(int(max_tokens or 2048), 16384)
    local = _local_ai_settings(settings)
    if not local["model"]:
        local["model"] = _discover_local_model(local)
    headers = _local_auth_headers(local)
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

    response_format_index = 0
    transient_attempt = 0
    while True:
        if concurrency.cancel_event.is_set():
            raise concurrency.OperationCancelledError("Operation cancelled.")
        
        try:
            data = _post_json(f"{local['base_url']}/chat/completions", headers, payload, timeout=120)
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            if not text:
                # Thinking-mode models (qwythos, some Qwen3 configs) route all
                # output to reasoning_content; content is always empty string.
                text = (msg.get("reasoning_content") or "").strip()
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
                    delay = UNSLOTH_RETRY_DELAY * transient_attempt
                    print(f"Rate limited / server busy. Retrying in {delay}s... (attempt {transient_attempt}/{UNSLOTH_MAX_RETRIES})")
                    time.sleep(delay)
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
                print(f"Local endpoint timeout. Retrying in {UNSLOTH_RETRY_DELAY}s... (attempt {transient_attempt}/{UNSLOTH_MAX_RETRIES})")
                time.sleep(UNSLOTH_RETRY_DELAY)
                continue
            else:
                raise Exception(f"Local endpoint timed out after {UNSLOTH_MAX_RETRIES} attempts.")
        except LLMRequestError as e:
            if transient_attempt < UNSLOTH_MAX_RETRIES - 1:
                transient_attempt += 1
                print(f"Local endpoint request error: {e}. Retrying in {UNSLOTH_RETRY_DELAY}s...")
                time.sleep(UNSLOTH_RETRY_DELAY)
                continue
            else:
                raise Exception(f"Local endpoint request failed after {UNSLOTH_MAX_RETRIES} attempts: {e}")
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

    Sizes both the analysis worker pool and the shared LLM concurrency gate,
    so it is the single authority on how many requests hit the endpoint at
    once. Clamped to 1-8, default 1.

    The local endpoint is treated as single-slot: it returns HTTP 429 the
    moment a second request arrives, and there is no reliable signal that a
    given local runtime serves parallel requests. So whenever the matching
    workflow targets the local provider, concurrency is forced to 1 regardless
    of the stored setting. The setting only takes effect for hosted / free
    OpenAI-compatible matching providers, which comfortably run 4-8.
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
