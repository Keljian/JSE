"""Turning model output into usable data: reasoning-block stripping and JSON recovery.

Split out of llm_handler.py, which re-exports everything here.
"""
import json
import re
from .providers import (
    _call_document_ai,
)

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


def _bullet_section(title, values):
    items = _coerce_list(values)
    if not items:
        return f"{title}:\n- N/A"
    return f"{title}:\n- " + "\n- ".join(items)
