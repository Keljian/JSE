"""Commute model: pure coordinate math.

Leaf layer. Imports nothing from `db/`, `llm/` or `bridge/`.

Nothing in this module knows what a suburb is called in any country. An earlier
attempt hardcoded Melbourne suburb sets and bundled a postcode CSV; JSE ships to
other people, so that was thrown away. Every place name here arrives from the
user's settings or from a geocoder, never from a table in the source.

Three signals, all derived from coordinates:

- `distance_km`     great-circle, home to job.
- `sector`          compass bearing from the *metro centre* to the job, bucketed
                    to eight points. The user's "eastern and northern suburbs"
                    becomes "E,NE,N" in settings, not a constant in code.
- `crosses_centre`  detour ratio. If travelling via the centre is barely longer
                    than going direct, the centre is on the path. A cross-town
                    commute is far slower than its straight-line distance
                    suggests, and this holds in any city without knowing its
                    road network.

Anything unresolved yields "unknown", which never blocks. A geocoder outage is a
gap in our data, not a fact about the job.
"""

import math
import re
import time

SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

EARTH_RADIUS_KM = 6371.0088
KM_PER_MILE = 1.609344

# A job this close to the metro centre *is* the centre. Without this, every CBD
# role read as "crosses the centre" and was penalised for arriving at its own
# destination.
DEFAULT_CENTRE_RADIUS_KM = 6.0

# Travelling via the centre may be at most this much longer than going direct
# before we stop calling the centre "on the way". 1.0 is exactly on the path.
CROSS_DETOUR_RATIO = 1.25

# Cross-town travel time penalty, applied to distance for verdict purposes only.
# The reported distance_km stays honest.
CROSS_PENALTY = 1.35

# A hybrid role is commuted fewer days a week, so a longer trip is tolerable.
HYBRID_RADIUS_BONUS = 1.3

PRECISE_PRECISIONS = frozenset({"address", "street", "suburb", "town", "postcode"})

# A centroid for one of these covers hundreds of kilometres. The distance to it
# is not a commute and is not recorded: "somewhere in Victoria" is a gap in our
# data about the job, and gaps pass.
UNUSABLE_PRECISIONS = frozenset({"region", "state", "country"})

# How far past the limit a job must sit before an imprecise geocode may block
# it. "Melbourne VIC" resolves to a city centroid that can be 30km from the
# actual office, so a job 50km out on a city-level match is a question, not a
# fact. A job in another state is still 700km out after the slack, and setting
# that one aside needs no more precision than we have.
_DEFAULT_SLACK_KM = 25.0
_BLOCK_SLACK_KM = {
    "address": 0.0, "street": 0.0, "suburb": 0.0, "town": 0.0, "postcode": 0.0,
    "city": 25.0,
}

_FULLY_REMOTE = re.compile(
    r"(?:\b(?:fully|100%|completely|entirely|permanently)\s+remote\b"
    r"|\bremote[\s-]*(?:first|only)\b"
    r"|\bwork\s+from\s+(?:home|anywhere)\s+(?:full[\s-]*time|permanently)\b"
    r"|\banywhere\s+in\s+\w+)",
    re.I,
)
_HYBRID = re.compile(
    r"(?:\bhybrid\b"
    r"|\bwork\s+from\s+home\b"
    r"|\bwfh\b"
    r"|\b\d\s*days?\s+(?:a|per)\s+week\s+(?:in|at|from)\s+(?:the\s+)?(?:office|site)\b"
    r"|\bflexible\s+(?:work|working)\s+arrangements?\b)",
    re.I,
)
_REMOTE_LOCATION = re.compile(r"\b(remote|anywhere|work\s+from\s+home|wfh)\b", re.I)

# Stripped from scraped location strings before geocoding. Work-mode and
# employment-type words are generic English, not place names, so removing them
# does not smuggle geography into the source.
_LOCATION_NOISE = re.compile(
    r"\b(?:full[\s-]?time|part[\s-]?time|casual|permanent|contract|temporary|"
    r"fixed[\s-]?term|ongoing|hybrid|remote|on[\s-]?site|onsite|wfh|"
    r"work\s+from\s+home|multiple\s+locations|various\s+locations)\b",
    re.I,
)


