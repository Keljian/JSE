"""The job market a lane searches, read from its search location (leaf).

The prompts were written for one market: a candidate in Australia searching
Australian boards. They call the model "an Australian career analyst", ask for
Australian English, and name ASX corporates and councils as the employer
landscape. A lane searching Manchester, UK inherited all of that, so the model
judged a British ad against Australian norms.

Rather than rewrite every prompt, the wording is localised on the way out:
the bridge records the lane's search location (``preferred_location``) for the
command it is running, and the LLM call layer passes every message through
``localise_messages``. For an Australian location, or none, the prompts are
left exactly as written, byte for byte, so the main install's prompt-prefix
cache and scoring are untouched. For anywhere else the Australian framing is
replaced with the lane's market and a SEARCH MARKET line is added to the
system message.

The location lives in a ContextVar rather than a module global. The bridge's
persistent worker runs one thread per request, and each new thread starts with
an empty context, so a lane's location can never leak into a request for a
different lane. Thread pools do not copy the context on their own; code that
fans an LLM call out to workers submits through ``run_in_context``.
"""
import contextlib
import contextvars
import re

_ACTIVE_LOCATION = contextvars.ContextVar("jse_search_location", default=None)

MARKET_LINE_PREFIX = "SEARCH MARKET:"

# Checked before the Australian test, so "Perth, Scotland" or "Newcastle, UK"
# are not read as Australian. "New South Wales" is masked first so its "Wales"
# can't make it British.
_COUNTRIES = (
    ("United Kingdom", "UK", "British English",
     r"\b(?:uk|u\.k\.|united kingdom|england|scotland|wales|northern ireland|great britain|britain|gb)\b"),
    ("Ireland", "Irish", "Irish English (British spelling)", r"\b(?:ireland|eire|éire)\b"),
    ("United States", "US", "American English",
     r"\b(?:usa|u\.s\.a\.?|u\.s\.|united states(?: of america)?|us)\b"),
    ("Canada", "Canadian", "Canadian English",
     r"\b(?:canada|british columbia|bc|ontario|quebec|québec|alberta|manitoba|saskatchewan|nova scotia|new brunswick)\b"),
    ("New Zealand", "New Zealand", "New Zealand English", r"\b(?:new zealand|nz|aotearoa|auckland|christchurch)\b"),
    ("India", "Indian", "Indian English", r"\bindia\b"),
    ("Singapore", "Singapore", "British English", r"\bsingapore\b"),
    ("South Africa", "South African", "South African English", r"\bsouth africa\b"),
)

_AUSTRALIA = re.compile(
    r"\b(?:australia|australian|vic|nsw|qld|tas|victoria|queensland|tasmania|northern territory|"
    r"melbourne|sydney|brisbane|perth|adelaide|canberra|hobart|darwin|geelong|ballarat|bendigo|"
    r"gold coast|sunshine coast|wollongong|newcastle|townsville|cairns|toowoomba|launceston)\b",
    re.IGNORECASE,
)

_ANYWHERE = re.compile(r"^(?:remote|anywhere|worldwide|global|international)\b", re.IGNORECASE)


def _clean(location):
    return " ".join(str(location or "").split())


def market_for(location):
    """The market a search location points at, or None to keep the prompts as written.

    None means Australian, blank, or unreadable as a place. Anything else gets
    a dict with the label to name in the prompt, an adjective (possibly empty)
    to stand in for "Australian", and the English variety to write in.
    """
    text = _clean(location)
    if not text:
        return None
    lower = text.lower()
    if re.search(r"\baustralia\b", lower):
        return None
    masked = re.sub(r"new south wales", " nsw ", lower)
    for country, adjective, english, pattern in _COUNTRIES:
        if re.search(pattern, masked):
            return {"label": text, "country": country, "adjective": adjective, "english": english}
    if _AUSTRALIA.search(masked):
        return None
    if _ANYWHERE.search(text):
        return {"label": "remote roles open to the candidate's location", "country": None, "adjective": "",
                "english": "the English spelling conventions of the employer's market"}
    return {"label": text, "country": None, "adjective": "",
            "english": f"the English spelling conventions used in {text}"}


def _city(market):
    return market["label"].split(",")[0].strip() or market["label"]


def _article_for(phrase):
    if re.match(r"(?:UK|US|Uni|Eu)", phrase):
        return "a"
    return "an" if phrase[:1].lower() in "aeiou" else "a"


