"""LLM integration for job triage, analysis, research, and documents.

The default provider is a local OpenAI-compatible endpoint, with optional
OpenAI, Claude, and Gemini paths selected through Settings. Job
analysis runs through resume triage, permissive scoring, deep gatekeeping, and
structured intelligence extraction. Document generation uses the same evidence
discipline to produce either structured JSON for DOCX templates or a
markdown-first application kit.
"""
#   - review_application_kit                 - post-hoc strictness check.
#
# Candidate-memory architecture (see also app_logic.py / database_manager.py):
#   - extract_application_memory_fragments   - mines TYPED fragments from each
#     submitted (human-validated) application kit. Fragment types: capability,
#     domain, seniority, outcome, tool, preference. Each fragment carries
#     keywords (to activate it from a job ad), anti_keywords (where it must
#     NOT be used), confidence + confidence_reasoning, and a status of
#     'established' (repeated evidence) or 'emerging' (one stretch role,
#     reuse cautiously). Submitted applications are higher-signal than raw
#     scraped jobs because the user spent a real slot on them.
#   - align_memory_fragments_to_role         - scores a target role against
#     the fragment bank: which fragments activate, which capability gaps have
#     no fragment support, what angle the strongest activations recommend,
#     and which emerging fragments a stretch role justifies capturing.
#
# Out of scope for THIS module but needed for the full vision (DB / app_logic):
#   * Persisting fragments with outcome weighting (applied / interviewed /
#     rejected / liked / archived) so confidence can decay or strengthen.
#   * Re-mining fragments on a schedule as more applications accumulate.
#   * Wiring fragment_score from align_memory_fragments_to_role into the
#     scoring pipeline alongside (not replacing) match_score.
#   * Deriving search terms from the fragment bank rather than only the resume
#     when fragments exist (the system_prompt for derive_search_terms_from_resume
#     accepts a fragment-augmented context string today).
import json
import re
import hashlib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from config import MY_INFO
import concurrency
import time
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
# Relaxed June 2026 (was 65/50): too few roles were surviving triage into the
# IT lane. Keep these aligned with the triage prompt's KEEP RULE and with
# database_manager.AUTO_REJECT_THRESHOLD.
FULL_ANALYSIS_TRIAGE_THRESHOLD = 60
TRIAGE_KEEP_THRESHOLD = 45

print("Local LLM endpoint defaults loaded; configure the active endpoint in Settings.")


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


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)


def _strip_reasoning_blocks(text):
    """Remove Qwen3-style <think>...</think> reasoning blocks before JSON parsing.

    Qwen3 emits chain-of-thought inside <think> tags by default. If the block is
    truncated (no closing tag) we drop everything from the opening tag onward —
    otherwise the leading reasoning text destroys downstream JSON extraction.
    """
    if not text:
        return text
    cleaned = _THINK_BLOCK_RE.sub("", str(text))
    open_match = _OPEN_THINK_RE.search(cleaned)
    if open_match:
        cleaned = cleaned[:open_match.start()]
    return cleaned.strip()


_IMAGE_REF_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"<img\b[^>]*>\s*</img>"  # bare img tags
    r"|<img\b[^>]*src\s*=\s*['\"]([^'\"]*?)['\"][^>]*/?>|"  # img with src attribute
    r"\[image:\s*[^]]*\]"  # [image: ...] style references
    r"|\bimage\.png\b|\bimage\.jpg\b|\bimage\.jpeg\b|\bimage\.gif\b|\bimage\.webp\b|"  # bare image filenames
    r"(?:src|href)\s*[:=]\s*['\"]?[^'\"]*\.(?:png|jpg|jpeg|gif|webp|svg|bmp)['\"]?"  # src/href to image files
    r"|data:image/[a-z]+;base64,[A-Za-z0-9+/=]{50,}"  # base64 image data
    r")",
    re.MULTILINE,
)


def _strip_image_references(text):
    """Remove image references and base64 image data from text before LLM calls.

    Vision-capable local LLMs may interpret image filenames or data URLs as
    instructions to load local files, which fails and produces errors like
    'Cannot read image.png'. This strips those references so only the text
    content reaches the model.
    """
    if not text:
        return text
    return _IMAGE_REF_RE.sub(" [IMAGE REMOVED] ", str(text))


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


def _call_unsloth(messages, temperature=0.2, max_tokens=2048, json_mode=False, settings=None):
    """Core local OpenAI-compatible chat-completions call with retry logic.

    json_mode=True requests OpenAI-compatible JSON response_format so the
    serving runtime (vLLM/llama.cpp/Ollama) constrains the model to valid JSON.
    """
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
            return _strip_reasoning_blocks(data["choices"][0]["message"]["content"])
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


def _call_openai_compatible(base_url, api_key, model, messages, temperature=0.2, max_tokens=4096, json_mode=False, require_key=True):
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


def _call_claude(api_key, model, messages, temperature=0.2, max_tokens=4096):
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


def _call_gemini(api_key, model, messages, temperature=0.2, max_tokens=4096):
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


def _json_object_candidate(text):
    """Return the largest likely JSON object from a model response."""
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"```$", "", value).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return value[start:end + 1]


def _escape_control_chars_in_json_strings(value):
    """Escape raw newlines/tabs that some local models place inside JSON strings."""
    output = []
    in_string = False
    escaped = False
    for char in str(value or ""):
        if in_string:
            if escaped:
                output.append(char)
                escaped = False
                continue
            if char == "\\":
                output.append(char)
                escaped = True
                continue
            if char == '"':
                output.append(char)
                in_string = False
                continue
            if char == "\n":
                output.append("\\n")
                continue
            if char == "\r":
                continue
            if char == "\t":
                output.append("\\t")
                continue
            output.append(char)
        else:
            output.append(char)
            if char == '"':
                in_string = True
    return "".join(output)


