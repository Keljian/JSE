# Scraper Plugin Guide

This guide explains how to build a scraper plugin for JSE. A plugin is a folder
containing a `scraper-plugin.json` manifest and a Python module with one callable
scrape function.

## Plugin Folder Shape

```text
my_scraper/
  scraper-plugin.json
  scraper.py
  README.md              optional
  helpers.py             optional
```

The plugin folder can be imported through the Settings UI or placed in a local
plugin directory. User-installed plugin folders and archives should stay out of
shared source.

## Building Plugins In JSE

JSE includes a guided plugin builder in **Settings > General > Searchers**. It
asks for the source name, careers URL, mode, platform hints, location, test
keyword, page limit, and any notes you know about the page structure. The builder
then sends those answers to the configured local OpenAI-compatible LLM and writes
a plugin folder under `scraper_plugins/`.

The builder performs a first-pass safety check before installing the generated
plugin:

- validates the manifest;
- parses `scraper.py` for syntax errors;
- blocks shell, subprocess, dynamic execution, and direct filesystem-write
  patterns;
- requires a callable `scrape(...)` function;
- imports the plugin through the normal plugin registry.

Use **Dry run** after generation. Dry runs call the plugin with `dry_run=True`
and a low page limit so the scraper can fetch and parse a sample without writing
jobs to the database. Review the sample output before enabling the plugin for a
real search.

## Manifest

Every plugin needs a `scraper-plugin.json` file.

```json
{
  "id": "example_jobs",
  "name": "Example Jobs",
  "source_name": "Example Jobs",
  "version": "1.0.0",
  "module": "scraper.py",
  "callable": "scrape_example_jobs",
  "mode": "keyword",
  "aliases": ["Example Careers"],
  "config_schema": [
    {
      "key": "base_url",
      "label": "Base URL",
      "type": "text",
      "default": "https://example.com/jobs"
    },
    {
      "key": "location",
      "label": "Location",
      "type": "text",
      "default": "Melbourne VIC"
    },
    {
      "key": "max_pages",
      "label": "Page limit",
      "type": "number",
      "default": 10,
      "legacy_key": "max_pages"
    }
  ]
}
```

Required fields:

- `id`: stable unique identifier, lowercase snake/kebab style recommended.
- `name`: display name in Settings.
- `source_name`: source value stored on jobs and shown in the UI.
- `module`: Python module path. For user plugins this can be a file such as
  `scraper.py`; bundled plugins can use import paths such as `scrapers.nga_net`.

Optional fields:

- `callable`: function name to call. Defaults to `scrape`.
- `version`: display/maintenance version.
- `mode`: `keyword` or `sweep`. Defaults to `keyword`.
- `aliases`: alternate source names that resolve to the plugin.
- `config_schema`: settings fields shown in the UI and merged into scraper args.

## Modes

`keyword` mode runs once per generated/search keyword.

```text
keyword: "business analyst" -> scrape
keyword: "systems analyst"  -> scrape
keyword: "delivery lead"    -> scrape
```

Use this for boards that provide a search box or query URL.

`sweep` mode runs once per selected source, independent of keywords.

```text
source: "Example Council" -> scrape all configured listing pages
```

Use this for employer career pages, council boards, university listings, or
feeds where keyword search is weak or unavailable. In sweep mode JSE passes a
placeholder keyword equal to the source name; your scraper can ignore it.

## Configuration

Config values are merged in this order:

1. Defaults from `config_schema`.
2. Plugin-level config saved in Settings.
3. Lane-specific scraper config saved in Settings.
4. Legacy search settings mapped through `legacy_key`.

The merged values are passed to your callable as keyword arguments. If
`max_pages` exists, JSE coerces it to an integer and falls back to `30` if
invalid.

Supported schema item keys are intentionally simple:

- `key`: required config key passed to the callable.
- `label`: user-facing label.
- `type`: UI hint such as `text`, `number`, `checkbox`, or similar.
- `default`: default value.
- `legacy_key`: optional older settings key to map into this config item.

## Callable Contract

Your scraper function is called by `scraper_dispatcher.run_scraper_for_keyword`.

```python
def scrape_example_jobs(
    keyword,
    status_callback=None,
    log_callback=None,
    profile_id=1,
    base_url="https://example.com/jobs",
    location="Melbourne VIC",
    max_pages=10,
    **config,
):
    ...
```

Required accepted arguments:

- `keyword`: search term, or a placeholder source name for `sweep` plugins.
- `status_callback`: optional UI status callback.
- `log_callback`: optional log callback.
- `profile_id`: active lane/profile ID.

Your function should also accept the keys defined in `config_schema`. Adding
`**config` is recommended so future config additions do not break the plugin.

Return value:

- Return `True` if the scraper ran and found/stored at least one useful result.
- Return `False` if it failed or found no useful results.
- Raise `OperationCancelledError` only when cancellation is requested.

## Cancellation And Pause

Long-running scrapers must cooperate with cancellation.

```python
from concurrency import paused, cancel_event, OperationCancelledError


def check_control_flags():
    if cancel_event.is_set():
        raise OperationCancelledError("Scraping cancelled.")
    paused.wait()
```

Call this before page loads, between pages, and inside long loops.

## Storing Jobs

Scrapers should store jobs through `database_manager.add_job`.

