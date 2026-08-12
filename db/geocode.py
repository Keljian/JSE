"""Persistent geocode cache and provider adapters.

Layer: sits with `settings` (reads credentials, writes its own table). Imports
only from `connection`.

Why a cache and not just live lookups: a real sweep of 40 jobs resolved to 20
distinct location strings, and those same strings recur in every later sweep.
Steady state is therefore almost entirely cache hits, which is what makes a
1-request-per-second public geocoder viable for a 200-job run.

Failed lookups are cached too, as a row with NULL coordinates. Without that, a
location string the provider cannot resolve is retried on every sweep forever.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .connection import get_db_connection

USER_AGENT = "JSE-JobSearchEngine/1.0 (local desktop app)"

# Nominatim's address hierarchy, coarsest last. Used to score how precisely a
# result pins a location: "Melbourne VIC" resolves to a city centroid that may
# sit 30km from the actual office, and must not be treated as confidently as a
# street address.
_PRECISION_BY_TYPE = {
    "house": "address", "building": "address", "residential": "address",
    # A named institution or workplace is as precise as a street address, and
    # employer names resolve to these far more often than to a house number.
    "amenity": "address", "office": "address", "university": "address",
    "college": "address", "school": "address", "hospital": "address",
    "isolated_dwelling": "address",
    "road": "street", "industrial": "street", "commercial": "street",
    "retail": "street",
    "neighbourhood": "suburb", "suburb": "suburb",
    "quarter": "suburb", "city_district": "suburb", "borough": "suburb",
    "postcode": "postcode", "hamlet": "town",
    "village": "town", "town": "town", "municipality": "town",
    "city": "city", "county": "region", "state_district": "region",
    "state": "state", "province": "state", "country": "country",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SqliteGeocodeStore:
    """A dict-like view over the geocode_cache table.

    Implements only the mapping operations `geo.GeocodeCache` uses, so the
    commute model never learns that a database exists and stays testable with
    a plain dict.
    """

    def __contains__(self, key):
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM geocode_cache WHERE query = ?", (key,)
            ).fetchone()
        return row is not None

    def __getitem__(self, key):
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT lat, lon, precision, country_code, display_name "
                "FROM geocode_cache WHERE query = ?", (key,)
            ).fetchone()
        if row is None:
            raise KeyError(key)
        if row["lat"] is None:
            return None  # cached negative result
        return {
            "lat": row["lat"], "lon": row["lon"],
            "precision": row["precision"] or "unknown",
            "country_code": row["country_code"] or "",
            "display_name": row["display_name"] or "",
        }

    def __setitem__(self, key, value):
        value = value or {}
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO geocode_cache
                    (query, lat, lon, precision, country_code, display_name,
                     provider, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    lat = excluded.lat, lon = excluded.lon,
                    precision = excluded.precision,
                    country_code = excluded.country_code,
                    display_name = excluded.display_name,
                    provider = excluded.provider,
                    fetched_at = excluded.fetched_at
                """,
                (
                    key,
                    value.get("lat"), value.get("lon"),
                    value.get("precision"), value.get("country_code"),
                    value.get("display_name"), value.get("provider", "nominatim"),
                    _now(),
                ),
            )
            conn.commit()

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def nominatim(query, timeout=8):
    """Forward-geocode via OpenStreetMap. No API key; 1 req/sec is their policy.

    Returns None on any failure. A geocoder outage must degrade the commute
    screen to "unknown", never block a sweep or reject a job.
    """
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not payload:
        return None
    hit = payload[0]
    address = hit.get("address") or {}
    return {
        "lat": float(hit["lat"]), "lon": float(hit["lon"]),
        "precision": _PRECISION_BY_TYPE.get(
            hit.get("addresstype") or hit.get("type") or "", "unknown"),
        "country_code": (address.get("country_code") or "").lower(),
        "display_name": hit.get("display_name") or "",
        "city": address.get("city") or address.get("town")
        or address.get("municipality") or address.get("state") or "",
        "provider": "nominatim",
    }


def reverse(lat, lon, timeout=8):
    """Reverse-geocode, used once to discover the metro centre for the home
    location so the user never has to name their nearest city by hand."""
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    address = (payload or {}).get("address") or {}
    return {
        "city": address.get("city") or address.get("town")
        or address.get("municipality") or "",
        "state": address.get("state") or "",
        "country_code": (address.get("country_code") or "").lower(),
    }


def resolve_provider(name=None):
    """Pluggable so a user with a paid key is not stuck on the free tier."""
    return {"nominatim": nominatim}.get((name or "nominatim").lower(), nominatim)


def cache_stats():
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN lat IS NULL THEN 1 ELSE 0 END) AS failed "
            "FROM geocode_cache"
        ).fetchone()
    return {"total": row["total"] or 0, "failed": row["failed"] or 0}


def clear_failed():
    """Drop cached negatives so they are retried; useful after fixing a
    provider outage or a bad home-location setting."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM geocode_cache WHERE lat IS NULL")
        conn.commit()