def _extract_json(text):
    """Extract JSON object from LLM response text."""
    candidate = _json_object_candidate(text)
    if not candidate:
        return None
    for attempt in (candidate, _escape_control_chars_in_json_strings(candidate)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError as e:
            last_error = e
    print(f"JSON decode error: {last_error}\nResponse snippet: {candidate[:300]}...")
    return None


def _extract_json_list(text):
    """Extract JSON array from LLM response text."""
    match = re.search(r'(\[.*?\])', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}\nResponse snippet: {match.group(1)[:200]}...")
            return None
    return None


def _coerce_list(value):
    """Return a clean list whether the LLM supplied a list, string, or nothing."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]
    return []


def _normalise_memory_fragments(value):
    """Coerce plausible LLM fragment objects into the persisted fragment shape."""
    fragments = []
    allowed_types = {"capability", "domain", "seniority", "outcome", "tool", "preference"}
    allowed_seniority = {"individual", "lead", "manager", "executive", "unknown"}
    allowed_confidence = {"high", "medium", "low"}
    allowed_status = {"established", "emerging"}

    if not isinstance(value, list):
        return fragments

    for item in value:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        theme = str(item.get("theme") or "").strip()
        if not claim or not theme:
            continue

        fragment_type = str(item.get("fragment_type") or "capability").strip().lower()
        if fragment_type not in allowed_types:
            fragment_type = "capability"

        seniority = str(item.get("seniority") or "unknown").strip().lower()
        if seniority not in allowed_seniority:
            seniority = "unknown"

        confidence = str(item.get("confidence") or "medium").strip().lower()
        if confidence not in allowed_confidence:
            confidence = "medium"

        status = str(item.get("status") or "established").strip().lower()
        if status not in allowed_status:
            status = "established"

        supporting_detail = (
            item.get("supporting_detail")
            or item.get("evidence")
            or item.get("supporting_evidence")
            or ""
        )
        fragments.append({
            **item,
            "fragment_type": fragment_type,
            "theme": theme,
            "claim": claim,
            "supporting_detail": str(supporting_detail).strip(),
            "job_families": _coerce_list(item.get("job_families")),
            "keywords": _coerce_list(item.get("keywords")),
            "anti_keywords": _coerce_list(item.get("anti_keywords")),
            "seniority": seniority,
            "skills": _coerce_list(item.get("skills")),
            "domains": _coerce_list(item.get("domains")),
            "reuse_guidance": str(item.get("reuse_guidance") or "").strip(),
            "confidence": confidence,
            "confidence_reasoning": str(item.get("confidence_reasoning") or "").strip(),
            "status": status,
            "reinforces_fragment_themes": _coerce_list(item.get("reinforces_fragment_themes")),
        })
    return fragments


def _repair_json_via_llm(broken_text, settings=None, max_tokens=4000):
    """One-shot LLM repair pass for malformed JSON returned by the primary call.

    Used by any consumer that wants a second chance before falling back. Returns
    the parsed dict on success or None on failure. Cheap to call because the
    repair prompt is short and json_mode constrains the output.
    """
    if not broken_text:
        return None
    repair_messages = [
        {
            "role": "system",
            "content": (
                "You repair malformed JSON. Return ONLY one valid JSON object that preserves the original "
                "content. Escape internal newlines as \\n. Do not add commentary, do not add new fields, "
                "do not invent content. If a field is truncated, keep what is valid and close the JSON correctly."
            ),
        },
        {"role": "user", "content": str(broken_text)[:30000]},
    ]
    try:
        repaired, _ = _call_document_ai(
            settings or {}, repair_messages, temperature=0.0, max_tokens=max_tokens, json_mode=True
        )
        return _extract_json(repaired)
    except Exception:
        return None


def _fallback_job_intelligence(job):
    text = " ".join(str(job.get(key) or "") for key in ("title", "description", "pdf_text")).lower()
    title = str(job.get("title") or "")
    role_family = "other"
    family_terms = {
        "IT leadership": ["it manager", "technology manager", "infrastructure", "platform", "service delivery", "systems manager"],
        "engineering systems": ["engineering", "engineer", "automation", "mechatronics", "embedded", "cad", "bim"],
        "business analysis": ["business analyst", "business partner", "requirements", "process", "stakeholder"],
        "delivery": ["project manager", "program manager", "delivery lead", "transformation", "implementation"],
    }
    for family, terms in family_terms.items():
        if any(term in text or term in title.lower() for term in terms):
            role_family = family
            break
    seniority = "unknown"
    if re.search(r"\b(head|director|executive|chief)\b", text):
        seniority = "executive"
    elif re.search(r"\b(lead|manager|principal)\b", text):
        seniority = "lead"
    elif re.search(r"\b(senior|sr)\b", text):
        seniority = "senior"
    elif re.search(r"\b(junior|graduate|assistant)\b", text):
        seniority = "junior"
    skills = []
    for term in [
        "stakeholder", "vendor", "cloud", "azure", "automation", "governance", "security",
        "salesforce", "erp", "project management", "requirements", "cad", "bim", "embedded",
        "integration", "operations", "strategy",
    ]:
        if term in text:
            skills.append(term)
    work_mode = "unknown"
    if "remote" in text:
        work_mode = "remote"
    elif "hybrid" in text:
        work_mode = "hybrid"
    elif any(term in text for term in ("on site", "on-site", "onsite")):
        work_mode = "onsite"
    return {
        "role_family": role_family,
        "seniority": seniority,
        "core_skills": skills[:12],
        "domains": [],
        "responsibilities": [],
        "hard_requirements": [],
        "soft_requirements": [],
        "dealbreakers": [],
        "work_mode": work_mode,
        "employer_type_hint": "unknown",
        "confidence": "low",
        "fallback": True,
    }


def extract_job_intelligence(job, settings=None, log_callback=None):
    """Use the local model to extract compact structured job intelligence."""
    log = log_callback or (lambda _message: None)
    fallback = _fallback_job_intelligence(job)
    if (settings or {}).get("force_fallback") or not _local_is_configured():
        return fallback, "deterministic fallback"
    local_settings = {**(settings or {}), "doc_ai_provider": "local"}
    text = "\n\n".join([
        f"Title: {job.get('title') or ''}",
        f"Company: {job.get('company') or ''}",
        f"Location: {job.get('location') or ''}",
        str(job.get("description") or "")[:9000],
        str(job.get("pdf_text") or "")[:4000],
    ])
    messages = [
        {
            "role": "system",
            "content": (
                "You extract compact, structured job-routing intelligence from a single Australian job ad. "
                "Return ONLY one valid JSON object — no <think> tags, no markdown, no prose. "
                "Do NOT assess the candidate; this is purely about the role. "
                "Use only evidence from the supplied ad. "
                "If a field is genuinely unclear, return 'unknown' or an empty list — do not guess."
            ),
        },
        {
            "role": "user",
            "content": f"""Extract a compact JSON object with EXACTLY these keys (all present, even if empty):

{{
  "role_family": "IT leadership | engineering systems | business analysis | delivery | product | support | sales | other",
  "seniority": "junior | mid | senior | lead | executive | unknown",
  "core_skills":          [list of 4-10 capability phrases the ad emphasises — phrases, not single words],
  "domains":              [list of 0-5 sector/industry markers actually named in the ad],
  "responsibilities":     [list of 3-8 short verb-led duty lines from the ad],
  "hard_requirements":    [list of explicit must-haves: certifications, clearances, named tools, years of experience, eligibility],
  "soft_requirements":    [list of nice-to-haves explicitly framed as preferred/desirable],
  "dealbreakers":         [list of items framed as mandatory that filter candidates: clearance, on-site only, mandatory shift, citizenship, registration],
  "work_mode": "onsite | hybrid | remote | unknown",
  "employer_type_hint": "direct | recruiter | mixed | unknown",
  "confidence": "low | medium | high"
}}

HINTS
- "recruiter": ad written by an agency, no end-client name, or generic 'our client'.
- "direct": clear single employer named, application goes to the employer.
- confidence = "low" when the ad is short, vague, or recruiter-written with no end client.

JOB:
---
{text}
---""",
        },
    ]
    try:
        response, provider = _call_document_ai(
            local_settings, messages, temperature=0.05, max_tokens=2500, json_mode=True
        )
        data = _extract_json(response)
        if not data:
            log("Local job intelligence returned malformed JSON; using deterministic fallback.")
            return fallback, "deterministic fallback"
        merged = {**fallback, **data, "fallback": False}
        return merged, provider
    except Exception as exc:
        log(f"Local job intelligence failed; using deterministic fallback: {exc}")
        return fallback, "deterministic fallback"


def review_application_kit(application_payload, settings=None, log_callback=None):
    """Use the local model to review an application kit for quality and learning signals."""
    log = log_callback or (lambda _message: None)
    fallback = {
        "strongest_evidence_used": [],
        "missing_evidence": [],
        "overclaimed_risks": [],
        "alignment_score": 0,
        "recommended_manual_checks": ["Review generated documents manually before applying."],
        "fragments_to_strengthen": [],
        "fallback": True,
    }
    if (settings or {}).get("force_fallback") or not _local_is_configured():
        return fallback, "deterministic fallback"
    local_settings = {**(settings or {}), "doc_ai_provider": "local"}
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict reviewer of a generated application kit (tailored resume + cover letter) for one Australian role. "
                "Your job is to catch truthfulness risk, weak claims, and missed leverage — NOT to rewrite the documents. "
                "Return ONLY one valid JSON object. No <think> tags, no markdown, no prose. "
                "Australian English spelling. Be sceptical, not encouraging."
            ),
        },
        {
            "role": "user",
            "content": f"""Review the supplied application kit. Return JSON with EXACTLY this shape:

{{
  "alignment_score": int 0-100 (how well the kit hits the ad's named requirements, evidence-anchored),
  "strongest_evidence_used":  [3-6 specific resume artefacts the kit leaned on well — name role/employer/outcome],
  "missing_evidence":          [2-5 ad requirements the kit failed to evidence even though the resume could have],
  "overclaimed_risks":         [items in the resume/letter that go beyond what the base resume actually supports — flag any number, scale, or sector claim not in the source],
  "recommended_manual_checks": [2-4 things the candidate should verify before sending — specific facts, claims, or framing decisions],
  "fragments_to_strengthen":   [2-5 themes that, with a stronger reusable fragment in memory, would have made this kit sharper]
}}

SCORING GUIDE
- 90+: every named ad requirement has resume-anchored evidence; cover letter ties evidence to specific outcomes; no overclaim risk.
- 75-89: most requirements covered, 1-2 weak bridges, no overclaim risk.
- 60-74: meaningful gaps OR generic claims that could apply to many ads.
- <60: significant gap, generic positioning, or at least one overclaim risk.

APPLICATION KIT:
---
{json.dumps(application_payload, ensure_ascii=False)[:18000]}
---""",
        },
    ]
    try:
        response, provider = _call_document_ai(
            local_settings, messages, temperature=0.05, max_tokens=3000, json_mode=True
        )
        data = _extract_json(response)
        if not data:
            log("Local application review returned malformed JSON; using deterministic fallback.")
            return fallback, "deterministic fallback"
        return {**fallback, **data, "fallback": False}, provider
    except Exception as exc:
        log(f"Local application review failed; using deterministic fallback: {exc}")
        return fallback, "deterministic fallback"


def _bullet_section(title, values):
    items = _coerce_list(values)
    if not items:
        return f"{title}:\n- N/A"
    return f"{title}:\n- " + "\n- ".join(items)


# ---------------------------------------------------------------------------
# POSITIONING DOCTRINE (June 2026) — single source of truth for how the
# screening prompts understand the target market. Update HERE when strategy
# changes; the triage / analysis / gatekeeper prompts all append it.
# ---------------------------------------------------------------------------
POSITIONING_DOCTRINE = """CANDIDATE POSITIONING (authoritative — June 2026)
Single identity: a technology leader for businesses whose product is physical, technical or creative work (manufacturing, agtech, food production, energy, industrial services, design-led and professional practices). The builder-practitioner who creates structure and foundations where none exist, and works in the tools himself.

TRACK 1 — PRIMARY (~70% of application effort): senior technology leadership.
- Titles: Head of IT, Head of Digital & Technology, Head of Technology, ICT Manager, IT Manager, Technology Manager, IT Operations Manager, Digital & Technology Lead.
- Strongest environment: mid-sized operational, manufacturing, agtech, energy, or design-led businesses, especially where structure is being built (first senior technology hire, MSP-governed estates, growth or consolidation phase).
- Salary band: AUD $140k-$185k+. A Track 1-shaped title advertised clearly below ~$120k is a level-mismatch signal, not a bargain.
- Evidence anchors: Flavorite (built the IT function from scratch, MSP governance, quantified multi-million savings), EPSA (Salesforce CPQ delivery), Bosch (commercial and creative-environment fluency).

TRACK 2 — SECONDARY (~25% of effort): embedded / power electronics engineering.
- Titles: Embedded Systems Engineer, Electronics Engineer, Power Electronics Engineer, Firmware Engineer, Hardware Engineer, Mechatronics Engineer, Product Development Engineer.
- Score on what was built (down-hole monitoring hardware at Firetail; honours capstone hardware), never on headcount or generic leadership.
- Salary band: AUD $85k-$110k with an engineering trajectory.

RETIRED — never score up: coordinator, project officer, BA-only, administration, or university/council coordinator-grade roles scoped or priced materially below the resume's demonstrated leadership ceiling. These fight the market's own signal about the candidate's level; treat as adjacent at best.

TIMING & LIABILITIES
- Mechatronics honours degree completes December 2027 (part-time alongside consulting). A hard completed-engineering-degree gate before then is a knockout for Track 2 roles.
- The recent gap is a deliberate investment in the degree's heavy phase plus part-time consulting delivery — never treat it as unemployment.
- The degree is practitioner fluency and the credential for a long-run IT/OT-convergence CTO path; on Track 1 it supports the story, it never leads it.

Use this positioning to judge role-family fit, level fit, and application ROI. Never use it to invent or inflate resume facts."""

ANALYSIS_SYSTEM_PROMPT = """You are a senior Australian career analyst evaluating ONE resume against ONE job advertisement. Downstream tooling uses your JSON to decide whether to apply and how to tailor documents.

OUTPUT CONTRACT
- Return exactly ONE minified JSON object. Nothing before or after it. No markdown, no code fences, no commentary, no <think> tags.
- Every string must be valid JSON (escape internal newlines as \\n). Use Australian English spelling.
- Strings are displayed directly in a UI: plain prose only — no markdown syntax, no bullet characters, no numbering inside strings.
- Each array item carries ONE idea, ideally under 20 words, and leads with the concrete evidence (role, employer, project) before the implication.

EVIDENCE DISCIPLINE (non-negotiable)
1. Cite, do not invent. Every strength, weakness, and rationale must point to text in the resume or job ad. If you cannot point to evidence, do not write the claim.
2. Score for THIS role. Generic seniority is not enough — the resume must credibly cover what this specific ad asks the person to deliver.
3. No keyword bingo. Matching the words "stakeholder", "delivery", "cloud", "transformation", "manager", or "leadership" without level/scope evidence does NOT lift the score.
4. Recognise Australian employer context (ASX-listed corporates, Big 4, state and federal government, Defence, universities, councils, recruiters acting for an undisclosed end client). Recruiter ads with no end-client clue are inherently weaker.

SCORING RUBRIC (match_score 0-100, integer)
- 90-100 EXCEPTIONAL: The resume credibly covers the role's core outcomes with named evidence. Normal tailoring (not invention) will produce a competitive application. Reserve 95+ for resumes that ALSO explicitly evidence most named must-haves (tools, sectors, certifications, scale of team/budget).
- 80-89 STRONG: Clear evidence for most requirements with a few manageable gaps or terminology differences that tailoring can credibly bridge.
- 70-79 POSSIBLE: Enough overlap to justify applying, but at least one important gap (level, domain, named platform, or scale) needs careful positioning.
- 50-69 WEAK: Notable gap in level, function, or core capability. Adjacent at best.
- 0-49 POOR: Wrong level, wrong function, missing eligibility, or core requirements absent. Do not prioritise.

PERMISSIVE GUARD
- Do NOT cap below 90 solely because 1-2 named tools, sectors, or domain terms are absent when the resume credibly covers the role's outcomes and those gaps are addressable in tailoring.

RESTRICTIVE GUARDS (apply these caps before finalising)
- Cap at 78 if "cover_letter_angle" is generic ("transferable skills", "proven leader", "strong fit") or could be reused unchanged for a dozen other jobs.
- Cap at 78 if you cannot name at least 3 specific resume artefacts (role, employer, project, outcome) that map to specific ad requirements.
- Cap at 74 if the ad is a recruiter post with no end client, no concrete duty detail, and no salary band — there is too little signal to score higher honestly.
- Cap at 69 if the seniority signalled by the ad is materially above or below the resume's demonstrated ceiling.

FIT LEVEL MAPPING
- 90-100 -> "exceptional"; 80-89 -> "strong"; 70-79 -> "possible"; 50-69 -> "weak"; 0-49 -> "poor".

FRAGMENT ALIGNMENT (only when VALIDATED MEMORY FRAGMENTS are supplied)
- Keep match_score independent: it is the resume-vs-ad fit score only. Do not raise match_score because the fragment bank looks strong.
- fragment_score is a separate 0-100 reusable-evidence score: 80+ means several activated fragments cover core advertised outcomes; 60-79 means partial useful support; 1-59 means weak or narrow support; 0 means no useful fragment support.
- activated_fragments must name exact fragment themes/claims supplied in the prompt. Do not invent memory fragments.
- fragment_capability_gaps are advertised requirements with little or no support in the fragment bank, even if the raw resume may contain some evidence.
- If no VALIDATED MEMORY FRAGMENTS section is supplied, return fragment_score as null and the fragment arrays as empty.

WHEN match_score >= 75
- "high_fit_rationale" MUST name (a) the strongest 1-2 specific resume artefacts to lead with, (b) which advertised requirement(s) each covers, (c) the single biggest risk to neutralise in tailoring. Generic encouragement is rejected.

RECOMMENDED ACTION
- "Apply now": 85+ AND cover_letter_angle is role-specific AND no material uncertainty about level/eligibility.
- "Prepare targeted application": 75-84, or 85+ with at least one meaningful tailoring effort required.
- "Research before applying": 65-74, OR any score where the employer/end-client/level is materially unclear.
- "Reject/retire": below 50, or any score with a hard knockout (eligibility, level, function).

REQUIRED JSON SHAPE (every key present, even if empty)
{
  "match_score": int 0-100,
  "fit_level": "exceptional" | "strong" | "possible" | "weak" | "poor",
  "suitability_summary": "2-4 sentence direct assessment naming concrete evidence from the resume",
  "high_fit_rationale": "string — empty when match_score < 75",
  "strengths": ["3-6 role-specific strengths, each anchored to a resume artefact (role, employer, project, certification)"],
  "weaknesses": ["2-5 honest gaps or risks specific to THIS role"],
  "key_skills": ["5-10 skills/capabilities the role needs, ordered by importance for this ad"],
  "application_focus_points": ["3-6 specific tailoring actions (what to foreground, what to mirror, what to quantify)"],
  "resume_focus": ["3-6 resume-specific actions (which bullets to lift to the summary, which to reword, which to drop)"],
  "cover_letter_angle": "ONE specific narrative positioning for THIS role — must reference something concrete in the ad",
  "interview_focus": ["2-5 preparation priorities, each tied to an ad requirement or likely risk"],
  "recommended_action": "Apply now" | "Prepare targeted application" | "Research before applying" | "Reject/retire",
  "fragment_score": null or int 0-100,
  "activated_fragments": ["0-6 exact supplied fragment themes/claims that support this role"],
  "fragment_capability_gaps": ["0-5 important role requirements not well supported by supplied fragments"],
  "fragment_angle": "concise application angle from the strongest activated fragments, or empty string",
  "fragment_confidence": "none" | "low" | "medium" | "high"
}

EXAMPLE (content style only; required schema above is authoritative):
{"match_score":86,"fit_level":"strong","suitability_summary":"Strong fit. The resume's eight years leading Microsoft 365 and Azure platform teams at <employer> covers the ad's core platform-ownership outcomes. Public-sector procurement language is absent and should be added in tailoring.","high_fit_rationale":"Lead with the <employer> M365 tenant consolidation (covers 'cloud platform leadership') and the <project> ITSM rebuild (covers 'service management uplift'). Biggest risk to neutralise: no explicit Victorian government experience — frame the council program as comparable public-sector delivery.","strengths":["Led M365 consolidation across 4 business units at <employer>","Owned $2.1M annual platform budget","Direct line management of 11 engineers"],"weaknesses":["No explicit Victorian government tenure","ITIL v4 certification not stated"],"key_skills":["Cloud platform leadership","Service management","Vendor governance","Stakeholder management","Budget ownership","Team leadership","Cyber risk posture","Change advisory"],"application_focus_points":["Mirror the ad's 'platform owner' language in the summary","Quantify team, budget and tenant scale up front","Add a public-sector framing line"],"resume_focus":["Promote the M365 consolidation bullet into the summary","Reword 'managed vendors' as 'governed $1.4M in panel contracts'","Drop early helpdesk role detail to free space"],"cover_letter_angle":"Position as the platform owner who already ran a multi-business-unit M365 consolidation with the budget and team scale this Victorian government role expects.","interview_focus":["Walk through the M365 tenant consolidation decision tree","Prepare a public-sector procurement story"],"recommended_action":"Prepare targeted application"}""" + "\n\n" + POSITIONING_DOCTRINE

TRIAGE_SYSTEM_PROMPT = """You are a fast first-pass triage classifier for an Australian job-search pipeline. Your only job is to decide whether this role deserves expensive full analysis.

OUTPUT CONTRACT
- Return exactly ONE minified JSON object. Nothing before, after, or around it. No <think> tags, no markdown, no commentary.
- Australian English spelling. "reason" is plain prose (no markdown) and names the single dominant signal first.

DECISION PROCESS (apply in order)
1. ROLE FAMILY: TRACK 1 — senior IT / digital / technology leadership (Head of IT/Digital & Technology, ICT/IT/Technology Manager, IT Operations Manager) with platform, vendor, budget, or team ownership; strongest in mid-sized operational, manufacturing, agtech, energy, or design-led businesses where structure is being built. TRACK 2 — embedded / power electronics / mechatronics / firmware / product engineering where the resume's engineering evidence matches the ad's scope. Business systems / transformation / delivery / technical BA roles qualify ONLY at genuine senior-ownership level. If clearly outside (sales, marketing, finance, clinical, trades, legal, HR, customer support L1/L2), score <= 35. Coordinator / project-officer / BA-only / administration roles below senior level are a RETIRED track -> cap at 40.
2. SENIORITY: Is the level credible given a senior-leaning resume? Junior, graduate, intern, or coordinator roles -> cap at 40 (retired track). Executive C-suite roles the resume cannot evidence -> cap at 45.
3. SALARY/LEVEL SIGNAL: A Track 1-shaped title with an advertised band clearly below ~AUD $120k signals coordinator-level scope wearing a manager title -> cap at 55 unless the duties evidence genuine Head-of ownership.
4. ELIGIBILITY KNOCKOUTS: Mandatory clearances/citizenship/registrations/trade tickets/completed-degree gates that the resume cannot meet -> cap at 35.
5. EVIDENCE OVERLAP: With knockouts cleared, score on credible overlap with the role's core outcomes.

SCORING BANDS (match_score 0-100)
- 90-100: Credible high-fit. Resume covers the core outcomes; only normal tailoring needed.
- 80-89: Strong fit with manageable gaps.
- 60-79: Worth full analysis — adjacent senior with credible bridges.
- 45-59: Weak or uncertain. Keep in the pipeline, but not worth the full-analysis spend.
- 40-44: Poor/weak. Do not keep unless the ad has unusual strategic value.
- 0-39: Poor fit, wrong family, or hard knockout.

CALIBRATION GUARDS
- PERMISSIVE: Do NOT cap below 85 just because 1-2 named tools, platforms, or industries are absent — the full analysis stage will check those properly.
- STRICT: A vague recruiter ad with no concrete duties or end client caps at 70.
- STRICT: Generic title overlap without level evidence in the resume summary caps at 65.

KEEP RULE
- keep = true if match_score >= 45 AND no hard knockout fired.
- keep = false otherwise.

REQUIRED JSON SHAPE
{
  "match_score": int 0-100,
  "reason": "1-2 sentences naming the dominant signal (e.g. role family fit, level mismatch, eligibility knockout)",
  "keep": boolean
}

EXAMPLES (shape only)
{"match_score":72,"reason":"Adjacent program-delivery role with credible senior overlap; recruiter ad so end client is unclear.","keep":true}
{"match_score":28,"reason":"Clinical practice manager role outside target families; no transferable evidence in resume summary.","keep":false}""" + "\n\n" + POSITIONING_DOCTRINE

DEEP_GATEKEEPER_SYSTEM_PROMPT = """You are a strict Australian job-search gatekeeper for the candidate. Roles arriving at this stage already scored >=78 in a permissive first analysis. Your only job is to catch false positives before a real application slot is committed.

OUTPUT CONTRACT
- Return exactly ONE minified JSON object. Nothing before, after, or around it. No <think> tags, no markdown.
- Be sceptical, not encouraging. Reward concrete evidence; penalise vibes.
- Strings are plain prose for direct UI display — no markdown inside strings; keep list items under 20 words.
- "evidence_matches" items use EXACTLY the format "<resume artefact> -> <ad requirement>" with one "->" per item.

ASSUMPTIONS THAT BIAS YOU TOWARD A CAP
- Words like "IT", "cloud", "stakeholder", "project", "systems", "manager", "transformation", "delivery", "analyst", "support", "leadership" are noise unless the ad evidences real seniority, ownership, and role-family fit.
- A flattering full-analysis JSON is not evidence. Re-derive your view from the ad and resume.

TARGET ROLE FAMILIES (anything else is adjacent at best)
- TRACK 1: senior IT / digital / technology leadership (Head of IT/Digital & Technology, ICT/IT/Technology Manager) with budget, team, vendor, and platform ownership — strongest in mid-sized operational, manufacturing, agtech, energy, or design-led environments where structure is being built.
- Business systems, enterprise systems, transformation, service management ONLY with genuine senior delivery ownership.
- TRACK 2: mechatronics, embedded, power electronics, firmware, automation, or product engineering ONLY when the resume's engineering evidence is directly relevant to the ad's engineering scope.
- RETIRED (reject or treat as adjacent): coordinator, project officer, BA-only, or administration roles scoped or priced materially below the resume's demonstrated leadership ceiling — regardless of how attractive the employer is.

HARD REJECT OR CAP AT 49 (any one of these)
- Primarily helpdesk, service desk, desktop support, L1/L2 support, field tech, installation, generic support analyst, or hands-on break/fix.
- Pure software developer / full-stack / coding role without credible product, architecture, systems, or delivery ownership.
- Sales, account management, customer success, presales, or BD without technical delivery ownership.
- Junior coordinator / admin / graduate / clearly sub-target seniority.
- Mandatory shift / heavy on-call / unacceptable location or work mode stated explicitly.
- Mandatory credential, clearance, trade ticket, licence, or completed degree that the resume cannot evidence.

CAP AT 69
- Track 1-shaped title with an advertised salary clearly below ~AUD $120k and no evidence of genuine Head-of scope (level-mismatch signal).
- Vague recruiter ad with no identifiable employer/end client AND weak responsibility detail.
- "Manager" title but the duties listed are mainly IC support / admin.
- Keyword overlap exists but the ad shows weak platform, team, budget, stakeholder, delivery, or strategic ownership.
- Role family is adjacent but not a clear priority for the candidate today.

CAP AT 74
- Decision is "research_first". By definition this is not action-grade.

CAP AT 76
- Application angle is generic ("strong fit", "transferable skills", "proven leader") or could be reused unchanged for many similar jobs.
- Fewer than four specific evidence bridges between resume artefacts and named ad requirements.

CAP AT 78
- At least one material uncertainty remains in: seniority, employer/end client, salary band, domain, work mode, or whether the ownership is real.

ALLOW 80+ ONLY IF ALL OF THESE HOLD
- Role is unambiguously in a target family.
- Seniority and ownership are evidenced by duties, not inferred from a title.
- At least four named ad requirements map to specific, named resume artefacts.
- No hard knockout applies.
- Applying is a strong use of a real 45-90 minute application slot.
- decision == "apply_now", application_roi == "high".
- application_angle is crisp, role-specific, and could not be cut-pasted into another ad. If you cannot defend in one sentence why this is worth one of the candidate's real slots today, cap at 78.

NUMERIC INVARIANTS (the harness enforces these too — don't fight them)
- decision == "reject"        => gate_score <= 49
- decision == "research_first" => gate_score <= 74
- decision == "apply_now"      => gate_score >= 80
- decision == "apply_now" AND application_roi != "high" => cap at 78
- decision == "apply_now" AND generic application_angle  => cap at 76

REQUIRED JSON SHAPE
{
  "decision": "apply_now" | "research_first" | "reject",
  "gate_score": int 0-100,
  "confidence": "high" | "medium" | "low",
  "score_cap": null or int 0-100,
  "role_family": "short family label",
  "seniority_fit": "explicit assessment of level alignment",
  "application_roi": "high" | "medium" | "low",
  "application_angle": "one specific, non-generic sentence — must reference something concrete in the ad",
  "knockout_reasons": ["each item is one hard knockout that triggered (empty array if none)"],
  "false_positive_risks": ["specific patterns suggesting the first-pass score over-rated this role"],
  "evidence_matches": ["3-6 items: 'resume artefact -> ad requirement'"],
  "missing_or_weak_evidence": ["2-5 items naming what the ad asks for that the resume does not credibly provide"],
  "one_line_reason": "single sentence justifying the final decision and score"
}""" + "\n\n" + POSITIONING_DOCTRINE


def _format_gatekeeper_section(data, original_score, enforced_score=None):
    score = (max(0, min(100, int(enforced_score))) if enforced_score is not None
             else max(0, min(100, int(data.get("gate_score", original_score) or original_score))))
    decision = data.get("decision", "research_first")
    cap = data.get("score_cap")
    sections = [
        "Deep Gatekeeper Review:",
        f"- Decision: {decision}",
        f"- Gate Score: {score}%",
        f"- Original Full-Analysis Score: {original_score}%",
        f"- Score Cap Applied: {cap if cap is not None else 'None'}",
        f"- Confidence: {data.get('confidence', 'N/A')}",
        f"- Role Family: {data.get('role_family', 'N/A')}",
        f"- Seniority Fit: {data.get('seniority_fit', 'N/A')}",
        f"- Application ROI: {data.get('application_roi', 'N/A')}",
        f"- Application Angle: {data.get('application_angle', 'N/A')}",
        f"- Reason: {data.get('one_line_reason', 'N/A')}",
        "",
        _bullet_section("Gatekeeper Knockouts", data.get("knockout_reasons")),
        _bullet_section("False Positive Risks", data.get("false_positive_risks")),
        _bullet_section("Evidence Matches", data.get("evidence_matches")),
        _bullet_section("Missing / Weak Evidence", data.get("missing_or_weak_evidence")),
    ]
    return "\n".join(sections), score


def _run_deep_gatekeeper(resume_summary, resume_text, full_description, analysis_data, original_score, profile_id, log):
    preference_context = _analysis_preferences(profile_id)
    user_prompt = f"""Run a strict third-pass gatekeeper review.

Do not simply validate the prior score. Look for false positives and apply score caps aggressively.

COMPACT RESUME SUMMARY:
---
{resume_summary[:2200]}
---

PROFILE PREFERENCE WEIGHTING:
---
{preference_context}
---

FULL ANALYSIS JSON:
---
{json.dumps(analysis_data, ensure_ascii=False)[:4500]}
---

RESUME EXTRACT:
---
{resume_text[:9000]}
---

JOB DESCRIPTION:
---
{full_description[:10000]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": DEEP_GATEKEEPER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.05,
        max_tokens=4000,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        log(f"Deep gatekeeper response was not valid JSON. Keeping original score. Response: {response[:180]}...")
        return "", original_score

    gate_score = max(0, min(100, int(data.get("gate_score", original_score) or original_score)))
    decision = str(data.get("decision") or "").lower()
    cap = data.get("score_cap")
    if cap is not None:
        try:
            gate_score = min(gate_score, max(0, min(100, int(cap))))
        except (TypeError, ValueError):
            pass
    if decision == "reject":
        gate_score = min(gate_score, 49)
    elif decision == "research_first":
        gate_score = min(gate_score, 74)
    elif decision == "apply_now":
        gate_score = max(gate_score, 80)
        if str(data.get("application_roi") or "").lower() != "high":
            gate_score = min(gate_score, 78)
        angle = str(data.get("application_angle") or "").strip()
        generic_angle = not angle or len(angle) < 45 or any(
            phrase in angle.lower()
            for phrase in (
                "strong fit",
                "relevant experience",
                "transferable skills",
                "apply his experience",
                "technology leader",
            )
        )
        if generic_angle:
            gate_score = min(gate_score, 76)
    final_score = min(original_score, gate_score)
    # Format only after every decision invariant and cap has been applied, so
    # the visible Gate Score is the same number persisted as match_score.
    gate_section, _ = _format_gatekeeper_section(data, original_score, final_score)
    log(f"Deep gatekeeper: {decision or 'unknown'} at {final_score}% for originally {original_score}%.")
    return gate_section, final_score


def _format_analysis_text(data):
    score = int(data.get("match_score", 0) or 0)
    score = max(0, min(100, score))
    fit_level = data.get("fit_level", "N/A")
    summary = data.get("suitability_summary", "N/A")
    high_fit = data.get("high_fit_rationale", "")
    cover_letter_angle = data.get("cover_letter_angle", "N/A")
    recommended_action = data.get("recommended_action", "N/A")

    sections = [
        f"Match Score: {score}%",
        f"Fit Level: {fit_level}",
        f"Recommended Action: {recommended_action}",
        f"Suitability Summary:\n{summary}",
    ]
    if high_fit:
        sections.append(f"High-Fit Rationale:\n{high_fit}")
    sections.extend([
        _bullet_section("Strengths", data.get("strengths")),
        _bullet_section("Weaknesses / Risks", data.get("weaknesses")),
        _bullet_section("Key Skills Required", data.get("key_skills")),
        _bullet_section("Application Focus Points", data.get("application_focus_points")),
        _bullet_section("Resume Focus", data.get("resume_focus")),
        f"Cover Letter Angle:\n{cover_letter_angle}",
        _bullet_section("Interview Focus", data.get("interview_focus")),
    ])
    fragment_score = _coerce_fragment_score(data.get("fragment_score"))
    if fragment_score is not None:
        fragment_confidence = data.get("fragment_confidence", "N/A")
        fragment_angle = data.get("fragment_angle", "")
        sections.extend([
            "Fragment Alignment:",
            f"- Fragment Score: {fragment_score}%",
            f"- Confidence: {fragment_confidence}",
            _bullet_section("Activated Fragments", data.get("activated_fragments")),
            _bullet_section("Fragment Capability Gaps", data.get("fragment_capability_gaps")),
        ])
        if fragment_angle:
            sections.append(f"Fragment Angle:\n{fragment_angle}")
    return "\n\n".join(sections), score


def _coerce_fragment_score(value):
    if value in (None, ""):
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _analysis_fragment_alignment(data, has_fragment_context):
    """Extract fragment alignment from the full-analysis JSON."""
    if not has_fragment_context:
        return None, None
    fragment_score = _coerce_fragment_score(data.get("fragment_score"))
    if fragment_score is None:
        return None, None
    alignment = {
        "source": "full_analysis",
        "fragment_score": fragment_score,
        "activated_fragments": _coerce_list(data.get("activated_fragments")),
        "capability_gaps": _coerce_list(data.get("fragment_capability_gaps")),
        "angle_recommendation": str(data.get("fragment_angle") or "").strip(),
        "confidence": str(data.get("fragment_confidence") or "").strip() or "unknown",
    }
    return fragment_score, json.dumps(alignment, ensure_ascii=False, separators=(",", ":"))


def _resume_hash(resume_text):
    return hashlib.sha256(str(resume_text or "").encode("utf-8", errors="replace")).hexdigest()


def _get_resume_triage_summary(resume_text, profile_id, log):
    resume_hash = _resume_hash(resume_text)
    cached = db.get_resume_triage_cache(profile_id, resume_hash)
    if cached:
        return cached

    log("Creating compact resume triage cache...")
    prompt = f"""Summarise this Australian candidate's resume for fast first-pass job-fit triage. Plain text only, no markdown, no <think> tags. Australian English spelling. Maximum 300 words.

Structure the summary as labelled lines so the downstream triage prompt can scan it cheaply:

TARGET ROLE FAMILIES: comma-separated families the resume credibly supports (e.g. "Senior IT leadership, Business systems, Delivery / project").
SENIORITY CEILING: highest level credibly evidenced (e.g. "senior manager / head-of, but not C-suite").
STRONGEST SKILLS: 5-8 capability phrases (not single words) ordered by evidence weight.
DOMAIN STRENGTHS: sectors with named tenure (utilities, councils, higher education, manufacturing, etc.).
TRANSFERABLE ADJACENT ROLES: 3-5 adjacent role families where the resume credibly stretches.
CLEAR NON-FIT FAMILIES: 2-4 role families this resume does NOT credibly serve (e.g. "Pure software engineering, Clinical, Sales/BD, Junior support").
RECENT ANCHORS: 2-3 specific recent role/employer/outcome anchors a triage pass can name as evidence.

Use only facts present in the resume. Do not invent.

RESUME:
---
{resume_text[:12000]}
---"""
    summary = _call_scoring_ai(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.15,
        max_tokens=1000,
    ).strip()
    db.save_resume_triage_cache(profile_id, resume_hash, summary)
    return summary


def _triage_job(resume_summary, full_description, log):
    user_prompt = f"""Estimate job fit for first-pass triage.

COMPACT RESUME SUMMARY:
---
{resume_summary[:1800]}
---

JOB EXTRACT:
---
{full_description[:3500]}
---"""
    response = _call_scoring_ai(
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.05,
        max_tokens=768,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        log(f"Triage response was not valid JSON; sending to full analysis. Response: {response[:180]}...")
        return 100, "Triage failed open.", True
    score = max(0, min(100, int(data.get("match_score", 0) or 0)))
    return score, data.get("reason", "No triage reason supplied."), bool(data.get("keep", score >= TRIAGE_KEEP_THRESHOLD))


def _lane_title_overlap(title, lane_target_text):
    """Token overlap between a job title and the lane's stated targets.

    Used by the borderline-rescue path: a single noisy triage number should not
    kill a role whose title plainly matches what the lane is hunting for."""
    stop = {"and", "the", "for", "of", "a", "an", "to", "in"}
    tokenize = lambda value: {
        token for token in re.findall(r"[a-z0-9]{2,}", str(value or "").lower())
        if token not in stop
    }
    return len(tokenize(title) & tokenize(lane_target_text))


def _analysis_preferences(profile_id):
    settings = db.get_lane_settings(profile_id)
    boost_terms = settings.get("boost_terms") or ""
    penalty_terms = settings.get("penalty_terms") or ""
    if not boost_terms and not penalty_terms:
        return "No extra lane weighting terms have been set."
    return (
        "Extra lane weighting terms:\n"
        f"- Add weight when present: {boost_terms or 'None'}\n"
        f"- Subtract weight when present: {penalty_terms or 'None'}\n"
        "Treat these as preference signals, not absolute rules. Mention any strong effect in the rationale."
    )


def _apply_preference_weight(score, text, profile_id):
    settings = db.get_lane_settings(profile_id)
    haystack = str(text or "").lower()
    boost_hits = [term for term in _coerce_list((settings.get("boost_terms") or "").replace(",", "\n")) if term.lower() in haystack]
    penalty_hits = [term for term in _coerce_list((settings.get("penalty_terms") or "").replace(",", "\n")) if term.lower() in haystack]
    adjusted = score + min(10, 3 * len(boost_hits)) - min(15, 5 * len(penalty_hits))
    return max(0, min(100, adjusted)), boost_hits, penalty_hits


def check_job_relevance(job_description: str, resume_text: str, log_callback=None):
    """Check if a job is relevant to the candidate's resume using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()

    log = log_callback or print

    if not job_description or not resume_text:
        log("Error: Missing job description or resume text for relevance check.")
        return False

    if not _local_is_configured():
        log("ERROR: Local LLM endpoint is not configured for job relevance check.")
        return False

    system_prompt = """You are a fast Australian career-fit relevance gate. One resume, one job ad. Decide if this role is worth analysing.

OUTPUT CONTRACT
- Return exactly ONE minified JSON object. Nothing before or after. No <think> tags, no markdown.
- Use ONLY evidence from the supplied resume and job ad. Do not invent.
- Australian English spelling.

DECISION RULES
- "relevant" = plausibly worth full analysis. Includes credible step-ups and adjacent senior roles.
- "not relevant" = wrong level (junior/graduate or executive C-suite the resume cannot evidence), wrong function (sales, clinical, trades, etc.), missing mandatory eligibility (clearance, registration, citizenship), or no credible skill overlap.
- A vague recruiter ad with weak signal but plausible family fit -> relevant with low confidence.

REQUIRED JSON SHAPE
{
  "is_relevant": boolean,
  "confidence": int 0-100,
  "fit_level": "exceptional" | "strong" | "possible" | "weak" | "poor",
  "reason": "one sentence naming the dominant signal",
  "strengths": ["1-3 concise role-specific strengths grounded in resume evidence"],
  "weaknesses": ["1-2 concise gaps or risks specific to this role"],
  "application_focus": ["1-3 tailoring actions for this role"]
}

EXAMPLE
{"is_relevant":true,"confidence":78,"fit_level":"strong","reason":"Senior IT leadership role with credible cloud/platform overlap.","strengths":["Platform leadership at <employer>","Vendor governance"],"weaknesses":["No explicit Victorian government tenure"],"application_focus":["Lead with platform consolidation outcomes","Add a public-sector framing line"]}"""

    user_prompt = f"""Decide whether this role is worth full analysis given the candidate's resume.

CANDIDATE RESUME:
---
{resume_text[:11000]}
---

JOB ADVERTISEMENT:
---
{job_description[:6000]}
---"""

    try:
        llm_response_text = _call_scoring_ai(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1200,
            json_mode=True,
        )

        data = _extract_json(llm_response_text)
        if data:
            is_relevant = data.get("is_relevant", False)
            confidence = data.get("confidence", 0)
            fit_level = data.get("fit_level", "unknown")
            reason = data.get("reason", "No reason provided")
            strengths = "; ".join(_coerce_list(data.get("strengths"))[:3])
            weaknesses = "; ".join(_coerce_list(data.get("weaknesses"))[:2])
            focus = "; ".join(_coerce_list(data.get("application_focus"))[:3])
            detail = f"Relevance check - Relevant: {is_relevant}, Confidence: {confidence}%, Fit: {fit_level}, Reason: {reason}"
            if strengths:
                detail += f", Strengths: {strengths}"
            if weaknesses:
                detail += f", Risks: {weaknesses}"
            if focus:
                detail += f", Focus: {focus}"
            log(detail)
            return is_relevant
        else:
            log(f"Could not find JSON in LLM response for relevance check. Response: {llm_response_text[:200]}...")
            return False

    except Exception as e:
        log(f"Error in job relevance check: {e}")
        return False


def generalize_search_term(failed_term: str, resume_text: str):
    """Generate a more general search term using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()

    if not _local_is_configured():
        print("ERROR: Local LLM endpoint is not configured for generalize_search_term.")
        return failed_term

    system_prompt = """You broaden a failed Australian job-board search term to a more general, higher-recall alternative.

Return ONLY one minified JSON object with a single key "new_term". No <think> tags, no prose.

RULES
- Output one canonical job title that Seek and LinkedIn actually use (e.g. "IT Manager", "Business Systems Analyst", "Project Manager").
- Stay in the same seniority band as the original term.
- Drop the most-specialised qualifier first (sector, tool, sub-discipline) before dropping seniority.
- Do NOT return the original term unchanged.
- Do NOT include locations, salary, or qualifiers like "experienced", "senior" (unless the original had it).

EXAMPLE
{"new_term":"Senior Technology Manager"}"""
    user_prompt = (
        f"The job search for '{failed_term}' returned zero results. Based on the resume excerpt, "
        f"return ONE broader job title to retry.\n\nRESUME EXCERPT:\n{resume_text[:2500]}"
    )

    try:
        llm_response_text = _call_scoring_ai(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=300,
            json_mode=True,
        )

        data = _extract_json(llm_response_text)
        if data:
            new_term = data.get("new_term", failed_term).strip()
            if not new_term or new_term.lower() == failed_term.lower():
                return failed_term
            return new_term
        else:
            print(f"Could not find JSON object in LLM response for generalization.")
            return failed_term

    except Exception as e:
        print(f"Error getting generalized search term from local endpoint: {e}")
        return failed_term


def derive_search_terms_from_resume(optimism_level: int, resume_text: str):
    """Generate search terms using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()
    if not resume_text:
        raise ValueError("Resume text cannot be empty.")

    if not _local_is_configured():
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")

    if optimism_level <= 1:
        level_description = "3-4 direct, conservative title matches"
        spread = "direct matches only"
    elif optimism_level == 2:
        level_description = "4-5 titles: direct matches + realistic step-up"
        spread = "direct + realistic step-up"
    elif optimism_level == 3:
        level_description = "5-6 titles: direct + step-up + adjacent senior"
        spread = "direct + step-up + adjacent"
    elif optimism_level == 4:
        level_description = "6-8 titles: direct + step-up + adjacent + selective reach"
        spread = "direct + step-up + adjacent + selective reach"
    else:
        level_description = "8-10 titles: direct + step-up + adjacent + ambitious-but-credible reach"
        spread = "full spread including ambitious reach"

    system_prompt = """You generate Australian job-board search titles. Return ONLY a JSON array of strings — nothing else, no <think> tags, no commentary.

RULES
- Each string is a canonical job title that Seek and LinkedIn keyword search will match (e.g. "IT Manager", "Senior Business Analyst", "Technology Operations Manager", "Digital Delivery Lead").
- Titles only — no locations, salaries, qualifiers like "experienced", boolean operators, or markdown.
- No near-duplicates ("IT Manager" and "Manager IT" are the same query).
- Order from most to least likely to surface a fit.
- Use Australian title conventions (e.g. "Programme Manager" or "Program Manager" — match the spelling the user's market actually uses).

TRACK ANCHORS
- When the resume/lane context signals senior technology leadership, anchor on: "Head of IT", "Head of Digital and Technology", "Head of Technology", "IT Manager", "ICT Manager", "Technology Manager", "IT Operations Manager".
- When it signals embedded/electronics engineering, anchor on: "Embedded Systems Engineer", "Electronics Engineer", "Power Electronics Engineer", "Firmware Engineer", "Mechatronics Engineer", "Product Development Engineer".
- NEVER generate coordinator, project officer, helpdesk, service desk, support analyst, or graduate titles — these are retired tracks."""
    user_prompt = (
        f"Generate {level_description}. Spread: {spread}.\n"
        "Return a JSON array of strings only.\n\n"
        f"RESUME / LANE CONTEXT:\n---\n{resume_text}\n---"
    )

    llm_response_text = _call_scoring_ai(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=1500,
    )
    return llm_response_text


def _format_fragment_context(fragments):
    """Render up to ~25 fragments as compact bullets for the analysis prompt.

    The analysis prompt then has the option of leaning on prior validated
    fragments as additional evidence, or noting capability gaps the fragment
    bank does not cover.
    """
    if not fragments:
        return ""
    lines = []
    for frag in fragments[:25]:
        theme = str(frag.get("theme") or "").strip()
        claim = str(frag.get("claim") or "").strip()
        conf = str(frag.get("confidence") or "").strip()
        status = str(frag.get("status") or "").strip()
        ftype = str(frag.get("fragment_type") or "").strip()
        keywords = frag.get("keywords") or []
        if isinstance(keywords, list):
            kw = ", ".join(str(k) for k in keywords[:6])
        else:
            kw = str(keywords)
        suffix_bits = [bit for bit in (ftype, conf, status) if bit]
        suffix = f" [{' / '.join(suffix_bits)}]" if suffix_bits else ""
        line = f"- {theme}: {claim}{suffix}"
        if kw:
            line += f" — activates on: {kw}"
        lines.append(line)
    return "\n".join(lines)


def _compose_score(match_score, fragment_score, match_weight=0.80, fragment_weight=0.20):
    """Blend the resume-vs-ad match score with the fragment-bank alignment score.

    Tuning rationale: match_score still dominates because it grounds in current
    truth (this resume vs this ad), while the fragment bank remains a secondary
    long-memory signal about which capabilities carry prior applications.
    """
    if fragment_score is None:
        return int(round(match_score))
    return int(round(match_weight * float(match_score) + fragment_weight * float(fragment_score)))


def _maybe_align_fragments(job_id, score, full_description_for_analysis, profile_id, log):
    """Compute fragment_score + alignment_json for a job that's worth the spend.

    Skips below-threshold jobs (no point spending an extra LLM call to refine
    a rejection) and skips when the lane has no fragment bank yet (graceful
    degradation — composite_score falls back to match_score).
    """
    if score < 65:
        return None, None, None
    try:
        fragments = [dict(row) for row in db.get_lane_fragments(profile_id, limit=120)]
    except Exception as exc:
        log(f"Could not load lane fragments for composite scoring: {exc}")
        return None, None, None
    if not fragments:
        return None, None, None
    role_payload = {
        "job_id": job_id,
        "description": str(full_description_for_analysis or "")[:9000],
    }
    try:
        alignment, _provider = align_memory_fragments_to_role(role_payload, fragments, log_callback=log)
    except Exception as exc:
        log(f"Fragment alignment skipped for job {job_id}: {exc}")
        return None, None, None
    try:
        fragment_score = int(round(float(alignment.get("fragment_score") or 0)))
    except (TypeError, ValueError):
        fragment_score = 0
    fragment_score = max(0, min(100, fragment_score))
    alignment_json = json.dumps(alignment, ensure_ascii=False, separators=(",", ":"))
    return fragment_score, alignment_json, alignment


def _perform_analysis_loop(jobs_to_analyze, resume_text, system_prompt, log_callback, profile_id=1, fragments=None):
    """A shared helper function to run the core analysis loop.

    When `fragments` is supplied (a list of fragment dicts from the memory
    bank), the analysis prompt includes them as additional evidence so the
    model can lean on validated reusable claims, not just the raw resume.
    If not supplied, composite scoring falls back to match_score. Fragment
    alignment is normally read from the full-analysis JSON to avoid a second
    LLM call per job.
    """
    log = log_callback or print
    resume_summary = _get_resume_triage_summary(resume_text, profile_id, log)
    preference_context = _analysis_preferences(profile_id)
    lane_settings = db.get_lane_settings(profile_id)
    lane_target_text = " ".join([
        lane_settings.get("target_titles") or "",
        lane_settings.get("lane_intent") or "",
    ])
    if fragments is None:
        try:
            fragments = [dict(row) for row in db.get_lane_fragments(profile_id, limit=40)]
        except Exception:
            fragments = []
    fragment_context = _format_fragment_context(fragments)

    for job in jobs_to_analyze:
        job_id, description, pdf_text = job['id'], job['description'], job['pdf_text']
        position_description_text = job["position_description_text"] if "position_description_text" in job.keys() else ""
        if concurrency.cancel_event.is_set():
            log("Analysis cancelled by user.")
            raise concurrency.OperationCancelledError("Analysis cancelled by user.")
        concurrency.paused.wait()

        full_description_for_analysis = _strip_image_references(description or "")
        if position_description_text:
            full_description_for_analysis = (
                f"--- UPLOADED POSITION DESCRIPTION ---\n{_strip_image_references(position_description_text)}\n\n"
                f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_description_for_analysis}"
            )
        if pdf_text:
            full_description_for_analysis += f"\n\n--- ADDITIONAL TEXT FROM PDF ---\n{_strip_image_references(pdf_text)}"
        analysis_signature = db.make_analysis_signature(resume_text, description, pdf_text, position_description_text)

        try:
            triage_score, triage_reason, keep = _triage_job(
                f"{resume_summary}\n\n{preference_context}",
                full_description_for_analysis,
                log,
            )
            triage_score, boost_hits, penalty_hits = _apply_preference_weight(triage_score, full_description_for_analysis, profile_id)
            if boost_hits or penalty_hits:
                triage_reason += f" Preference flags: +{', '.join(boost_hits) or 'none'}; -{', '.join(penalty_hits) or 'none'}."
            if not keep:
                triage_score = min(triage_score, TRIAGE_KEEP_THRESHOLD - 1)
                triage_reason += " Triage keep=false; treating as below keep threshold."
            log(f"Triage for job ID {job_id}: {triage_score}% - {triage_reason}")
            rescued = False
            if triage_score < FULL_ANALYSIS_TRIAGE_THRESHOLD:
                # Borderline rescue: one noisy triage number must not kill a
                # role whose title plainly matches the lane's stated targets.
                # Those get the evidence-anchored full analysis instead — the
                # only stage equipped to promote as well as demote.
                job_title = job["title"] if "title" in job.keys() else ""
                rescued = (
                    keep
                    and triage_score >= TRIAGE_KEEP_THRESHOLD
                    and _lane_title_overlap(job_title, lane_target_text) >= 2
                )
                if rescued:
                    log(
                        f"Borderline rescue for job ID {job_id}: triage {triage_score}% but title "
                        f"'{job_title}' matches lane targets. Escalating to full analysis."
                    )
            if triage_score < FULL_ANALYSIS_TRIAGE_THRESHOLD and not rescued:
                analysis_text = (
                    f"Triage Match Score: {triage_score}%\n\n"
                    f"Triage Result:\n{triage_reason}\n\n"
                    f"Full analysis skipped because the first-pass score was below {FULL_ANALYSIS_TRIAGE_THRESHOLD}%."
                )
                db.update_job_analysis(job_id, analysis_text, triage_score, analysis_signature)
                try:
                    db.update_job_fragment_alignment(job_id, None, _compose_score(triage_score, None), None)
                except Exception as exc:
                    log(f"Composite score persist skipped for job {job_id}: {exc}")
                continue
        except Exception as e:
            if concurrency.cancel_event.is_set():
                raise
            log(f"Triage failed for job ID {job_id}; falling back to full analysis: {e}")

        fragment_block = (
            f"\n\nVALIDATED MEMORY FRAGMENTS (reusable claims with prior evidence — lean on these where the job activates them):\n"
            f"---\n{fragment_context}\n---"
            if fragment_context else ""
        )
        user_prompt = f"""Analyse this Australian job advertisement against the candidate's resume. Return the required JSON only.

CANDIDATE RESUME:
---
{resume_text[:12000]}
---

PROFILE PREFERENCE WEIGHTING:
---
{preference_context}
---{fragment_block}

JOB ADVERTISEMENT:
---
{full_description_for_analysis[:9000]}
---"""
        score = 0
        analysis_text = "Analysis failed."
        json_string = ""
        llm_response_text = ""

        try:
            log(f"Analyzing job ID {job_id}...")
            llm_response_text = _call_scoring_ai(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.15,
                max_tokens=6000,
                json_mode=True,
            )

            data = _extract_json(llm_response_text)
            if data:
                json_string = llm_response_text
                analysis_text, score = _format_analysis_text(data)
                if score >= 78:
                    log(f"Running deep gatekeeper for job ID {job_id} ({score}%).")
                    gatekeeper_text, gated_score = _run_deep_gatekeeper(
                        resume_summary,
                        resume_text,
                        full_description_for_analysis,
                        data,
                        score,
                        profile_id,
                        log,
                    )
                    if gatekeeper_text:
                        analysis_text = f"{analysis_text}\n\n{gatekeeper_text}"
                        score = gated_score
                        analysis_text = re.sub(
                            r"^Match Score:\s*\d+%",
                            f"Match Score: {score}%",
                            analysis_text,
                            count=1,
                        )
            else:
                log(f"Could not find JSON in response for job ID {job_id}.")
                analysis_text = "Failed to find JSON in response.\n\n" + llm_response_text

            fragment_score, alignment_json = (
                _analysis_fragment_alignment(data, bool(fragment_context))
                if data and score >= 65 else (None, None)
            )
            db.update_job_analysis(job_id, analysis_text, score, analysis_signature)
            # Fragment-aware composite scoring now uses the full-analysis JSON
            # instead of a separate alignment LLM call. composite_score falls
            # back to match_score when no fragment score is available.
            composite_score = _compose_score(score, fragment_score)
            try:
                db.update_job_fragment_alignment(job_id, fragment_score, composite_score, alignment_json)
            except Exception as exc:
                log(f"Composite score persist skipped for job {job_id}: {exc}")
            if fragment_score is not None:
                log(f"Analyzed job ID {job_id}. Match score: {score}%; Fragment score: {fragment_score}%; Composite: {composite_score}%")
            else:
                reason = "no fragment bank" if not fragment_context else "no fragment score returned"
                log(f"Analyzed job ID {job_id}. Match score: {score}% ({reason}; composite = match)")

        except json.JSONDecodeError as e:
            log(f"Error decoding JSON for job ID {job_id}: {e}")
            log(f"Failing string: {json_string}")
            error_text = f"Analysis failed: Malformed JSON.\nError: {e}\nResponse:\n{llm_response_text}"
            db.update_job_analysis(job_id, error_text, 0, analysis_signature)
            try:
                db.update_job_fragment_alignment(job_id, None, _compose_score(0, None), None)
            except Exception as exc:
                log(f"Composite score persist skipped for job {job_id}: {exc}")
        except Exception as e:
            log(f"Error analyzing job ID {job_id}: {e}")
            error_text = f"Analysis failed: {e}"
            db.update_job_analysis(job_id, error_text, 0, analysis_signature)
            try:
                db.update_job_fragment_alignment(job_id, None, _compose_score(0, None), None)
            except Exception as exc:
                log(f"Composite score persist skipped for job {job_id}: {exc}")


def analyze_jobs(log_callback=None, resume_text: str = "", re_analyze: bool = False, status_filter: str = 'new', profile_id=1):
    """Analyze jobs using the configured local endpoint."""
    log = log_callback or print
    local = _local_ai_settings()
    log(f"Analyzing '{status_filter}' jobs with local endpoint ({local['model'] or 'no model configured'})...")

    if not _local_is_configured():
        log("ERROR: Local LLM endpoint is not configured. Analysis halted.")
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")
    if not resume_text:
        log("Halting analysis because resume text was not provided.")
        return

    jobs_to_analyze = db.get_jobs_to_analyze(status_filter, re_analyze, profile_id, resume_text)
    log(f"Found {len(jobs_to_analyze)} jobs to analyze in the '{status_filter}' view.")

    _perform_analysis_loop(jobs_to_analyze, resume_text, ANALYSIS_SYSTEM_PROMPT, log_callback, profile_id)
    log("Analysis complete.")


def analyze_specific_jobs(job_ids, log_callback=None, resume_text: str = "", profile_id=1):
    """Analyzes a specific list of jobs by their IDs using the local endpoint."""
    log = log_callback or print
    local = _local_ai_settings()
    log(f"Analyzing {len(job_ids)} specific job(s) with local endpoint ({local['model'] or 'no model configured'})...")

    if not _local_is_configured():
        log("ERROR: Local LLM endpoint is not configured. Analysis halted.")
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")
    if not resume_text:
        log("Halting analysis because resume text was not provided.")
        return

    jobs_to_analyze = db.get_jobs_to_analyze_by_ids(job_ids)
    log(f"Found {len(jobs_to_analyze)} jobs in DB from the provided list of IDs.")

    _perform_analysis_loop(jobs_to_analyze, resume_text, ANALYSIS_SYSTEM_PROMPT, log_callback, profile_id)
    log("Specific analysis complete.")


def generate_application_documents(
    base_resume_text: str,
    job_id: int,
    log_callback=None,
    profile_id=1,
    position_description_text="",
):
    """Generate tailored resume and cover letter using the local endpoint."""
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()
    log = log_callback if log_callback else print

    if not _local_is_configured():
        raise ValueError("Local LLM endpoint is not configured. Check Settings > AI & Credentials.")

    if not base_resume_text:
        raise ValueError("Base resume text cannot be empty.")

    job_data = db.get_job_details(job_id)
    if not job_data:
        raise ValueError(f"Job with ID {job_id} not found.")

    job_title, company, job_description, pdf_text = (
        job_data['title'], job_data['company'], job_data['description'], job_data['pdf_text']
    )
    fit_analysis = job_data.get('ai_analysis') or "No prior fit analysis is available."

    full_job_text = job_description or ""
    if pdf_text:
        full_job_text += f"\n\n--- JOB DETAILS FROM PDF ---\n{pdf_text}"
    if position_description_text:
        full_job_text = (
            f"--- UPLOADED POSITION DESCRIPTION ---\n{position_description_text}\n\n"
            f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_job_text}"
        )
        log(f"Using uploaded position description ({len(position_description_text)} chars).")

    log(f"Generating formatted, tailored resume for {job_title}...")
    resume_prompt = f"""You are a senior Australian resume writer producing a tailored single-document resume for ONE specific application: '{job_title}' at '{company}'.

OUTPUT CONTRACT
- Output ONLY the resume markdown. No preamble, no commentary, no <think> tags, no code fences.
- The response MUST start with the original resume header name exactly as supplied, formatted as `# <Candidate Name>`.
- Use Australian English spelling throughout (organisation, optimise, programme/program, etc.).

EVIDENCE DISCIPLINE
1. Preserve every real employer, title, date, qualification, and contact detail exactly as it appears in the original resume. Never alter dates or invent dates.
2. Never invent achievements, metrics, responsibilities, tools, certifications, sectors, scale, or relationships. If a number is not in the source, do not state one.
3. Where the fit analysis names a gap, reposition adjacent evidence honestly — do not paper over it with vague claims.
4. Mirror the ad's language only where the resume genuinely backs it. Do not echo ad keywords that are not evidenced.

TAILORING STRATEGY
- The top third of the resume (summary + core capabilities + first role's leading bullets) must carry the application strategy for THIS ad.
- Reorder roles to keep chronology, but reorder bullets within each role to surface what matters for this job first.
- Older / less relevant roles may be compressed to 2-3 bullets; do not delete them outright if the source includes them.
- Each bullet starts with a strong verb (Led, Delivered, Owned, Designed, Reduced, Standardised, Migrated, Negotiated, Established, Recovered). No "Responsible for" / "Duties included".
- Where a real metric exists in the source, surface it in **bold**. Do not fabricate one.

MARKDOWN FORMAT (the downstream renderer depends on this exactly)
- `# Name` — candidate's name, top line only.
- Immediately under the name, contact lines (Phone, Email, LinkedIn) on separate lines. No special characters or icons.
- `## SECTION HEADING` for each section. Recommended order: PROFESSIONAL SUMMARY, CORE CAPABILITIES, PROFESSIONAL EXPERIENCE, EDUCATION, CERTIFICATIONS (if present in source), TECHNICAL SKILLS.
- `### Job Title` on its own line for each role.
- Immediately under, on its own line: `**Company Name** | City, State | Month Year - Month Year` (use exactly the city/state/dates from the source).
- `* Bullet text.` for every achievement/responsibility bullet. Use `**bold**` inline for key metrics, named tools, or platforms — sparingly.
- PROFESSIONAL SUMMARY is a single paragraph of 3-5 sentences, ad-targeted. CORE CAPABILITIES is a bulleted list of 8-12 capability phrases ordered by relevance to the ad.

LENGTH TARGET
- 2 pages of A4 equivalent. Trim padding before adding length.

INPUTS

Fit Analysis (use this to choose tailoring priorities):
---
{fit_analysis}
---

Job Advertisement (the target of all tailoring decisions):
---
{full_job_text}
---

Original Resume (source of truth — every fact must come from here):
---
{base_resume_text}
---
"""

    try:
        tailored_resume_draft = _call_unsloth(
            messages=[{"role": "user", "content": resume_prompt}],
            temperature=0.25,
            max_tokens=8000,
        )
    except Exception as e:
        log(f"Error generating tailored resume: {e}")
        raise
    log("Tailored resume draft generated.")

    log(f"Generating cover letter for {job_title} at {company}...")
    cover_letter_prompt = f"""You are a senior Australian cover letter writer. Write the cover letter body for '{job_title}' at '{company}'.

OUTPUT CONTRACT
- Output ONLY the cover letter body. No subject, no date, no addresses, no "Dear ..." salutation, no signoff line. The downstream renderer adds those.
- Start with the first paragraph of the letter. Do NOT use Markdown headings (`#`, `##`, `###`) or list bullets (`*`, `-`). Plain paragraphs separated by blank lines. `**bold**` is permitted, sparingly.
- Australian English spelling.

VOICE
- Confident, specific, conversational-professional. Plain sentences.
- Banned phrases: "I am writing to apply", "please find attached", "thank you for your consideration", "passionate", "dynamic", "results-driven", "team player", "proven track record", "wear many hats", "go the extra mile", "synergy".

EVIDENCE DISCIPLINE
- Every claim must trace to the tailored resume, fit analysis, or job ad. Do not invent facts, metrics, relationships, certifications, sectors, or scale.
- Where the fit analysis names a gap, address it once, honestly, with the strongest adjacent evidence. Do not apologise and do not pretend it isn't there.

STRUCTURE (4 paragraphs, ~300-380 words total)
1. Opening (~3 sentences): name the role and one concrete anchor — a specific past role/project/outcome from the tailored resume that maps to the ad's headline requirement. No throat-clearing.
2. Evidence paragraph 1 (~4 sentences): the strongest fit claim from the analysis, anchored to a specific employer/project in the resume. Mirror ad language only where the resume backs it.
3. Evidence paragraph 2 (~4 sentences): the second strongest claim. If a meaningful gap exists, handle it here in one honest sentence framed around the adjacent strength.
4. Forward-looking close (~2-3 sentences): what the candidate would prioritise in the first 90 days, grounded in the ad's named priorities. End with a real call to discuss specific examples in interview — no "thank you for considering".

INPUTS

Fit Analysis (use this to choose the argument):
---
{fit_analysis}
---

Job Advertisement (the target):
---
{full_job_text}
---

Tailored Resume (the source of every factual claim in the letter):
---
{tailored_resume_draft}
---
"""

    try:
        cover_letter_draft = _call_unsloth(
            messages=[{"role": "user", "content": cover_letter_prompt}],
            temperature=0.55,
            max_tokens=2500,
        )
    except Exception as e:
        log(f"Error generating cover letter: {e}")
        raise
    log("Cover letter draft generated.")

    return tailored_resume_draft, cover_letter_draft


APPLICATION_DOCUMENT_SYSTEM_PROMPT = """You are a senior Australian application writer producing structured content for one targeted application. The app renders your JSON into DOCX templates — you write content, the app owns layout.

OUTPUT CONTRACT
- Return exactly ONE valid JSON object. Nothing before or after. No markdown fences, no <think> tags, no commentary.
- All strings must be valid JSON. Escape every internal line break as \\n. No raw newlines inside string values.
- Australian English spelling throughout (e.g. organisation, optimise, recognised, programme/program).

TRUTHFULNESS DISCIPLINE (hard rules)
1. Use ONLY facts present in the base resume, user-supplied additional candidate evidence, fit analysis, lane fragments, and job advertisement. Do not invent employers, dates, titles, qualifications, certifications, tools, metrics, sectors, awards, salary, scale, or relationships.
2. If a number, scale, or outcome is not in the source, do not state it. "Significant" / "large" / "complex" are acceptable only when the source supports it.
3. Mirror the ad's language only where the resume genuinely backs it. Do not echo ad keywords that are not evidenced.
4. The fit analysis names gaps. Reposition adjacent evidence honestly; do not pretend the gap does not exist.

PROFESSIONAL PROFILE (3-5 sentences, single string)
- Sentence 1: positioning headline naming the role family and seniority the resume supports.
- Sentence 2-3: two strongest pieces of evidence for THIS ad, named with employer/project context.
- Sentence 4: one capability differentiator that matters for this ad.
- Optional sentence 5: outcome orientation or sector framing if the ad warrants it.
- Avoid: "passionate", "dynamic", "results-driven", "team player", "seeking" — they read as filler.

CORE SKILLS (8-12 items)
- Each item is a capability phrase, not a single word. Prefer "Cloud platform leadership" over "Cloud".
- Order by relevance to THIS role. The first 4 items should map directly to the top 4 ad requirements.
- No duplicates, no near-duplicates, no acronyms without expansion unless the ad uses them.

PROFESSIONAL EXPERIENCE
- Choose the most relevant 3-5 roles from the base resume. Older / less relevant roles can be omitted entirely.
- Preserve real company, title, and date exactly as they appear in the source. Do not normalise or paraphrase dates.
- "summary" is 1-2 sentences setting role context FOR THIS APPLICATION: scope, team, sector, mandate.
- "achievements" are 4-8 bullets. Each bullet:
  * Starts with a strong verb (Led, Delivered, Owned, Designed, Reduced, Standardised, Migrated, Negotiated).
  * Names a concrete output or behaviour and, where the source provides it, the outcome.
  * Uses ad-aligned language when the resume evidence supports it.
  * Is one line where possible. No multi-clause padding.
  * Never invents metrics. If the source has a number, use it; if not, describe outcome qualitatively.

GENERATION NOTES (1-4 items)
- Surface any evidence gap that could not be honestly bridged, anything the user should manually verify, or any assumption you had to make. Empty array is acceptable only if nothing is worth flagging.

REQUIRED JSON SHAPE
{
  "professional_profile": "string",
  "core_skills": ["string", ...],
  "professional_experience": [
    {
      "company": "exact employer name from resume",
      "title": "exact title from resume",
      "dates": "exact date range from resume",
      "summary": "1-2 sentence role context",
      "achievements": ["bullet", ...]
    }
  ],
  "generation_notes": ["string", ...]
}

The cover letter is generated in a SEPARATE call. Do not include cover letter content in this response.
"""


def generate_template_application_content(
    job_id: int,
    resume_text: str,
    settings=None,
    log_callback=None,
    position_description_text="",
    additional_candidate_context="",
):
    settings = _settings_for_ai_task(settings, "document_ai_provider")
    if concurrency.cancel_event.is_set():
        raise concurrency.OperationCancelledError("Operation cancelled.")
    concurrency.paused.wait()
    log = log_callback or print
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job with ID {job_id} not found.")
    if not resume_text:
        raise ValueError("Base resume text cannot be empty.")

    additional_candidate_context = str(
        additional_candidate_context
        or (job["additional_candidate_context"] if "additional_candidate_context" in job.keys() else "")
        or ""
    ).strip()
    additional_context_block = f"""
ADDITIONAL CANDIDATE EVIDENCE (USER-SUPPLIED FOR THIS APPLICATION):
Treat this as first-party evidence. Use only what is stated; do not infer or embellish beyond it. If it expresses a preference or instruction rather than a fact, use it as writing guidance.
---
{additional_candidate_context[:12000]}
---
""" if additional_candidate_context else ""

    uploaded_position_description = position_description_text or job["position_description_text"] or ""
    full_job_text = job["description"] or ""
    if job["pdf_text"]:
        full_job_text += f"\n\n--- ADDITIONAL JOB TEXT ---\n{job['pdf_text']}"
    if uploaded_position_description:
        full_job_text = (
            f"--- UPLOADED POSITION DESCRIPTION ---\n{uploaded_position_description}\n\n"
            f"--- SCRAPED JOB ADVERTISEMENT ---\n{full_job_text}"
        )
        log(f"Using uploaded position description ({len(uploaded_position_description)} chars).")
    company_context = job["company_intelligence"] or "{}"
    provider = ((settings or {}).get("doc_ai_provider") or "local").lower()
    is_local_provider = provider == "local"
    # Qwen3 32K context budget — keep inputs generous so the model has the
    # source material to ground every claim, and give the JSON output enough
    # room for 3-5 roles with 4-8 evidence-anchored bullets each.
    resume_limit = 14000 if is_local_provider else 18000
    job_limit = 10000 if is_local_provider else 12000
    cover_resume_limit = 9000 if is_local_provider else 10000
    cover_job_limit = 7000 if is_local_provider else 9000
    output_tokens = 8000 if is_local_provider else 10000
    lane_context = (settings or {}).get("lane_context") or {}
    lane = lane_context.get("lane") or {}
    lane_settings = lane_context.get("settings") or {}
    lane_fragments = lane_context.get("fragments") or []
    fragment_lines = []
    for fragment in lane_fragments[:30]:
        fragment_lines.append(
            f"- [{fragment.get('id')}] {fragment.get('theme')}: {fragment.get('claim')} "
            f"Guidance: {fragment.get('reuse_guidance') or ''}"
        )
    lane_prompt_context = f"""
LANE / POSITIONING STRATEGY:
Name: {lane.get('name') or ''}
Intent: {lane_settings.get('lane_intent') or ''}
Target titles: {lane_settings.get('target_titles') or ''}
Target domains: {lane_settings.get('target_domains') or ''}
Seniority: {lane_settings.get('seniority') or ''}
Document strategy: {lane_settings.get('document_strategy') or ''}
Must-have signals: {lane_settings.get('must_have_terms') or ''}
Avoid signals: {lane_settings.get('avoid_terms') or ''}

SELECTED CANDIDATE FRAGMENTS:
{chr(10).join(fragment_lines) if fragment_lines else 'No lane-selected fragments were available.'}
"""

    def build_resume_messages(local_retry=False):
        retry_resume_limit = 6000 if local_retry else resume_limit
        retry_job_limit = 3500 if local_retry else job_limit
        retry_analysis_limit = 2500 if local_retry else None
        fit_analysis = job['ai_analysis'] or 'No prior analysis is available.'
        if retry_analysis_limit:
            fit_analysis = fit_analysis[:retry_analysis_limit]
        return [
        {"role": "system", "content": APPLICATION_DOCUMENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"""Create structured content for a targeted application.

CANDIDATE:
{MY_INFO.get('first_name', '')} {MY_INFO.get('last_name', '')}
{MY_INFO.get('phone', '')}
{MY_INFO.get('email', '')}
{MY_INFO.get('linkedin', '')}

{lane_prompt_context}

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}
Application URL: {job['application_url'] or job['url'] or ''}
Salary / rate: {job['salary'] or ''}
Closing date: {job['closing_date'] or ''}
Contact: {job['contact_person'] or ''} {job['contact_email'] or ''} {job['contact_phone'] or ''}

FIT ANALYSIS:
---
{fit_analysis}
---

COMPANY INTELLIGENCE:
---
{company_context}
---

JOB ADVERTISEMENT:
---
{full_job_text[:retry_job_limit]}
---

BASE RESUME:
---
{resume_text[:retry_resume_limit]}
---

{additional_context_block}""",
        },
    ]

    messages = build_resume_messages()
    try:
        response, provider_label = _call_document_ai(
            settings or {}, messages, temperature=0.2, max_tokens=output_tokens, json_mode=True
        )
    except Exception as exc:
        if not is_local_provider:
            raise
        log(f"Local document AI failed on the full application prompt; retrying with compact context. Error: {exc}")
        response, provider_label = _call_document_ai(
            settings or {}, build_resume_messages(local_retry=True),
            temperature=0.2, max_tokens=5000, json_mode=True,
        )
    data = _extract_json(response)
    if not data:
        log("The selected AI returned malformed JSON. Asking it to repair the response...")
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Convert the supplied text into one valid JSON object only. "
                    "Do not add commentary, markdown fences, or new content. "
                    "Escape line breaks inside string values as \\n. "
                    "If a field is incomplete, keep the valid completed content and close the JSON correctly."
                ),
            },
            {"role": "user", "content": response[:30000]},
        ]
        repaired, _ = _call_document_ai(
            settings or {}, repair_messages, temperature=0.0, max_tokens=output_tokens, json_mode=True
        )
        data = _extract_json(repaired)
    if not data:
        raise ValueError(f"The selected AI did not return valid JSON. Response started: {response[:300]}")
    log("Generating cover letter content separately...")
    cover_messages = [
        {
            "role": "system",
            "content": (
                "You are a senior Australian cover letter writer for the candidate described in the supplied resume. "
                "Return ONLY one valid JSON object. No markdown fences, commentary, or <think> tags. "
                "Escape every internal newline inside strings as \\n.\n\n"
                "VOICE: confident, specific, conversational-professional. Australian English. "
                "Plain sentences over corporate jargon. No 'passionate', 'dynamic', 'team player', "
                "'results-driven', 'I am writing to apply', 'please find attached', 'thank you for your consideration'.\n\n"
                "EVIDENCE DISCIPLINE: every claim must trace to the resume, fit analysis, lane fragments, or job ad. "
                "Do not invent employers, dates, qualifications, certifications, tools, metrics, sectors, or relationships. "
                "Where the fit analysis names a gap, neutralise it once, honestly, with the strongest adjacent evidence.\n\n"
                "STRUCTURE:\n"
                "- subject: 'RE: <Role title> application' (Australian convention).\n"
                "- greeting: 'Dear Hiring Manager,' unless a named contact is supplied.\n"
                "- opening (1 short paragraph): name the role and one concrete anchor — a specific resume artefact, an outcome that maps to the ad, or a relevant sector pattern. No throat-clearing.\n"
                "- body (2 paragraphs in the array): each one is evidence-led. Paragraph 1 anchors the strongest fit claim from the analysis to a specific past role/project. Paragraph 2 covers the second strongest claim and, if there's a gap, addresses it in one sentence without apology.\n"
                "- value_proposition (1 short paragraph): what the candidate would do in the first 90 days, grounded in the ad's named priorities. Avoid generic 'add value'.\n"
                "- closing (1 short paragraph): a real call-to-action — happy to talk through specific examples in interview. No 'thank you for considering'.\n"
                "- signoff: 'Kind regards\\n<Candidate Name>' using the exact name from the resume header.\n\n"
                "LENGTH TARGET: ~300-380 words total. Trim before padding.\n\n"
                "REQUIRED SHAPE:\n"
                "{\"cover_letter\":{\"subject\":\"...\",\"greeting\":\"Dear Hiring Manager,\","
                "\"opening\":\"...\",\"body\":[\"...\",\"...\"],\"value_proposition\":\"...\","
                "\"closing\":\"...\",\"signoff\":\"Kind regards\\n<Candidate Name>\"}}"
            ),
        },
        {
            "role": "user",
            "content": f"""Write the cover letter content for this application.

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}

{lane_prompt_context}

FIT ANALYSIS:
---
{job['ai_analysis'] or 'No prior analysis is available.'}
---

COMPANY INTELLIGENCE:
---
{company_context}
---

JOB ADVERTISEMENT:
---
{full_job_text[:cover_job_limit]}
---

BASE RESUME:
---
{resume_text[:cover_resume_limit]}
---

{additional_context_block}""",
        },
    ]
    try:
        cover_response, _ = _call_document_ai(
            settings or {}, cover_messages, temperature=0.35, max_tokens=5000, json_mode=True
        )
    except Exception as exc:
        if not is_local_provider:
            raise
        log(f"Local document AI failed on the cover letter prompt; retrying with compact context. Error: {exc}")
        compact_cover_messages = [
            cover_messages[0],
            {
                "role": "user",
                "content": f"""Write the cover letter content for this application.

ROLE:
Title: {job['title']}
Company: {job['company'] or ''}
Location: {job['location'] or ''}

FIT ANALYSIS:
---
{(job['ai_analysis'] or 'No prior analysis is available.')[:1800]}
---

JOB ADVERTISEMENT:
---
{full_job_text[:3000]}
---

BASE RESUME:
---
{resume_text[:4500]}
---

{additional_context_block}""",
            },
        ]
        cover_response, _ = _call_document_ai(
            settings or {}, compact_cover_messages, temperature=0.35, max_tokens=3500, json_mode=True
        )
    cover_data = _extract_json(cover_response)
    if cover_data and isinstance(cover_data.get("cover_letter"), dict):
        data["cover_letter"] = cover_data["cover_letter"]
    elif cover_data:
        cover_keys = {"subject", "greeting", "opening", "body", "value_proposition", "closing", "signoff"}
        flattened = {key: cover_data.get(key) for key in cover_keys if cover_data.get(key)}
        if flattened:
            data["cover_letter"] = flattened
    if not isinstance(data.get("cover_letter"), dict):
        log("Cover letter generation did not return usable content; inserting a manual-review placeholder.")
        data["cover_letter"] = {
            "subject": f"RE: {job['title']} application",
            "greeting": "Dear Hiring Manager,",
            "opening": "Cover letter generation did not return usable content. Please regenerate or review the AI provider settings.",
            "body": [],
            "value_proposition": "",
            "closing": "",
            "signoff": "Kind regards\nCandidate",
        }
    log(f"Application content generated with {provider_label}.")
    return data, provider_label


def extract_application_memory_fragments(
    application_payload,
    settings=None,
    log_callback=None,
    prior_lane_fragments=None,
    kit_outcome=None,
):
    """Mine reusable typed fragments from a saved (human-validated) application kit.

    Submitted applications are higher-signal than raw scraped jobs: a human chose
    to spend a real application slot on this role and approved the kit. The
    extracted fragments form a candidate-memory bank that future jobs are scored
    against, that future search terms are derived from, and that future
    tailoring leans on.

    Parameters
    ----------
    application_payload
        Dict containing the saved kit (job ad, tailored resume, cover letter,
        analysis, source paths). See _saved_application_document_sources in
        python_bridge.py for the shape.
    prior_lane_fragments
        OPTIONAL list of fragments already in this lane's bank (from earlier
        applied jobs). When supplied, the prompt reconciles the new extraction
        against the existing bank: reinforcing themes get lifted confidence,
        genuinely new themes are flagged as `emerging`, and near-duplicates are
        merged via `reinforces_fragment_themes`. Pass this every time so the
        extraction is lane-aware, not isolated.
    kit_outcome
        OPTIONAL string: 'applied', 'interviewed', 'rejected', 'liked',
        'archived', or 'unknown'. Biases confidence and status assignment.

    See the top-of-file architecture note for the wider memory loop.
    """
    log = log_callback or print
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")

    prior_context = _format_fragment_context(prior_lane_fragments)
    prior_block = (
        f"\n\nPRIOR LANE FRAGMENT BANK (from earlier applied jobs in THIS lane — reconcile against these):\n"
        f"---\n{prior_context}\n---\n"
        f"When this kit reinforces a prior theme, list its theme in `reinforces_fragment_themes` and lift confidence accordingly. "
        f"When the new claim is genuinely new for this lane, mark status='emerging'. Avoid producing near-duplicates of prior themes — "
        f"merge by reusing the prior theme name."
        if prior_context else
        "\n\nPRIOR LANE FRAGMENT BANK: (none — this is the first applied job mined for this lane, or the caller did not supply prior context)"
    )

    outcome_block = ""
    if kit_outcome:
        outcome_block = (
            f"\n\nKIT OUTCOME: {kit_outcome}\n"
            "Bias confidence by outcome: 'interviewed' or 'liked' lifts confidence one band on fragments well-anchored in the kit; "
            "'rejected' caps confidence at 'medium' and prefers status='emerging' for new themes; 'archived' caps at 'low'; "
            "'applied' or 'unknown' uses the normal evidence-based heuristic."
        )

    messages = [
        {
            "role": "system",
            "content": (
                "You extract reusable, typed candidate-memory fragments from a saved Australian job application kit "
                "(job ad + tailored resume + cover letter + analysis). Fragments form a long-term memory of what the "
                "candidate can credibly claim, where evidence sits, what job language activates each claim, and how "
                "strong each signal is. Return ONLY one valid JSON object with key 'fragments'. No <think> tags, no markdown. "
                "Australian English. Every fragment must be grounded in the supplied documents — do not invent. "
                "When a prior lane fragment bank is supplied, reconcile against it: reinforce known themes, flag truly new ones."
            ),
        },
        {
            "role": "user",
            "content": f"""Extract 8-18 typed fragments from the saved application kit below.{prior_block}{outcome_block}

FRAGMENT TYPES (use one per fragment; pick the best fit)
- capability: a skill or pattern the candidate repeatedly sells (e.g. systems thinking, technical leadership, vendor management).
- domain:     a sector / context with named tenure (utilities, councils, healthcare, higher education, manufacturing, infrastructure).
- seniority:  a leadership-level signal (team lead, manager, strategic advisor, executive-facing, governance, budget responsibility).
- outcome:    a concrete value-claim (reduced risk, improved reliability, delivered transformation, automated manual work).
- tool:       platform / stack evidence (ERP, M365, Azure, Power BI, ITSM, integrations, governance frameworks).
- preference: a pattern across roles the candidate actually applied for (hybrid Melbourne, IT/business bridge, delivery-heavy not pure coding, systems ownership).

PER-FRAGMENT FIELDS (all required)
- fragment_type:   one of the six types above.
- theme:           short title (3-6 words).
- claim:           one reusable sentence the candidate can credibly assert.
- evidence:        one or two sentences citing the source — name the employer/project/outcome from THIS application kit. Use exact dates only when present in the source.
- job_families:    1-4 role families this fragment is genuinely useful for (e.g. "Delivery Lead", "Business Systems Manager").
- keywords:        4-10 ad-side phrases that should ACTIVATE this fragment (the words you would scan a job ad for).
- anti_keywords:   2-6 ad-side signals that mean this fragment is NOT a good fit (e.g. "L1 support", "pure coding role", "C-suite").
- seniority:       "individual" | "lead" | "manager" | "executive" | "unknown".
- skills:          0-6 supporting skills.
- domains:         0-4 supporting domains.
- reuse_guidance:  one sentence on WHEN to use it AND when to avoid it.
- confidence:      "high" | "medium" | "low".
- confidence_reasoning: one short sentence explaining the confidence (e.g. "Appears across two roles with named outcomes" vs "Single-role evidence, not yet repeated").
- status:          "established" if the evidence is concrete and repeatable; "emerging" if the fragment is plausible but rests on a single stretch application — emerging fragments are kept for cautious reuse, not narrow echo-chamber filtering.

QUALITY BAR
- Fragments are small reusable units, NOT whole paragraphs to copy. Aim for portable claims.
- Avoid generic platitudes ("strong communicator"). Every fragment must have something specific to point at in the kit.
- Prefer fragments that appear across multiple roles in the kit — they are higher-confidence.
- A single one-off claim can still produce a fragment, but mark status="emerging" and confidence<=medium.

JSON SHAPE
{{"fragments":[
  {{
    "fragment_type":"capability|domain|seniority|outcome|tool|preference",
    "theme":"...",
    "claim":"...",
    "evidence":"...",
    "job_families":["..."],
    "keywords":["..."],
    "anti_keywords":["..."],
    "seniority":"individual|lead|manager|executive|unknown",
    "skills":["..."],
    "domains":["..."],
    "reuse_guidance":"...",
    "confidence":"high|medium|low",
    "confidence_reasoning":"...",
    "status":"established|emerging",
    "reinforces_fragment_themes":["EXACT theme name(s) from the PRIOR LANE FRAGMENT BANK this kit reinforces — empty array when this is a genuinely new theme"]
  }}
]}}

APPLICATION KIT:
---
{json.dumps(application_payload, ensure_ascii=False)[:18000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.15, max_tokens=5000, json_mode=True
    )
    data = _extract_json(response) or _repair_json_via_llm(response, settings=local_settings)
    fragments = _normalise_memory_fragments(data.get("fragments") if isinstance(data, dict) else None)
    if not fragments:
        raise ValueError(f"Memory extraction did not return valid fragments. Response started: {response[:250]}")
    log(f"Extracted memory fragments with {provider_label}.")
    return fragments, provider_label


def align_memory_fragments_to_role(role_payload, fragments, settings=None, log_callback=None):
    """Score a target role against the candidate-memory fragment bank.

    Instead of only asking "does this resume match the ad?", this asks:
      1. Which stored fragments does this role ACTIVATE (via keyword match)?
      2. Which required capabilities have NO fragment support (true gaps)?
      3. Which activated fragments form the strongest application angle?
      4. Should we suggest an EMERGING fragment for a stretch role with no
         prior pattern, so the memory bank doesn't become an echo chamber?

    The output is intended to drive both tailoring and a fragment-aware score.
    """
    log = log_callback or print
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")
    messages = [
        {
            "role": "system",
            "content": (
                "You match a candidate-memory fragment bank to ONE Australian job advertisement. "
                "Return ONLY one valid JSON object. No <think> tags, no markdown. Australian English. "
                "Do not invent facts. Only cite fragments that genuinely match the role's named requirements. "
                "Do not narrow the candidate to known patterns: a stretch role should produce an EMERGING fragment "
                "suggestion rather than a rejection."
            ),
        },
        {
            "role": "user",
            "content": f"""Score this role against the supplied fragment bank.

PROCESS
1. Identify 5-10 role features from the job ad — the duties, capabilities, and ownership the ad actually asks for.
2. For each fragment in the bank, decide whether it is activated by the role features. A fragment is activated when at least one of its keywords appears in (or is clearly evidenced by) the role features, AND none of its anti_keywords describe the role.
3. List capability_gaps: role features for which NO fragment in the bank provides credible evidence.
4. Pick the angle_recommendation: the 2-3 strongest activated fragments combined into one sentence that should drive the application angle.
5. Suggest emerging fragments (status="emerging") ONLY when the role activates fewer than 3 fragments but the resume context suggests there is honest adjacent evidence worth capturing for future use.
6. Weight activated fragments by their stored confidence; flag any activations that rely solely on emerging/low-confidence fragments.

REQUIRED JSON SHAPE
{{
  "role_features": ["5-10 short feature strings from the ad"],
  "fragment_matches": [
    {{
      "fragment_id": 123,
      "theme": "...",
      "match_strength": "strong" | "medium" | "weak",
      "role_feature": "which role feature this fragment activates",
      "activating_keywords": ["keyword from the fragment that fired"],
      "fragment_confidence": "high|medium|low",
      "fragment_status": "established|emerging",
      "how_to_use": "one sentence on how to deploy this fragment in resume/cover letter",
      "caution": "risk or empty string"
    }}
  ],
  "capability_gaps": ["role features with no credible fragment support"],
  "angle_recommendation": "one sentence application angle drawing on the strongest activated fragments",
  "fragment_score": int 0-100 (composite: 4+ strong activations covering core features => 80+; 2-3 medium activations => 60-79; <2 activations => <60),
  "emerging_suggestions": [
    {{"theme":"...","claim":"...","why":"why this stretch role justifies capturing a low-confidence fragment for future cautious reuse"}}
  ],
  "writing_strategy": "concise strategy for how to lean on activated fragments and how to address each capability gap"
}}

ROLE:
---
{json.dumps(role_payload, ensure_ascii=False)[:9000]}
---

FRAGMENT BANK:
---
{json.dumps(fragments, ensure_ascii=False)[:16000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.1, max_tokens=5000, json_mode=True
    )
    data = _extract_json(response)
    if not data:
        raise ValueError(f"Memory alignment did not return valid JSON. Response started: {response[:250]}")
    log(f"Aligned memory fragments with {provider_label}.")
    return data, provider_label


# ---------------------------------------------------------------------------
# Fragment-architecture extras: consolidation, promotion, fragment-driven
# search terms, and a thin helper for fragment-aware analysis injection.
# These are intentionally pure LLM helpers — persistence and scheduling are
# the caller's responsibility (see the architecture note at the top of file).
# ---------------------------------------------------------------------------


def consolidate_memory_fragments(fragments_from_kits, settings=None, log_callback=None):
    """Dedupe + merge fragments across many application kits.

    Same theme appearing across multiple kits should produce ONE consolidated
    fragment with a lifted confidence. Truly one-off claims stay as separate
    emerging fragments. The output is intended to overwrite or supplement the
    persisted fragment bank.

    Input shape: a list of {kit_id, role_title, outcome, fragments:[...]}
    where outcome is one of: 'applied', 'interviewed', 'rejected', 'liked',
    'archived', or 'unknown'. Outcome weighting is performed by the model.
    """
    log = log_callback or print
    if not fragments_from_kits:
        return [], "no fragments to consolidate"
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")
    messages = [
        {
            "role": "system",
            "content": (
                "You consolidate a candidate's typed memory fragments across many submitted Australian "
                "job applications. Same theme across many kits => ONE merged fragment with lifted "
                "confidence. One-off claims stay separate, marked status='emerging' and confidence<=medium. "
                "Return ONLY one valid JSON object. No <think> tags, no markdown. Australian English. "
                "Never invent facts not present in the supplied fragments."
            ),
        },
        {
            "role": "user",
            "content": f"""Consolidate the supplied per-kit fragments into a deduped fragment bank.

RULES
- Merge fragments with the same theme/claim across kits. Keep the strongest evidence wording.
- Track which kits supported the merged fragment in `source_kit_ids` and how many times the theme appeared in `support_count`.
- Outcome weighting: bias confidence UP when supporting kits include outcome='interviewed' or 'liked'; bias DOWN when only 'rejected' or 'archived' kits support it; ignore 'unknown'.
- Confidence ladder: support_count >= 4 with at least one interviewed kit => "high"; support_count 2-3 => "medium"; support_count 1 => "low" and status="emerging".
- Promote status from 'emerging' to 'established' ONLY when support_count >= 2 AND at least one non-rejected outcome.
- Preserve the typed shape — keep fragment_type, keywords, anti_keywords, job_families, etc.

REQUIRED JSON SHAPE
{{
  "consolidated_fragments": [
    {{
      "fragment_type": "capability|domain|seniority|outcome|tool|preference",
      "theme": "...",
      "claim": "merged sentence using strongest source wording",
      "evidence": "merged evidence citing the strongest source kit",
      "job_families": ["..."],
      "keywords": ["..."],
      "anti_keywords": ["..."],
      "seniority": "individual|lead|manager|executive|unknown",
      "skills": ["..."],
      "domains": ["..."],
      "reuse_guidance": "...",
      "confidence": "high|medium|low",
      "confidence_reasoning": "explain support_count + outcomes that drove the level",
      "status": "established|emerging",
      "source_kit_ids": [int, ...],
      "support_count": int,
      "outcomes_seen": ["interviewed","applied", ...]
    }}
  ],
  "dropped_fragments": [
    {{"theme":"...","reason":"why this fragment was dropped (duplicate of X, contradicted by Y, too vague)"}}
  ],
  "consolidation_notes": ["any patterns the user should know: dominant themes, gaps, contradictions"]
}}

PER-KIT FRAGMENTS:
---
{json.dumps(fragments_from_kits, ensure_ascii=False)[:30000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.05, max_tokens=8000, json_mode=True
    )
    data = _extract_json(response) or _repair_json_via_llm(response, settings=local_settings)
    if not data or not isinstance(data.get("consolidated_fragments"), list):
        raise ValueError(f"Fragment consolidation did not return valid JSON. Response started: {response[:250]}")
    log(f"Consolidated {len(data['consolidated_fragments'])} fragments with {provider_label}.")
    return data, provider_label


def promote_emerging_fragments(fragments, outcome_history, settings=None, log_callback=None):
    """Decide which 'emerging' fragments have earned promotion to 'established'.

    `outcome_history` is a list of {kit_id, outcome, role_title} so the model
    can check whether the fragment was reused successfully. The output lists
    only fragments whose status should change; the caller patches the bank.
    """
    log = log_callback or print
    emerging = [f for f in (fragments or []) if str(f.get("status", "")).lower() == "emerging"]
    if not emerging:
        return {"promotions": [], "demotions": [], "notes": ["No emerging fragments to evaluate."]}, "no emerging fragments"
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")
    messages = [
        {
            "role": "system",
            "content": (
                "You audit emerging candidate-memory fragments and decide which have earned promotion to "
                "'established' status. Return ONLY one valid JSON object. No <think> tags. Australian English. "
                "Be cautious: established fragments shape future applications, so the bar is real."
            ),
        },
        {
            "role": "user",
            "content": f"""Audit these emerging fragments against the outcome history.

PROMOTION RULE
- Promote to 'established' if the fragment now has source_kit_ids count >= 2 AND at least one supporting kit had outcome in ('interviewed', 'liked').
- Keep as 'emerging' otherwise — but lift confidence one band if outcomes are net positive (interviewed/liked outweigh rejected).
- DEMOTE (suggest deletion) if the only supporting kits had outcome='rejected' AND the fragment has not been reused in 3+ subsequent applications.

REQUIRED JSON SHAPE
{{
  "promotions": [
    {{"fragment_id_or_theme":"...","reason":"why this earned promotion (cite outcomes)","new_confidence":"high|medium|low"}}
  ],
  "demotions": [
    {{"fragment_id_or_theme":"...","reason":"why this should be dropped or downgraded"}}
  ],
  "confidence_adjustments": [
    {{"fragment_id_or_theme":"...","old":"medium","new":"high","reason":"..."}}
  ],
  "notes": ["patterns worth surfacing to the user"]
}}

EMERGING FRAGMENTS:
---
{json.dumps(emerging, ensure_ascii=False)[:18000]}
---

OUTCOME HISTORY:
---
{json.dumps(outcome_history or [], ensure_ascii=False)[:8000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.05, max_tokens=4000, json_mode=True
    )
    data = _extract_json(response) or _repair_json_via_llm(response, settings=local_settings)
    if not data:
        raise ValueError(f"Promotion audit did not return valid JSON. Response started: {response[:250]}")
    log(f"Promotion audit complete with {provider_label}.")
    return data, provider_label


def derive_search_terms_from_fragments(fragments, optimism_level=3, settings=None, log_callback=None):
    """Generate job-board search terms from the fragment bank, not the raw resume.

    Fragments are higher-signal than the resume because they encode WHICH
    capabilities have actually carried previous applications and WHERE the
    candidate has chosen to spend slots. The terms generated here should bias
    toward fragments that are 'established' and that activated in successful
    (interviewed/liked) applications.

    `fragments` may carry optional outcome metadata (avg_outcome_strength,
    times_activated, last_interview_at) populated by the caller from the DB.
    The prompt uses it when present and ignores it gracefully when absent.
    """
    log = log_callback or print
    if not fragments:
        return [], "no fragments — caller should fall back to derive_search_terms_from_resume"
    local_settings = _settings_for_ai_task(settings, "memory_ai_provider")

    if optimism_level <= 1:
        spread = "3-4 conservative titles drawn from the strongest established fragments only"
    elif optimism_level == 2:
        spread = "4-5 titles: established fragments + one realistic step-up"
    elif optimism_level == 3:
        spread = "5-6 titles: established + step-up + one cautious emerging fragment"
    elif optimism_level == 4:
        spread = "6-8 titles: established + step-up + adjacent + selective reach using emerging fragments"
    else:
        spread = "8-10 titles: full spread including ambitious reach from emerging fragments — but flag the reach titles"

    messages = [
        {
            "role": "system",
            "content": (
                "You generate Australian job-board search titles from a candidate's memory-fragment bank. "
                "Return ONLY a JSON array of strings — nothing else, no <think> tags, no markdown. "
                "Each title is canonical (Seek/LinkedIn keyword-search friendly), no boolean operators, "
                "no locations, no salary. Bias toward fragments with status='established', high confidence, "
                "and positive outcome history when that metadata is present. Use Australian title conventions."
            ),
        },
        {
            "role": "user",
            "content": f"""Generate {spread} from this fragment bank.

PROCESS
1. Group fragments by job_families.
2. For each high-confidence established cluster, generate the best matching canonical title.
3. Add step-up titles where a 'seniority' fragment indicates the candidate has demonstrated lead/manager/executive evidence.
4. Add adjacent titles by mixing capability + domain fragments (e.g. capability='enterprise systems ownership' + domain='councils' => 'Business Systems Manager').
5. Order by activation strength (established + high confidence + recent positive outcomes first).
6. NEVER include a title that conflicts with the anti_keywords on relevant fragments.

FRAGMENT BANK:
---
{json.dumps(fragments, ensure_ascii=False)[:18000]}
---""",
        },
    ]
    response, provider_label = _call_document_ai(
        local_settings, messages, temperature=0.35, max_tokens=1500
    )
    terms = _extract_json_list(response) or []
    if not terms:
        # Lenient fallback: line-split the response if the model returned a list-like blob.
        terms = [line.strip(' -"\t,') for line in str(response).splitlines() if line.strip(' -"\t,')]
        terms = [t for t in terms if len(t) <= 120 and not t.startswith('{')]
    log(f"Derived {len(terms)} fragment-driven search terms with {provider_label}.")
    return terms, provider_label


COMPANY_RESEARCH_SYSTEM_PROMPT = """You are a cautious Australian job-application company intelligence analyst.
You have NO live web access. Reason ONLY from the supplied job ad, existing local classifier output, and fit analysis. Never fabricate facts, addresses, headcount, revenue, founder names, recent news, or executives.

Return ONLY one valid JSON object — no markdown, no <think> tags, no commentary. Australian English spelling.

EMPLOYER-TYPE HEURISTICS
- "recruiter": agency name in the company field, "our client" / "on behalf of" language, generic role description with no specific employer context, application redirects to an ATS branded with an agency, contact is a consultant.
- "direct_employer": clearly named single employer with specific business context, application goes to that employer's careers portal or named hiring contact.
- "mixed": named employer but evidence suggests the role is being managed via an agency.
- "unknown": insufficient signal.

AUSTRALIAN CONTEXT TO RECOGNISE (only when explicitly evidenced in the ad)
- ASX-listed corporates, Big 4, state/federal government departments, Defence primes, universities, councils, water/energy utilities, health networks, NFP/NGO.
- Public-sector and regulated employers carry probity, conflict-of-interest, and clearance considerations — surface these in `risks` and `questions_to_clarify` when relevant.

RECRUITER-AD WARNING
- If employer_type is "recruiter" or "unknown" end client, populate `recruiter_warning` with concrete cautions: do not name the end client speculatively, ask for a position description, confirm whether the role is being managed exclusively, confirm the actual hiring entity before customising heavily.

REQUIRED JSON SHAPE
{
  "employer_type": "direct_employer" | "recruiter" | "mixed" | "unknown",
  "actual_company": "best-evidence employer name, or 'Unknown'",
  "confidence": "high" | "medium" | "low",
  "company_summary": "2-4 sentence summary of what can be inferred from the supplied evidence ONLY",
  "business_context": ["3-6 specific context points actually supported by the ad/classifier"],
  "application_angle": "one sentence on how to refer to the organisation in the resume and cover letter without speculating",
  "recruiter_warning": "string — empty if employer_type is direct_employer with high confidence",
  "evidence": ["3-6 specific quotes or signals from the ad/classifier that justify the assessment"],
  "questions_to_clarify": ["3-5 specific questions to ask the recruiter or hiring contact before applying"],
  "risks": ["2-5 specific risks/uncertainties — probity, clearance, ambiguous end client, salary opacity, etc."]
}
"""


def research_company_for_job(job_id: int, settings=None, log_callback=None):
    log = log_callback or print
    job = db.get_job_details(job_id)
    if not job:
        raise ValueError(f"Job with ID {job_id} not found.")
    existing = job["company_intelligence"] or "{}"
    prompt = f"""Build company intelligence for this job application.

ADVERTISER / COMPANY FIELD:
{job['company'] or ''}

JOB TITLE:
{job['title'] or ''}

CONTACT EMAIL:
{job['contact_email'] or ''}

APPLICATION URL:
{job['application_url'] or job['url'] or ''}

EXISTING LOCAL CLASSIFIER:
{existing}

FIT ANALYSIS:
{job['ai_analysis'] or 'No fit analysis yet.'}

JOB ADVERTISEMENT:
---
{(job['description'] or '')[:12000]}
---
"""
    response, provider_label = _call_document_ai(
        _settings_for_ai_task(settings, "research_ai_provider"),
        [
            {"role": "system", "content": COMPANY_RESEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=3500,
        json_mode=True,
    )
    data = _extract_json(response)
    if not data:
        raise ValueError(f"Company research did not return valid JSON. Response started: {response[:250]}")
    log(f"Company intelligence researched with {provider_label}.")
    return data, provider_label


def hidden_market_strategy(target, lane_context="", settings=None):
    """Generate a short, tailored outreach angle + concrete next steps for a
    hidden-market target using the local model. Returns plain prose (not JSON)."""
    target = target or {}
    target_type = target.get("target_type") or "target"
    type_label = {
        "recruiter": "recruitment agency / consultant who repeatedly carries this role family",
        "direct_employer": "direct employer that keeps hiring this role family",
        "leadership_gap": "employer hiring junior/IC staff with no leadership role advertised (possible unadvertised leadership need)",
    }.get(target_type, "hidden-market target")

    facts = [
        f"Target type: {type_label}",
        f"Name: {target.get('name') or target.get('target_name') or 'Unknown'}",
    ]
    if target.get("sample_titles"):
        facts.append("Roles seen: " + ", ".join(str(t) for t in (target.get("sample_titles") or [])))
    for label, key in (("Best fit score", "best_score"), ("Relevant roles in window", "roles"),
                       ("Junior/IC hires with no leader", "ic_count"), ("Domain", "domain"),
                       ("Locations", "locations")):
        value = target.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value:
            facts.append(f"{label}: {value}")
    contact = " · ".join(
        str(target.get(field)) for field in ("contact_person", "contact_email", "contact_phone") if target.get(field)
    )
    if contact:
        facts.append(f"Known contact: {contact}")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a pragmatic outreach strategist for the Australian hidden job market "
                "(unadvertised roles). Give specific, actionable advice the candidate can use today. "
                "Australian English. Plain text only — no markdown headings, no preamble, no <think> tags."
            ),
        },
        {
            "role": "user",
            "content": (
                "Candidate / lane context:\n"
                + (lane_context or "Experienced candidate; specific context not provided.")
                + "\n\nHidden-market target:\n" + "\n".join(facts)
                + "\n\nIn under 140 words give: (1) one or two sentences on the angle — why approach this "
                "target now and how to position; (2) 2 to 4 concrete next steps (who to contact, which channel, "
                "and what to say). Be specific to this target, not generic advice."
            ),
        },
    ]
    text = _call_unsloth(messages, temperature=0.3, max_tokens=600, json_mode=False, settings=settings)
    return (text or "").strip()