```python
import database_manager as db


job_data = {
    "title": "Systems Analyst",
    "company": "Example Employer",
    "location": "Melbourne VIC",
    "url": "https://example.com/jobs/123",
    "description": "Full cleaned job advertisement text...",
    "pdf_text": "",
    "search_keyword": keyword,
    "closing_date": "2026-07-31",
    "contact_person": "Hiring Manager",
    "contact_email": "jobs@example.com",
    "contact_phone": "",
    "salary": "$120,000 - $140,000 plus super"
}

stored = db.add_job(job_data, "Example Jobs", profile_id=profile_id, log_callback=log_callback)
```

Important fields:

- `title`: required for useful dedupe and UI display.
- `company`: advertiser or employer name as shown by the source.
- `location`: human-readable location.
- `url`: canonical job URL. JSE dedupes strongly by normalized URL.
- `description`: full text used for analysis and dedupe.
- `pdf_text`: extracted position description or attached PDF text, if available.
- `search_keyword`: original search term.
- `closing_date`: optional, preferably ISO `YYYY-MM-DD` if known.
- `contact_person`, `contact_email`, `contact_phone`, `salary`: optional but
  useful. JSE also attempts best-effort extraction from text.

`add_job` handles:

- source normalization
- broad-feed plausibility filtering
- closing-date expiry skipping
- duplicate detection by URL, title/company, and description fingerprint
- company intelligence cache hints
- lane opportunity sync

## Minimal Example

```python
"""Example scraper plugin for JSE."""
from html.parser import HTMLParser
from urllib.parse import quote_plus

import requests

import database_manager as db
from concurrency import paused, cancel_event, OperationCancelledError


class JobCardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self._in_link = False
        self._current_href = ""
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href") and "job" in attrs.get("href", ""):
            self._in_link = True
            self._current_href = attrs["href"]
            self._current_text = []

    def handle_data(self, data):
        if self._in_link:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            title = " ".join(part.strip() for part in self._current_text if part.strip())
            if title:
                self.cards.append({"title": title, "url": self._current_href})
            self._in_link = False


def _check_cancelled():
    if cancel_event.is_set():
        raise OperationCancelledError("Scraping cancelled.")
    paused.wait()


def scrape_example_jobs(
    keyword,
    status_callback=None,
    log_callback=None,
    profile_id=1,
    base_url="https://example.com/jobs",
    location="Melbourne VIC",
    max_pages=10,
    **config,
):
    found = 0
    session = requests.Session()
    for page in range(1, int(max_pages or 10) + 1):
        _check_cancelled()
        if status_callback:
            status_callback(f"Example Jobs: page {page}", True)

        url = f"{base_url}?q={quote_plus(keyword)}&location={quote_plus(location)}&page={page}"
        response = session.get(url, timeout=30)
        response.raise_for_status()

        parser = JobCardParser()
        parser.feed(response.text)
        cards = parser.cards
        if not cards:
            break

        for card in cards:
            _check_cancelled()
            job_url = card["url"]
            detail = session.get(job_url, timeout=30)
            detail.raise_for_status()
            description = detail.text

            stored = db.add_job(
                {
                    "title": card["title"],
                    "company": "Example Jobs",
                    "location": location,
                    "url": job_url,
                    "description": description,
                    "search_keyword": keyword,
                },
                "Example Jobs",
                profile_id=profile_id,
                log_callback=log_callback,
            )
            if stored:
                found += 1

    if log_callback:
        log_callback(f"Example Jobs stored {found} jobs for '{keyword}'.")
    return found > 0
```

If a site requires Selenium, prefer shared helpers in `scraping_helpers.py` and
ensure WebDriver sessions are closed by using the existing resource-management
pattern.

## Quality Requirements

A production scraper should:

- Respect cancellation and pause controls.
- Limit pages and requests using `max_pages`.
- Use stable canonical URLs for dedupe.
- Extract detail-page descriptions, not just card summaries.
- Extract PDF position descriptions when the source provides them.
- Log meaningful progress without exposing secrets or personal data.
- Handle missing fields, changed selectors, timeouts, and HTTP errors gracefully.
- Return `False` rather than crashing on ordinary scrape failures.
- Avoid committing cookies, browser profiles, API keys, generated documents, or
  scraped personal data into source.

## Import And Testing Checklist

1. Create a plugin folder with `scraper-plugin.json` and the Python module.
2. Validate the manifest fields: `id`, `name`, `source_name`, and `module`.
3. Make sure the callable name matches `callable` or is named `scrape`.
4. Run the callable manually with a small `max_pages` value.
5. Confirm it calls `db.add_job` and returns `True` only when useful results are
   stored.
6. Import the plugin through Settings.
7. Enable it globally and for the target lane.
8. Run a one-keyword search and confirm jobs appear in the pipeline.
9. Test cancellation during page loading and detail extraction.
10. Re-run the same search and confirm duplicates are skipped/refreshed rather
    than added again.

## Common Failures

- `No scraper-plugin.json manifest found.`: import path points at the wrong
  folder or file.
- `Plugin manifest missing required field(s)`: add the missing required manifest
  fields.
- `mode must be 'keyword' or 'sweep'`: use only those exact mode values.
- `has no callable`: the Python function name does not match the manifest.
- No jobs appear: check `url`, `title`, `company`, and `description`; broad-feed
  filtering and duplicate detection may skip weak or repeated records.
- Search runs once per keyword unexpectedly: set `"mode": "sweep"` for
  keyword-independent source sweeps.
