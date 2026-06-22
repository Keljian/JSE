# JSE Scraper Plugin Reference

## Plugin structure

Each plugin lives in a directory with two files:
- `scraper-plugin.json` — manifest
- `scraper.py` — scraper code

`manifest.callable` must be `"scrape"`, `manifest.module` must be `"scraper.py"`.

## Required scrape() signature

```python
def scrape(keyword, status_callback=None, log_callback=None, profile_id=1,
           base_url="", company_name="", location="", max_pages=3, dry_run=False, **config):
```

`**config` is mandatory — the system passes additional config keys as kwargs.

## Approach selection guide

| Evidence from recon | Correct approach |
|---|---|
| `<script type="application/ld+json">` with JobPosting | Parse JSON-LD directly (requests, no Selenium) |
| `__NEXT_DATA__` / `__NUXT__` embedded in HTML | Parse embedded JSON blob (requests, no Selenium) |
| Known ATS: Greenhouse / Lever / Workday / SmartRecruiters | Hit their JSON API directly (requests) |
| Job links visible in `requests.get()` response | requests + BeautifulSoup |
| JS-rendered, no static job data visible | Selenium via `scraper_resource_manager` |

## scraping_helpers API

### `scraper_resource_manager(wait_timeout=20)`

Decorator that creates and destroys a headless Chrome driver with stealth settings. The
**decorated** function receives extra leading args `(driver, wait, ...)`. The outer `scrape()`
entry point is produced by the decorator automatically. Assign the decorated function to
the name `scrape` at module level:

```python
from scraping_helpers import scraper_resource_manager, scrape_job_details

@scraper_resource_manager(wait_timeout=20)
def _inner(driver, wait, keyword, status_callback, log_callback, location, max_pages,
           base_url="", company_name="", profile_id=1, dry_run=False, **config):
    log = log_callback or print
    ...

scrape = _inner  # required — this IS the scrape() entry point
```

### `scrape_job_details(driver, wait, jobs_list, log_callback, profile_id)`

Iterates a list of job dicts, visits each URL via Selenium, extracts description using
multiple fallback CSS selectors, and calls `db.add_job`. Returns `int` saved count.
`jobs_list` items must have keys: `title`, `url`, `company`, `location`.

### `_get_pdf_text_from_url(pdf_url, base_url, log_callback)`

Downloads a PDF and returns its extracted text as a string, or `None` on failure.

## database_manager API

```python
import database_manager as db

# Returns True if inserted as new job, False if duplicate/error
db.add_job(job_dict, source_name, profile_id=1, log_callback=None)
```

`job_dict` keys: `title`, `company`, `location`, `url`, `description`, `pdf_text`, `salary`, `search_keyword`.

## concurrency

```python
from concurrency import cancel_event, paused, OperationCancelledError

# Call inside every page loop iteration:
if cancel_event.is_set():
    raise OperationCancelledError("Scraping cancelled by user.")
paused.wait()  # blocks while user has paused scraping
```

## dry_run contract

When `dry_run=True`:
- Fetch and parse at most 1–2 pages.
- Do NOT call `db.add_job()`.
- Return this exact dict:
  ```python
  {"ok": True, "found": <int>, "sample_jobs": [{"title": "...", "company": "..."}], "warnings": []}
  ```
  `"ok"` is `True` if at least one job was parsed successfully.

When `dry_run=False`: return `True` if any jobs were stored, else `False`.

## Allowed imports

Only these top-level modules are permitted: `__future__`, `bs4`, `concurrency`,
`database_manager`, `datetime`, `html`, `hashlib`, `json`, `lxml`, `math`, `re`,
`requests`, `selenium`, `scraping_helpers`, `threading`, `time`, `traceback`, `urllib`, `urllib3`.

## Known ATS platforms

| ATS | URL marker | API / approach |
|---|---|---|
| Greenhouse | `boards.greenhouse.io` | `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `jobs.lever.co` | `GET https://api.lever.co/v0/postings/{slug}?mode=json` |
| Workday | `myworkdayjobs.com` | `POST /wday/cxs/{tenant}/{site}/jobs` with JSON body |
| SmartRecruiters | `jobs.smartrecruiters.com` | `GET https://api.smartrecruiters.com/v1/companies/{co}/postings?keyword=X` |
| PageUp | `pageuppeople.com` links | Selenium + `scrape_job_details`, links at `/job/` or `/listing/` |
| BambooHR | `bamboohr.com` | `GET https://{company}.bamboohr.com/careers/list` |
| Recruitee | `recruitee.com` | `GET https://{company}.recruitee.com/api/offers/?status=open` |
| Ashby | `ashbyhq.com` | `POST https://api.ashbyhq.com/posting-public/job/list` |
| Jobvite | `jobvite.com` | `GET https://jobs.jobvite.com/api/job?c={company_code}` |
