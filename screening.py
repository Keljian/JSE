"""Deterministic pre-LLM screening: commute and pay.

Sits between the pure logic modules (`geo`, `salary`) and the analysis
pipeline. Leaf layer by import rules: it may be imported by `llm/` and
`bridge/`, and imports neither.

Why this runs before triage rather than after: a job in another state is in
another state whatever a 3B model concludes, and screening it in plain Python
costs microseconds where an LLM call costs seconds. The saving compounds over a
200-job sweep.

Two rules the pipeline depends on, both deliberate:

1. A blocked job is never deleted and never hidden. It keeps its row, its
   verdict and a human-readable reason, and is only skipped for analysis and
   omitted from the shortlist. The brief this engine was built to is explicit
   that false negatives cost more than false positives, so a role that
   disappears with no explanation is the worst possible outcome.
2. Anything unresolved passes. A geocoder outage, an unparseable location, a
   missing salary — all yield "unknown", which never blocks. Absence of
   evidence about a job is a gap in our data, not a fact about the job.
"""

import geo
import salary as salary_mod

# Screening does not decide; it nudges an existing score and, at the extreme,
# defers a role. Kept well under the LLM's own 0-100 range so a strong match
# is never buried by geography alone.
BLOCKED_VERDICTS = ("blocked",)


