"""Currency- and locale-aware pay parsing.

Leaf layer. Imports nothing from `db/`, `llm/` or `bridge/`.

Everything scales off a per-currency reference wage. Y6,000,000 is an ordinary
annual salary; $6,000,000 is not. Hardcoded thresholds in one currency cannot
express that, so there are none here: every judgement about whether a number is
plausible is made relative to `REFERENCE_WAGE` for the currency in play.

The output separates two things the caller needs to keep apart:

- `base_min` / `base_max` are the figures **as quoted, in the quoted period**,
  so a contractor ad reads "AUD 1,300-1,400 (day rate)" rather than an
  annualised number nobody wrote down.
- `package_max` is the annualised top of the package, including superannuation
  where the ad puts it on top. This is the only figure comparable to a salary
  floor.

Two hard-won rules, both load-bearing:

1. Prose is only scanned with `strict=True`. Without that guard the parser
   invented a $54,000 contract rate for a role whose ad quotes no salary at all,
   and produced the same figure for two unrelated employers by reading a
   reference number and a date. That is the dangerous failure mode: it would
   have filtered out the best role on the list.
2. Only an explicitly stated period is trusted enough to act on. An inferred
   period is capped below the caller's confidence threshold, so a guess about
   money can raise a question but can never discard a job.
"""

import math
import re

# Approximate full-time gross annual wage per currency. These are scale
# references, not economics: they only ever appear as a ratio, so being 20% out
# changes nothing. Add a currency here and the whole module works in it.
REFERENCE_WAGE = {
    "AUD": 90000, "NZD": 78000, "USD": 62000, "CAD": 65000, "GBP": 37000,
    "EUR": 42000, "CHF": 85000, "SEK": 480000, "NOK": 620000, "DKK": 460000,
    "ISK": 8000000, "PLN": 90000, "CZK": 500000, "HUF": 6000000, "RON": 90000,
    "JPY": 4500000, "CNY": 120000, "KRW": 45000000, "TWD": 700000,
    "HKD": 350000, "SGD": 70000, "MYR": 60000, "THB": 350000, "PHP": 400000,
    "IDR": 60000000, "VND": 200000000, "INR": 700000, "PKR": 1200000,
    "ZAR": 400000, "NGN": 6000000, "KES": 900000, "AED": 180000,
    "SAR": 150000, "QAR": 180000, "ILS": 160000, "TRY": 400000,
    "BRL": 45000, "MXN": 180000, "ARS": 8000000, "CLP": 12000000,
    "COP": 30000000, "RUB": 900000, "UAH": 300000,
}
DEFAULT_REFERENCE_WAGE = 60000

COUNTRY_CURRENCY = {
    "au": "AUD", "nz": "NZD", "us": "USD", "ca": "CAD", "gb": "GBP", "uk": "GBP",
    "ie": "EUR", "de": "EUR", "fr": "EUR", "es": "EUR", "it": "EUR", "nl": "EUR",
    "be": "EUR", "at": "EUR", "pt": "EUR", "fi": "EUR", "gr": "EUR", "sk": "EUR",
    "si": "EUR", "ee": "EUR", "lv": "EUR", "lt": "EUR", "lu": "EUR", "cy": "EUR",
    "mt": "EUR", "hr": "EUR", "ch": "CHF", "se": "SEK", "no": "NOK", "dk": "DKK",
    "is": "ISK", "pl": "PLN", "cz": "CZK", "hu": "HUF", "ro": "RON",
    "jp": "JPY", "cn": "CNY", "kr": "KRW", "tw": "TWD", "hk": "HKD",
    "sg": "SGD", "my": "MYR", "th": "THB", "ph": "PHP", "id": "IDR",
    "vn": "VND", "in": "INR", "pk": "PKR", "za": "ZAR", "ng": "NGN",
    "ke": "KES", "ae": "AED", "sa": "SAR", "qa": "QAR", "il": "ILS",
    "tr": "TRY", "br": "BRL", "mx": "MXN", "ar": "ARS", "cl": "CLP",
    "co": "COP", "ru": "RUB", "ua": "UAH",
}

# Prefixed dollar variants must be tried before the bare symbol.
PREFIXED_DOLLAR = {
    "a$": "AUD", "au$": "AUD", "nz$": "NZD", "us$": "USD", "c$": "CAD",
    "ca$": "CAD", "s$": "SGD", "hk$": "HKD", "r$": "BRL", "sg$": "SGD",
}
SYMBOL_CURRENCY = {
    "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR",
    "₩": "KRW", "₪": "ILS", "₽": "RUB", "₺": "TRY",
    "₱": "PHP", "฿": "THB", "₫": "VND", "₿": "BTC",
}

