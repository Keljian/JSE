"""Conservative pre-generation checks for whether a saved job is still live."""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from urllib.parse import urlparse

import requests


_CLOSED_MARKERS = (
    "this job is no longer available",
    "this job is no longer accepting applications",
    "no longer accepting applications",
    "this job has expired",
    "this job posting has expired",
    "job posting has expired",
    "applications are now closed",
    "applications have now closed",
    "applications for this job are closed",
    "this vacancy is closed",
    "this vacancy has closed",
    "this position has been filled",
    "the position has been filled",
    "this opportunity is no longer available",
)


def _past_explicit_closing_date(job, today=None):
    source = str(job.get("closing_date_source") or "").lower()
    value = str(job.get("closing_date") or "")[:10]
    if source not in {"advertisement", "provided"} or not value:
        return False
    try:
        closing = datetime.fromisoformat(value).date()
    except ValueError:
        return False
    return closing < (today or date.today())


def _visible_page_text(content):
    text = html.unescape(str(content or ""))
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def check_job_liveness(job, timeout=20, session=None, today=None):
    """Return live/closed/unknown, only declaring closed on strong evidence."""
    job = dict(job or {})
    if _past_explicit_closing_date(job, today=today):
        return {
            "status": "closed",
            "reason": f"Explicit closing date passed ({str(job.get('closing_date'))[:10]}).",
            "source": "closing_date",
        }

    url = str(job.get("url") or job.get("application_url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"status": "unknown", "reason": "No checkable job URL is available.", "source": "url"}

    client = session or requests
    try:
        response = client.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
    except requests.RequestException as exc:
        return {"status": "unknown", "reason": f"Listing check could not connect: {exc}", "source": "http"}
    except Exception as exc:
        return {"status": "unknown", "reason": f"Listing check failed: {exc}", "source": "http"}

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {404, 410}:
        return {
            "status": "closed",
            "reason": f"Job URL returned HTTP {status_code}.",
            "source": "http",
            "http_status": status_code,
        }
    if status_code in {401, 403, 429} or status_code >= 500 or status_code == 0:
        return {
            "status": "unknown",
            "reason": f"Job board returned HTTP {status_code}; it may be blocking automated checks.",
            "source": "http",
            "http_status": status_code,
        }

    page_text = _visible_page_text(getattr(response, "text", ""))
    marker = next((value for value in _CLOSED_MARKERS if value in page_text), None)
    if marker:
        return {
            "status": "closed",
            "reason": f"Job page says “{marker}”.",
            "source": "page",
            "http_status": status_code,
        }

    if 200 <= status_code < 400:
        return {
            "status": "live",
            "reason": f"Job page is reachable (HTTP {status_code}) with no closure signal.",
            "source": "http",
            "http_status": status_code,
        }
    return {
        "status": "unknown",
        "reason": f"Job URL returned HTTP {status_code}.",
        "source": "http",
        "http_status": status_code,
    }
