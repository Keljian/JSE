"""Resolve scraper source names and run the matching scraper plugin."""
from concurrency import OperationCancelledError, cancel_event
import database_manager as db
import scraper_plugins


def _load_scraper(source):
    plugin = scraper_plugins.get_plugin(source, include_disabled=False)
    return scraper_plugins.load_callable(plugin) if plugin else None


def run_scraper_for_keyword(source, keyword, status_callback=None, log_callback=None, max_pages=30, profile_id=1, search_settings=None):
    """Dispatcher that calls the correct scraper for a single source."""
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
        return False

    args = scraper_plugins.build_config(plugin, {**search_settings, "max_pages": search_settings.get("max_pages") or max_pages})
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
    db.record_scraper_health(plugin["id"], "success" if result else "empty")
    return result