# Working days and hours behind an annualisation. Deliberately conservative:
# 260 days is every weekday with no leave, which is how contract rates are
# quoted, and 1950 hours is 37.5 x 52.
ANNUALISE = {"year": 1.0, "month": 12.0, "week": 52.0, "day": 260.0, "hour": 1950.0}

# How often a job ad quotes pay in each period, given it did not say which.
# Weekly quoting is vanishingly rare; daily is not. Without this, "$1300 -
# $1400" inferred *weekly*, because a contractor day rate annualises well above
# a median wage and weekly happened to land closest to it.
PERIOD_PRIOR = {"year": 0.70, "day": 0.16, "hour": 0.12, "month": 0.02, "week": 0.001}

# Spread of real salaries around the reference wage, in log space. 0.45 puts one
# standard deviation at roughly 1.6x, which is about right for a job board.
_LOG_SIGMA = 0.45

# Outside this band of the reference wage a reading is not a salary. Wide on
# purpose at the top, because partner-track and executive roles are real. The
# floor is set where it is because the scrapers leave truncated values like "$8"
# and "$4" in the salary column: annualised at full-time hours, a quarter of the
# reference wage is already below any lawful rate, whatever the currency.
_PLAUSIBLE_BAND = (0.25, 12.0)

# Australian superannuation guarantee. Only used when an ad says "plus super"
# without naming a rate; any stated rate wins, including the 17% common in
# university awards.
DEFAULT_SUPER_RATE = 0.12

_PERIOD_PATTERNS = (
    ("year", r"(?:per\s+annum|p\.?\s?a\.?(?![a-z])|per\s+year|annually|annualis(?:ed|ed)|/\s*(?:yr|year|annum)|\bp\.?y\.?\b|yearly|per\s+ann)"),
    ("month", r"(?:per\s+month|monthly|/\s*(?:mth|month|mo)\b|\bpcm\b|p\.?\s?m\.?(?=\s|$))"),
    ("week", r"(?:per\s+week|weekly|/\s*(?:wk|week)\b|\bp\.?w\.?(?![a-z]))"),
    ("day", r"(?:per\s+day|daily|day\s+rate|/\s*day\b|per\s+diem|\ba\s+day\b)"),
    ("hour", r"(?:per\s+hour|hourly|/\s*(?:hr|hour)\b|\bp\.?\s?h\b|an\s+hour|\bph\b)"),
)

_SALARY_CUE = re.compile(
    r"(?:salar|remunerat|\bpay\b|\bpaying\b|\bwage|\bpackage\b|compensation|"
    r"\brate\b|\bearn|superannuat|\bsuper\b|total\s+reward|base\s+pay|"
    r"classification|\bband\b|\bplus\s+super)",
    re.I,
)
# Numbers that live next to these are identifiers, not money.
_NOT_MONEY_BEFORE = re.compile(
    r"(?:ref(?:erence)?|position\s+(?:no|number|id)|req(?:uisition)?|vacancy|"
    r"job\s*(?:no|id|number)|\bid\b|abn|acn|phone|tel|fax|po\s*box|"
    r"clause|section|award)\W{0,6}$",
    re.I,
)

_SUPER_INCLUSIVE = re.compile(
    r"(?:incl(?:uding|usive|\.)?|\binc\b)[^.;]{0,24}?super", re.I)
_SUPER_ON_TOP = re.compile(
    r"(?:\+|plus|excl(?:uding|usive|\.)?|\bex\b|on\s+top\s+of)[^.;]{0,24}?super", re.I)
_SUPER_RATE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%[^.;]{0,20}?super", re.I)

_MULTIPLIER = {
    "k": 1e3, "m": 1e6, "mn": 1e6, "b": 1e9,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "lpa": 1e5,
    "crore": 1e7, "crores": 1e7, "cr": 1e7,
}
_MULTIPLIER_ALT = "|".join(sorted(_MULTIPLIER, key=len, reverse=True))
_CODE_ALT = "|".join(sorted(set(REFERENCE_WAGE) | set(PREFIXED_DOLLAR), key=len, reverse=True))
_SYMBOLS = "".join(SYMBOL_CURRENCY) + "$"

_MONEY = re.compile(
    r"(?P<code>" + _CODE_ALT.replace("$", r"\$") + r")?\s*"
    r"(?P<sym>[" + re.escape(_SYMBOLS) + r"])?\s*"
    r"(?P<num>\d[\d.,  ' ]{0,18}\d|\d)"
    r"\s*(?P<mult>" + _MULTIPLIER_ALT + r")?",
    re.I,
)
_RANGE_JOIN = re.compile(r"^\s*(?:-|–|—|to|and|up\s+to|~)\s*$", re.I)
_UPPER_ONLY = re.compile(r"(?:up\s+to|below|under|max(?:imum)?(?:\s+of)?)\s*$", re.I)