def km(value, unit="km"):
    """Interpret a user-entered distance in their chosen unit as kilometres.

    The database and this module are km-native; `distance_unit` is a display
    preference, so it is converted once on the way in.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value * KM_PER_MILE if str(unit or "km").lower() in ("mi", "mile", "miles") else value


def haversine_km(a, b):
    """Great-circle distance between two (lat, lon) pairs."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(origin, target):
    """Initial compass bearing from origin to target, 0-360 with 0 = north."""
    lat1, lat2 = math.radians(origin[0]), math.radians(target[0])
    dlon = math.radians(target[1] - origin[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def sector_of(origin, target):
    """Bucket a bearing to one of eight compass points."""
    if not origin or not target:
        return None
    index = int((bearing_deg(origin, target) + 22.5) % 360.0 // 45.0)
    return SECTORS[index]


def normalise_query(text):
    """Cache key for a location string.

    Case and punctuation noise must not split the cache: a sweep that resolves
    40 jobs to 20 distinct strings only stays cheap if "Melbourne, VIC" and
    "melbourne vic" are the same key.
    """
    text = str(text or "")
    text = text.split("|")[0]
    text = _LOCATION_NOISE.sub(" ", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"[^\w\s,'/-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?:,\s*)+", ", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,-/")
    return text.lower()


def _variants(query):
    """Progressively coarser forms to try when the full string will not resolve.

    Scraped locations often lead with a venue or campus name the geocoder has
    never heard of. Dropping the leading component usually leaves something it
    knows. Bounded to two attempts, and every attempt is cached.
    """
    yield query
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) > 2:
        yield ", ".join(parts[-2:])


class GeocodeCache:
    """Cache-first geocoding with polite rate limiting.

    `store` is any mapping; the app passes a SQLite-backed one, tests pass a
    dict. Negative results are stored as None so an unresolvable string is not
    re-requested against a 1-request-per-second public endpoint on every sweep,
    forever.
    """

    def __init__(self, store=None, geocoder=None, min_interval=1.05):
        self.store = {} if store is None else store
        self.geocoder = geocoder
        self.min_interval = float(min_interval)
        self._last_call = 0.0
        self.hits = 0
        self.misses = 0
        self.failures = 0

    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _fetch(self, key):
        if self.geocoder is None:
            return None
        self._throttle()
        try:
            return self.geocoder(key)
        except Exception:
            # A geocoder outage degrades screening to "unknown". It never fails
            # a sweep and never rejects a job.
            return None

    def lookup(self, text):
        """Resolve a location string to a record, or None."""
        query = normalise_query(text)
        if not query:
            return None
        for variant in _variants(query):
            try:
                cached = self.store[variant]
            except KeyError:
                pass
            else:
                if cached:
                    self.hits += 1
                    return cached
                continue  # cached negative: try the next variant, not the network
            self.misses += 1
            result = self._fetch(variant)
            self.store[variant] = result
            if result and result.get("lat") is not None:
                return result
            self.failures += 1
        return None


class CommuteModel:
    """Screens one lane's jobs on travel. Build once per sweep: the resolved
    home coordinates, the discovered metro centre and the geocode cache are all
    reused across the batch."""

    def __init__(self, home, centre=None, cache=None, preferred_km=25.0,
                 max_km=45.0, accepted_sectors=None,
                 centre_radius_km=DEFAULT_CENTRE_RADIUS_KM, home_country=None):
        self.home = home
        self.centre = centre
        self.home_country = (home_country or "").lower() or None
        self.cache = cache if cache is not None else GeocodeCache()
        self.preferred_km = float(preferred_km or 0) or 25.0
        self.max_km = float(max_km or 0) or 45.0
        self.accepted_sectors = set(accepted_sectors) if accepted_sectors else None
        self.centre_radius_km = float(centre_radius_km)
        self.home_sector = sector_of(centre, home) if centre else None

    # -- work mode ---------------------------------------------------------

    def _work_mode(self, location_text, ad_text):
        blob = f"{location_text or ''}\n{ad_text or ''}"
        if _FULLY_REMOTE.search(blob):
            return "remote"
        if _REMOTE_LOCATION.search(str(location_text or "")):
            return "remote"
        # Deliberately scanned against the ad, never against our own `analysis`
        # column: that field is our prose *about* hybrid arrangements, so
        # matching it made every job look flexible.
        if _HYBRID.search(blob):
            return "hybrid"
        return "onsite"

    # -- placement ---------------------------------------------------------

    def _place(self, location_text, employer):
        """Resolve the job to coordinates, reading the advertiser when the
        posted location is too coarse to be useful.

        Melton City Council advertises its jobs as "Melbourne VIC". Screening on
        the posted location alone put a 40km-away employer at the top of the
        list, because the city centroid sat 8km from home. The employer name is
        the more precise statement of where the work is.
        """
        hit = self.cache.lookup(location_text)
        precision = (hit or {}).get("precision") or "unknown"
        if hit and precision in PRECISE_PRECISIONS:
            return hit, precision, "location"
        if not employer:
            return hit, precision, "location"
        emp = self.cache.lookup(employer)
        if not emp or emp.get("precision") not in PRECISE_PRECISIONS:
            return hit, precision, "location"
        # An employer name is not a place name, and geocoders will happily match
        # one to a street or hamlet anywhere on earth. A global consultancy
        # advertising in "Sydney or Melbourne or Tokyo" resolved to a European
        # address 16,614km away and the job was set aside on it.
        if hit:
            same_country = (not emp.get("country_code") or not hit.get("country_code")
                            or emp["country_code"] == hit["country_code"])
            near = haversine_km((hit["lat"], hit["lon"]), (emp["lat"], emp["lon"])) <= 150.0
            if not (same_country and near):
                return hit, precision, "location"
        elif self.home_country and emp.get("country_code") \
                and emp["country_code"] != self.home_country:
            # Nothing posted to cross-check against, so the user's own country is
            # the only anchor left. A match outside it is a coincidence of names.
            return hit, precision, "location"
        return emp, emp.get("precision") or "unknown", "employer"

    # -- verdict -----------------------------------------------------------

    def evaluate(self, location_text, ad_text="", employer=None):
        """Return a commute verdict for one job.

        Verdicts: "pass", "review", "blocked", "unknown". Only "blocked" stops a
        job reaching the model, and only a precisely located job beyond the
        maximum radius earns it.
        """
        out = {
            "verdict": "unknown", "score_delta": 0, "reason": "",
            "distance_km": None, "sector": None, "crosses_centre": None,
            "precision": None, "work_mode": None, "source": None,
        }
        mode = out["work_mode"] = self._work_mode(location_text, ad_text)
        if mode == "remote":
            out.update(verdict="pass", score_delta=8, reason="Remote role")
            return out

        hit, precision, source = self._place(location_text, employer)
        out["precision"] = precision
        out["source"] = source
        if not hit or hit.get("lat") is None:
            out["reason"] = "Location not resolved"
            return out
        if precision in UNUSABLE_PRECISIONS:
            out["reason"] = f"Location too imprecise to measure ({precision})"
            return out

        point = (float(hit["lat"]), float(hit["lon"]))
        distance = round(haversine_km(self.home, point), 1)
        out["distance_km"] = distance
        out["sector"] = sector = sector_of(self.centre, point) if self.centre else None

        effective = distance
        limit = self.max_km * (HYBRID_RADIUS_BONUS if mode == "hybrid" else 1.0)
        notes = []

        crosses = self._crosses_centre(point, distance)
        out["crosses_centre"] = crosses
        if crosses:
            effective *= CROSS_PENALTY
            notes.append("crosses the centre")

        # Sector is a tie-breaker, not a veto. Applying it everywhere blocked a
        # role 23km away while passing one at 38km, which is the wrong way round.
        wrong_sector = bool(
            self.accepted_sectors and sector and sector not in self.accepted_sectors
            and distance > self.preferred_km
        )
        if wrong_sector:
            notes.append(f"{sector} of centre")

        detail = f"{distance:g}km"
        if source == "employer":
            detail += " (via employer)"
        if mode == "hybrid":
            detail += ", hybrid"

        if effective <= self.preferred_km:
            out.update(verdict="pass", score_delta=6)
        elif effective <= limit:
            # Wrong side of town *and* through the centre is the combination
            # that actually hurts; either one alone is only a nudge.
            out.update(verdict="review" if (wrong_sector and crosses) else "pass",
                       score_delta=-4 if (wrong_sector or crosses) else 0)
        else:
            slack = _BLOCK_SLACK_KM.get(precision, _DEFAULT_SLACK_KM)
            # Blocking is decided on the real distance, never on the cross-town
            # penalty. The user said this many kilometres is acceptable; a
            # modelled travel-time uplift may raise a question about a trip
            # inside that radius but must not overrule the number they set.
            if distance > limit + slack:
                out.update(verdict="blocked", score_delta=-15)
            else:
                out.update(verdict="review", score_delta=-8)
                if slack:
                    notes.append("imprecise location")

        reason = {
            "pass": "Commute", "review": "Commute uncertain:",
            "blocked": f"Beyond {limit:g}km commute limit:",
        }.get(out["verdict"], "Commute")
        out["reason"] = f"{reason} {detail}" + (f" — {', '.join(notes)}" if notes else "")
        return out

    def _crosses_centre(self, point, direct_km):
        """True when the centre lies essentially on the path from home to job.

        A job *at* the centre is a destination, not a crossing, and a home in
        the centre crosses nothing — both handled by centre_radius_km.
        """
        if not self.centre or direct_km < 1.0:
            return False
        home_leg = haversine_km(self.home, self.centre)
        job_leg = haversine_km(self.centre, point)
        if home_leg <= self.centre_radius_km or job_leg <= self.centre_radius_km:
            return False
        return (home_leg + job_leg) / direct_km <= CROSS_DETOUR_RATIO