def _f(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sectors(raw):
    parts = [p.strip().upper() for p in str(raw or "").replace(";", ",").split(",")]
    return {p for p in parts if p in geo.SECTORS} or None


class Screener:
    """Screens jobs for one lane. Build once per sweep, not once per job:
    the geocode cache, the resolved home coordinates and the discovered metro
    centre are all reused across the batch."""

    def __init__(self, lane_settings, store=None, geocoder=None, log=None):
        self.log = log or (lambda *a, **k: None)
        self.settings = lane_settings or {}
        self.enabled = bool(self.settings.get("commute_screening_enabled", True))
        self.cache = geo.GeocodeCache(store=store, geocoder=geocoder)
        self.salary_floor = int(_f(self.settings.get("salary_floor"), 0))
        self.currency = str(self.settings.get("salary_currency") or "").strip().upper() or None
        self.model = None
        self.home_country = None
        if self.enabled:
            self._build_model()

    def _build_model(self):
        home_text = (str(self.settings.get("home_location") or "").strip()
                     or str(self.settings.get("preferred_location") or "").strip())
        if not home_text:
            self.log("Commute screening off: no home location set.")
            self.enabled = False
            return
        home = self.cache.lookup(home_text)
        if not home or home.get("lat") is None:
            # Do not fail the sweep over this. Screening simply stays off and
            # every job passes, which is the behaviour before this feature.
            self.log(f"Commute screening off: could not resolve home '{home_text}'.")
            self.enabled = False
            return
        home_pt = (float(home["lat"]), float(home["lon"]))
        self.home_country = home.get("country_code") or None
        centre = self._discover_centre(home_pt, home)
        # `distance_unit` is a display preference; the model and the database
        # are km-native, so a user working in miles is converted once, here.
        unit = self.settings.get("distance_unit") or "km"
        self.model = geo.CommuteModel(
            home=home_pt,
            centre=centre,
            cache=self.cache,
            preferred_km=geo.km(_f(self.settings.get("preferred_commute_km"), 25), unit),
            max_km=geo.km(_f(self.settings.get("max_commute_km"), 45), unit),
            accepted_sectors=_sectors(self.settings.get("accepted_sectors")),
            home_country=self.home_country,
        )
        self.log(f"Commute screening anchored on {home_text}"
                 + (f", centre {centre[0]:.3f},{centre[1]:.3f}" if centre else ", no centre"))

    def _discover_centre(self, home_pt, home_record):
        """Find the metro centre without asking the user to name their city.

        Reverse-geocode home, take the city from the returned admin hierarchy,
        then forward-geocode that name. Both results are cached, so this costs
        two lookups once in the life of a profile.
        """
        city = home_record.get("city")
        if not city:
            try:
                from db import geocode as geocode_db
                rev = geocode_db.reverse(*home_pt)
                city = (rev or {}).get("city")
            except Exception:
                city = None
        if not city:
            return None
        hit = self.cache.lookup(city)
        if hit and hit.get("lat") is not None:
            return (float(hit["lat"]), float(hit["lon"]))
        return None

    @staticmethod
    def _reader(job):
        """Read a sqlite3.Row or a dict the same way, tolerating absent columns:
        a caller that selected four columns must not crash the screen."""
        if hasattr(job, "get"):
            return job.get
        return lambda k, d=None: job[k] if k in job.keys() else d

    def salary_reading(self, job):
        """Just the pay fields, for a backfill that must not disturb a commute
        result computed earlier against a geocoder."""
        get = self._reader(job)
        ad_text = " ".join(str(get(k) or "") for k in ("description", "pdf_text"))
        pay = self._screen_salary(get, ad_text)
        if not pay:
            return None
        return {key: pay[key] for key in
                ("salary_min", "salary_max", "salary_currency",
                 "salary_period", "salary_confidence")}

    def screen(self, job):
        """Return a screening verdict for one job row or dict."""
        get = self._reader(job)
        ad_text = " ".join(str(get(k) or "") for k in ("description", "pdf_text"))
        result = {
            "verdict": "pass", "score_delta": 0, "reasons": [],
            "commute_km": None, "commute_sector": None,
            "salary_min": None, "salary_max": None, "salary_currency": None,
            "salary_period": None, "salary_confidence": None,
        }

        if self.enabled and self.model is not None:
            # The advertiser is read alongside the posted location because the
            # employer name often carries the truer one: Melton City Council
            # advertises its jobs as "Melbourne VIC", and screening the posted
            # location alone ranked a 60km commute first.
            employer = (get("actual_company") or get("advertiser_company")
                        or get("company") or "")
            commute = self.model.evaluate(get("location"), ad_text, employer)
            result["commute_km"] = commute.get("distance_km")
            result["commute_sector"] = commute.get("sector")
            result["score_delta"] += commute.get("score_delta") or 0
            result["reasons"].append(commute.get("reason") or "")
            if commute["verdict"] in BLOCKED_VERDICTS:
                result["verdict"] = "blocked"
            elif commute["verdict"] == "review" and result["verdict"] == "pass":
                result["verdict"] = "review"

        pay = self._screen_salary(get, ad_text)
        if pay:
            result.update({k: pay[k] for k in
                           ("salary_min", "salary_max", "salary_currency",
                            "salary_period", "salary_confidence")})
            result["reasons"].append(pay["reason"])
            result["score_delta"] += pay["delta"]
            if pay["below_floor"]:
                # Only a confident, same-currency reading may block on pay. A
                # guess about money is not grounds for discarding a role.
                result["verdict"] = "blocked" if pay["confident"] else "review"

        result["reason"] = "; ".join(r for r in result["reasons"] if r)
        return result

    def _screen_salary(self, get, ad_text):
        country = self.home_country
        parsed = salary_mod.summarise(get("salary") or "", country_code=country,
                                      currency_hint=self.currency)
        if not parsed:
            # Prose is scanned in strict mode only. Without that guard it
            # invented a $54,000 contract rate for a role whose ad quotes no
            # salary at all, and produced the same figure for two unrelated
            # employers by reading reference numbers and dates.
            parsed = salary_mod.summarise(ad_text[:6000], country_code=country,
                                          currency_hint=self.currency, strict=True)
        if not parsed:
            return None
        top = parsed["package_max"]
        below = bool(self.salary_floor and top < self.salary_floor)
        comparable = (not self.currency) or parsed["currency"] == self.currency
        confident = below and comparable and parsed["confidence"] >= 0.6
        band = f"{parsed['currency'] or ''} {parsed['base_min']:,}-{parsed['base_max']:,}".strip()
        if parsed["is_contract_rate"]:
            band += f" ({parsed['period_quoted']} rate)"
        return {
            "salary_min": parsed["base_min"], "salary_max": parsed["base_max"],
            "salary_currency": parsed["currency"], "salary_period": parsed["period_quoted"],
            "salary_confidence": parsed["confidence"],
            "below_floor": below, "confident": confident,
            "delta": -12 if below else (4 if self.salary_floor else 0),
            "reason": band + (" — below floor" if below else ""),
        }


def build(lane_settings, log=None):
    """Construct a Screener wired to the app's cache and provider."""
    try:
        from db import geocode as geocode_db
        store = geocode_db.SqliteGeocodeStore()
        provider = geocode_db.resolve_provider(lane_settings.get("geocode_provider"))
    except Exception:
        store, provider = None, None
    return Screener(lane_settings, store=store, geocoder=provider, log=log)