def currency_for(country_code=None, hint=None):
    """Resolve the currency to read numbers in, preferring an explicit hint."""
    hint = str(hint or "").strip().upper()
    if hint in REFERENCE_WAGE:
        return hint
    return COUNTRY_CURRENCY.get(str(country_code or "").strip().lower())


def reference_wage(currency):
    return REFERENCE_WAGE.get(str(currency or "").upper(), DEFAULT_REFERENCE_WAGE)


def _to_number(raw, mult=None):
    """Parse a grouped decimal number without knowing the writer's locale.

    Handles "120,000", "120.000" (European), "45.000,50", "12,00,000" (Indian
    lakh grouping) and thin-space grouping, by deciding which separator is the
    decimal point from where it sits rather than from a locale setting.
    """
    text = re.sub(r"[  ' ]", "", str(raw or "")).strip()
    if not text:
        return None
    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        # Whichever appears last is the decimal separator.
        dec = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        tail = text.rsplit(sep, 1)[1]
        if text.count(sep) > 1 or len(tail) == 3:
            # Repeated separators, or a trailing group of exactly three, is
            # grouping: "1,234,567", "12,00,000", "120.000".
            text = text.replace(sep, "")
        else:
            text = text.replace(sep, ".")
    try:
        value = float(text)
    except ValueError:
        return None
    if mult:
        value *= _MULTIPLIER[mult.lower()]
    return value


def _currency_of(match, default):
    code = (match.group("code") or "").strip().lower()
    if code:
        if code in PREFIXED_DOLLAR:
            return PREFIXED_DOLLAR[code], True
        return code.upper(), True
    sym = match.group("sym") or ""
    if sym in SYMBOL_CURRENCY:
        return SYMBOL_CURRENCY[sym], True
    if sym == "$":
        # A bare dollar sign names no country. The lane's currency decides, and
        # if the lane has none we still know it is dollars of some kind.
        return (default or "USD"), False
    return default, False


def _explicit_period(text, start, end):
    """Look for a stated period just after the amount, then just before it.

    Nearest wins rather than first-listed: "hourly rate of $45" states its
    period ahead of the number, and "$120,000 per annum, reviewed monthly" must
    not be read as a monthly wage.
    """
    after = text[end:end + 42].lower()
    best = None
    for period, pattern in _PERIOD_PATTERNS:
        found = re.search(pattern, after)
        if found and found.start() <= 24 and (best is None or found.start() < best[0]):
            best = (found.start(), period)
    if best:
        return best[1]
    before = text[max(0, start - 42):start].lower()
    for period, pattern in _PERIOD_PATTERNS:
        for found in re.finditer(pattern, before):
            distance = len(before) - found.end()
            if best is None or distance < best[0]:
                best = (distance, period)
    return best[1] if best else None


def _infer_period(amount, currency):
    """Choose a pay period for a bare number.

    Posterior over periods: prior x how well the annualised figure fits the
    reference wage. Returns (period, share of posterior mass).
    """
    ref = reference_wage(currency)
    scores = {}
    for period, factor in ANNUALISE.items():
        annual = amount * factor
        if annual <= 0:
            continue
        fit = math.exp(-((math.log(annual / ref)) ** 2) / (2 * _LOG_SIGMA ** 2))
        scores[period] = PERIOD_PRIOR[period] * fit
    total = sum(scores.values())
    if not total:
        return None, 0.0
    period = max(scores, key=scores.get)
    return period, scores[period] / total


def _super_treatment(text):
    """Return (rate, mode) where mode is "on_top", "inclusive" or None."""
    rate_match = _SUPER_RATE.search(text)
    rate = float(rate_match.group(1)) / 100.0 if rate_match else DEFAULT_SUPER_RATE
    if _SUPER_INCLUSIVE.search(text):
        return rate, "inclusive"
    if _SUPER_ON_TOP.search(text):
        return rate, "on_top"
    return rate, None