def _rewrite(text, market):
    label = market["label"]
    adjective = market["adjective"]
    english = market["english"]
    upper_label = (market["country"] or label).upper()

    text = re.sub(
        r"Recognise Australian employer context \([^)]*\)",
        f"Recognise the employer landscape of the {label} market (listed corporates, professional-services firms, "
        "national and regional government, defence, universities, local government, recruiters acting for an "
        "undisclosed end client)",
        text,
    )
    text = re.sub(
        r"AUSTRALIAN CONTEXT TO RECOGNISE([^\n]*)\n- ASX-listed[^\n]*",
        lambda m: (
            f"{upper_label} CONTEXT TO RECOGNISE{m.group(1)}\n- Listed corporates, professional-services firms, "
            "national/regional government departments, defence contractors, universities, local government, "
            "utilities, health networks, not-for-profits."
        ),
        text,
    )
    text = re.sub(r"Australian English spelling", f"{english} spelling", text)
    text = re.sub(r"Australian English", english, text)
    text = re.sub(r"Australian title conventions", f"the job-title conventions used in {label}", text)
    text = re.sub(r"\(Australian convention\)", "(common convention)", text)
    text = re.sub(r"the Australian hidden job market", f"the hidden job market in {label}", text)
    text = re.sub(r"\bhybrid Melbourne\b", f"hybrid {_city(market)}", text)
    text = re.sub(r"\bVictorian government\b", "public-sector", text)

    def _with_article(match):
        rest = match.group(2)
        phrase = f"{adjective} {rest}" if adjective else rest
        article = _article_for(phrase)
        if match.group(1)[0].isupper():
            article = article.capitalize()
        return f"{article} {phrase}"

    text = re.sub(r"\b([Aa]n?) Australian (\w)", _with_article, text)
    text = re.sub(r"\bAustralian (?=\w)", f"{adjective} " if adjective else "", text)
    return text


def market_line(market):
    return (
        f"{MARKET_LINE_PREFIX} the candidate is searching for roles in {market['label']}. Judge employer context, "
        f"pay norms, job-title conventions and spelling for that market. Where anything above assumes another "
        f"country's conventions, this market's conventions win."
    )


def active_location():
    return _ACTIVE_LOCATION.get()


@contextlib.contextmanager
def use_location(location):
    """Run a block with this search location in force."""
    token = _ACTIVE_LOCATION.set(_clean(location) or None)
    try:
        yield
    finally:
        _ACTIVE_LOCATION.reset(token)


def run_in_context(executor, fn, *args, **kwargs):
    """executor.submit that carries the current search location into the worker."""
    return executor.submit(contextvars.copy_context().run, fn, *args, **kwargs)


def _resolve(location):
    return active_location() if location is None else location


def localise(text, location=None, add_market_line=False):
    """The prompt rewritten for the lane's market, or untouched for Australia."""
    market = market_for(_resolve(location))
    if market is None or not isinstance(text, str):
        return text
    text = _rewrite(text, market)
    if add_market_line and MARKET_LINE_PREFIX not in text:
        text = f"{text}\n\n{market_line(market)}"
    return text


def localise_messages(messages, location=None):
    """Chat messages localised for the lane's market. Returns the input list when nothing changes."""
    market = market_for(_resolve(location))
    if market is None or not messages:
        return messages
    localised = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message = {**message, "content": _rewrite(message["content"], market)}
        localised.append(message)
    if not any(
        isinstance(m, dict) and MARKET_LINE_PREFIX in str(m.get("content") or "") for m in localised
    ):
        target = next((m for m in localised if isinstance(m, dict) and m.get("role") == "system"), None)
        if target is None:
            target = next((m for m in reversed(localised) if isinstance(m, dict) and m.get("role") == "user"), None)
        if target is not None and isinstance(target.get("content"), str):
            target["content"] = f"{target['content']}\n\n{market_line(market)}"
    return localised


def letter_style(location=None):
    market = market_for(_resolve(location))
    return "us" if market and market["country"] == "United States" else ""


def letter_date(when, location=None):
    """Cover-letter date: "October 9, 2026" for a US search, "09 October 2026" otherwise."""
    if letter_style(location) == "us":
        return f"{when:%B} {when.day}, {when:%Y}"
    return when.strftime("%d %B %Y")


def letter_closing(location=None):
    return "Sincerely," if letter_style(location) == "us" else "Yours sincerely,"
