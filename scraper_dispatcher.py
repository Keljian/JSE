"""Resolve scraper source names and run the matching scraper plugin."""
from concurrency import OperationCancelledError, cancel_event
import database_manager as db
import scraper_plugins


# Outcome of one (source, keyword) search:
#   saved  - at least one new job was stored
#   no_new - listings were found, but every one was already saved or filtered
#   empty  - the search found no listings at all
#   skipped - the scraper declined to run (e.g. a rate-limit cooldown)
# Only "empty" says anything about the search term, so only it is retried, and
# only it counts toward a source's empty streak in health.
SAVED, NO_NEW, EMPTY, SKIPPED = "saved", "no_new", "empty", "skipped"


def _load_scraper(source):
    plugin = scraper_plugins.get_plugin(source, include_disabled=False)
    return scraper_plugins.load_callable(plugin) if plugin else None


def _classify(result, listings, saved_count=0):
    """Turn a plugin's return value and the add_job counts into an outcome.

    Plugins return a bool ("saved something new"). A plugin may instead return
    a dict with "ok", and optionally "skipped" (a reason string) or "found".
    The counts win over the return value: a search that was rate-limited after
    finding listings still found them, and may have saved some.
    """
    skipped = False
    if isinstance(result, dict):
        skipped = bool(result.get("skipped"))
        ok = bool(result.get("ok"))
        listings = max(listings, int(result.get("found") or 0))
    else:
        ok = bool(result)
    if saved_count > 0 or ok:
        return SAVED
    if listings > 0:
        return NO_NEW
    return SKIPPED if skipped else EMPTY


def run_scraper_search(source, keyword, status_callback=None, log_callback=None, max_pages=30, profile_id=1, search_settings=None):
    """Run one search and report what it found.

    Returns {"outcome": saved|no_new|empty|skipped, "listings": n, "saved": bool}.
    """
    if cancel_event.is_set():
        raise OperationCancelledError("Scraping cancelled.")

    search_settings = search_settings or {}
    plugin = scraper_plugins.get_plugin(source, profile_id=profile_id, include_disabled=False)
    try:
        scraper_func = scraper_plugins.load_callable(plugin) if plugin else None
    except OperationCancelledError:
        raise
    except Exception as exc:
        if plugin:
            db.record_scraper_health(plugin["id"], "error", exc)
        raise
    if not scraper_func:
        if log_callback:
            log_callback(f"Unknown source selected for scraping: {source}")
        return {"outcome": SKIPPED, "listings": 0, "saved": False}

    args = scraper_plugins.build_config(plugin, {**search_settings, "max_pages": search_settings.get("max_pages") or max_pages})
    db.reset_listing_count()
    try:
        result = scraper_func(
            keyword=keyword,
            status_callback=status_callback,
            log_callback=log_callback,
            profile_id=profile_id,
            **args
        )
    except OperationCancelledError:
        raise
    except Exception as exc:
        db.record_scraper_health(plugin["id"], "error", exc)
        raise
    listings = db.listing_count()
    outcome = _classify(result, listings, db.saved_listing_count())
    if outcome in (SAVED, NO_NEW):
        # Finding listings that were all saved before is the source working.
        db.record_scraper_health(plugin["id"], "success")
    elif outcome == EMPTY:
        db.record_scraper_health(plugin["id"], "empty")
    return {"outcome": outcome, "listings": listings, "saved": outcome == SAVED}


def run_scraper_for_keyword(source, keyword, status_callback=None, log_callback=None, max_pages=30, profile_id=1, search_settings=None):
    """Dispatcher that calls the correct scraper for a single source.

    Returns True when at least one new job was saved.
    """
    return run_scraper_search(
        source, keyword, status_callback, log_callback,
        max_pages=max_pages, profile_id=profile_id, search_settings=search_settings,
    )["saved"]