def _candidates(text, default_currency, strict):
    """Money-shaped tokens, in order, with the junk filtered out."""
    found = []
    for match in _MONEY.finditer(text):
        start, end = match.span()
        # Trim the leading whitespace the optional groups may have absorbed.
        while start < end and text[start].isspace():
            start += 1
        if text[end:end + 1] == "%":
            continue  # "17% superannuation" is a rate, not an amount
        if re.match(r"[/-]\d", text[end:end + 2]) and not (match.group("sym") or match.group("code")):
            # Inside a date or a serial number. The separator must be flush
            # against the digits: "12/03/2026" is a date, "$1300 - $1400" is a
            # range, and an earlier version of this guard ate the low end of
            # every range it saw.
            continue
        if _NOT_MONEY_BEFORE.search(text[max(0, start - 24):start]):
            continue
        amount = _to_number(match.group("num"), match.group("mult"))
        if amount is None or amount <= 0:
            continue
        currency, explicit = _currency_of(match, default_currency)
        marked = bool(match.group("code") or match.group("sym") or match.group("mult"))
        if not marked and 1900 <= amount <= 2100 and float(amount).is_integer():
            continue  # a year, not a wage
        if strict and not (match.group("code") or match.group("sym")):
            continue
        found.append({
            "start": start, "end": end, "amount": amount,
            "currency": currency, "explicit_currency": explicit, "marked": marked,
        })
    return found


def _group(text, tokens):
    """Fold "X - Y" and "between X and Y" into single ranges."""
    groups, i = [], 0
    while i < len(tokens):
        token = dict(tokens[i])
        token["low"], token["high"] = token["amount"], token["amount"]
        token["is_range"] = False
        if i + 1 < len(tokens):
            nxt = tokens[i + 1]
            gap = text[token["end"]:nxt["start"]]
            if _RANGE_JOIN.match(gap) and nxt["amount"] >= token["amount"]:
                token["high"] = nxt["amount"]
                token["end"] = nxt["end"]
                token["is_range"] = True
                token["explicit_currency"] = token["explicit_currency"] or nxt["explicit_currency"]
                i += 1
        if _UPPER_ONLY.search(text[max(0, token["start"] - 16):token["start"]]):
            token["low"] = token["high"]
        groups.append(token)
        i += 1
    return groups


def _pick(text, groups, strict):
    """Choose the group most likely to be the advertised pay.

    Prefer one that states its period, then one carrying a currency marker,
    then the first. In strict mode a salary cue must sit near it, which is the
    guard that stops reference numbers and dates being read as money.
    """
    best, best_rank = None, None
    for group in groups:
        period = _explicit_period(text, group["start"], group["end"])
        if strict:
            window = text[max(0, group["start"] - 70):group["end"] + 40]
            if not (_SALARY_CUE.search(window) or period):
                continue
        rank = (period is not None, group["marked"], group["is_range"])
        if best_rank is None or rank > best_rank:
            best, best_rank = dict(group, period=period), rank
    return best


def summarise(text, country_code=None, currency_hint=None, strict=False):
    """Read pay out of a string. Returns None when there is nothing to read.

    `strict=True` is required for free prose: it demands a currency marker and
    a nearby salary cue, because an ad that quotes no salary at all will still
    happily offer up a reference number.
    """
    text = str(text or "")
    if not text.strip():
        return None
    default_currency = currency_for(country_code, currency_hint)
    tokens = _candidates(text, default_currency, strict)
    if not tokens:
        return None
    chosen = _pick(text, _group(text, tokens), strict)
    if not chosen:
        return None

    currency = chosen["currency"]
    period, source, share = chosen["period"], "explicit", 1.0
    if not period:
        period, share = _infer_period(chosen["high"], currency)
        source = "inferred"
    if not period:
        return None

    annual_high = chosen["high"] * ANNUALISE[period]
    ref = reference_wage(currency)
    if not (_PLAUSIBLE_BAND[0] * ref <= annual_high <= _PLAUSIBLE_BAND[1] * ref):
        return None

    rate, mode = _super_treatment(text)
    low, high = chosen["low"], chosen["high"]
    uplift = 1.0
    if mode == "inclusive":
        # The quoted figure is the package; the base sits underneath it.
        low, high = low / (1 + rate), high / (1 + rate)
    elif mode == "on_top":
        uplift = 1 + rate
    package_annual = annual_high * uplift

    if source == "explicit":
        confidence = 0.8 + (0.05 if chosen["explicit_currency"] else 0.0) \
            + (0.05 if chosen["is_range"] else 0.0)
    else:
        # Capped below the caller's acting threshold on purpose: an inferred
        # period may raise a question about pay, never settle one.
        confidence = 0.55 * (0.4 + 0.6 * share)
    if confidence < 0.3:
        return None

    return {
        "base_min": int(round(low)),
        "base_max": int(round(high)),
        "package_max": int(round(package_annual)),
        "annual_min": int(round(chosen["low"] * ANNUALISE[period] * uplift)),
        "currency": currency,
        "currency_explicit": chosen["explicit_currency"],
        "period_quoted": period,
        "period_source": source,
        "super_mode": mode,
        "super_rate": rate if mode else None,
        "is_contract_rate": period in ("hour", "day"),
        "confidence": round(confidence, 2),
    }
