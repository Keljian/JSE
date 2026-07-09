# Changelog

All notable changes to JSE are documented here.

## Unreleased

### Added

- Added a **Delete lane** control to Settings so a lane (job-search profile)
  can be removed from the GUI. Guarded behind a confirmation dialog and
  disabled when only one lane remains, since at least one lane must exist.
- Added PDF conversion actions for `.doc` and `.docx` application documents
  directly in the Application workspace.
- Improved scraper plugin generation success rate with layered prompt intelligence:
  - Added ATS fingerprinting for 13 platforms (Greenhouse, Lever, Workday, SmartRecruiters,
    PageUp, SuccessFactors, Taleo, BambooHR, Recruitee, Ashby, Jobvite, NGA, and others).
    ATS is identified from the URL alone before the HTTP fetch, so the correct approach is
    known even when sites block bots.
  - Reconnaissance now extracts a real job card HTML snippet and up to 1 500 chars of
    `__NEXT_DATA__` embedded JSON from the target page and injects them into the prompt so
    the LLM derives selectors from actual markup rather than assumptions.
  - Added a tier-routing directive that steers the LLM to the correct approach (JSON-LD,
    embedded JSON, ATS REST API, static HTML + BeautifulSoup, or Selenium) based on
    reconnaissance evidence.
  - Added a `scraping_helpers` API reference (`scraper_resource_manager`, `scrape_job_details`,
    `_get_pdf_text_from_url`) and a concrete `db.add_job` / concurrency pattern to every
    generation prompt.
  - Added an explicit code example of a working installed plugin (shortest matching HTTP or
    Selenium plugin) as a concrete reference in each generation prompt; falls back to a
    built-in minimal template when no plugins are installed yet.
  - Added an explicit `dry_run` return-contract code block so the test harness dict shape
    is unambiguous.
  - Generation `max_tokens` reduced from 16 000 to 8 000 and the prompt now instructs
    the LLM to stay under 150 lines and use helpers instead of re-implementing them,
    eliminating mid-JSON truncation failures.
  - Repair and second+ attempts use lower temperature (0.07/0.05 vs 0.15) for targeted
    corrections rather than creative rewrites.
  - Hardened local-LLM output handling for LM Studio and smaller models: the builder now
    accepts fenced, double-encoded, Python-style, and prose-wrapped JSON; reports empty
    responses with actionable chat-template guidance; and retries using structured JSON,
    portable JSON text, then a compact Python-only fallback.
  - Static validation now accepts the documented `scrape = decorated_function` pattern.
  - Added `SCRAPER_REFERENCE.md` — a living reference file injected into every build prompt
    covering the full scraper API, dry_run contract, allowed imports, and known ATS patterns.
  - Fixed local LLM response handling for thinking-mode models (qwythos, and Qwen3 configs
    where `/no_think` is not honoured): these models always return empty `content` and put
    all output — including the generated JSON — in the `reasoning_content` field. The LLM
    call layer now falls back to `reasoning_content` when `content` is empty, so generations
    that succeed in the model's reasoning trace are no longer silently discarded as failures.
  - Fixed `config_schema` normalisation: the LLM reliably generates `config_schema` as a
    dict (`{"key": {…}}`) rather than the required list (`[{"key": "...", …}]`). Generation
    now converts dict-format schemas to the correct list shape instead of crashing with
    `AttributeError: 'dict' object has no attribute 'append'`.
  - Added an explicit "CRITICAL MISTAKES" anti-pattern block to every generation prompt
    showing the wrong vs correct form for `config_schema`, `database_manager` import,
    `paused.wait()` usage, `found` integer in dry-run returns, keyword title filtering,
    and mode override. These mistakes appeared repeatedly in observed failure logs.
  - Fixed generated scrapers filtering job listings by keyword in the job title — the model
    was adding `if keyword.lower() not in title.lower(): continue` which causes dry-runs to
    find zero jobs whenever the test keyword doesn't appear in any current listing titles.
    Prompt now explicitly forbids title-based filtering and directs the model to pass keyword
    as a URL search parameter instead (or fetch all jobs for single-employer pages).
  - Fixed `_normalise_generation` to respect the user-chosen `mode` (sweep/keyword) from
    the answers rather than trusting the model's manifest output. The model frequently changed
    `mode: "sweep"` to `mode: "keyword"` for single-employer pages, breaking pagination logic.

- Added a verified SQLite backup on every application launch. Automatic startup
  backups are stored in `Backups/` and rotate after the newest 12; manual and
  recovery backups are never included in that rotation.
- Added **Recover database** beside database compaction in Settings. Recovery
  validates the selected backup, preserves the current database, restores it,
  and restarts JSE so every worker uses the recovered state.
- Rebuilt the former Hidden Market area as an Intelligence workspace with
  Market Signals, ranked Targets, Outreach, and Outcomes views.
- Added explainable opportunity scores using lane fit, recurrence, recency,
  momentum, identity confidence, contactability, and observed outcomes.
- Added auditable source-job evidence, classification reasons,
  counter-evidence, confidence, freshness, and data-coverage reporting.
- Added daily local market snapshots and period comparisons for title families,
  skills, salary bands, locations, work modes, and sources.
- Added structured, persistent outreach strategies with positioning, contact
  persona, channel, opening message, evidence, questions, follow-ups, and
  cautions.
- Added response, meeting, and conversion learning by target type, outreach
  channel, and opportunity-score band.
- Added public-source contact enrichment before person-specific strategy
  generation. JSE reconciles contacts across advertisements, checks publicly
  indexed organisation and professional-profile results, retains provenance,
  and pauses for user selection when identities conflict.
- Added integration coverage for market ranking, durable strategies, outcome
  learning, contact conflicts, provenance, and selected-person prompting.

### Changed

- The canonical database and settings location is now always the software's
  `settings` folder, so development and packaged launches cannot silently show
  different job histories.
- Contact extraction now preserves per-ad contact blocks, pairs names, emails,
  and phones by proximity, and rejects prose fragments masquerading as people.
  Target research shows one recommended contact and at most two credible
  alternatives; lower-quality candidates and extraction diagnostics start
  folded away.
- Identity selection now pauses strategy generation only when independently
  supported contacts remain genuinely close. Cached contact research is
  automatically refreshed under the stricter model.
- Renamed the main Hidden Market navigation item to Intelligence.
- Build Strategy now uses a resolved, evidence-backed person when available and
  safely falls back to an organisation-level approach when no reliable person
  can be found.
- Leadership-gap targets are explicitly treated as confidence-rated hypotheses
  rather than confirmed vacancies.
- Scraped position-description text is now attached to the Application
  workspace without replacing a document uploaded by the user.
- Document upload and path handling is more reliable in Electron.

### Fixed

- Fixed Run Search opening a blank, immovable window after lane deletion by
  normalising the active lane whenever the lane list changes, disabling search
  when no valid lane exists, and rejecting backend search requests with no
  active lane.
- Hardened Selenium search browser startup so scraper Chrome sessions are
  explicitly background/headless and scraper detail tabs are opened through
  WebDriver rather than page JavaScript.
- Fixed lane deletion leaving orphaned rows across a dozen lane-scoped tables
  (`lane_opportunities`, `application_kits`, `search_hits`, `local_llm_tasks`,
  the `hidden_market_*` tables, and more). The schema declares `ON DELETE
  CASCADE`/`SET NULL` against `profiles(id)` for these, but SQLite only
  enforces that with `PRAGMA foreign_keys` turned on, which the app's
  connections never set. The legacy `jobs` table predates the constraint
  entirely (columns were added via `ALTER TABLE` over time) and is now
  cleared explicitly. Deleting a lane also now refuses to remove the last
  remaining one.
- Readonly SQLite failures during document generation now identify the active
  database path and restart the persistent Python bridge worker so a stale
  worker state does not keep blocking retries.
- Search detail extraction now gives each advert a two-minute budget, uses
  short selector probes, and stops a search worker after four minutes without
  progress. Jobs already saved are retained when a timeout occurs.

### Privacy and safety

- Contact enrichment uses public search metadata and organisation pages only.
  It does not authenticate to or scrape LinkedIn profiles.
- Contact research, source provenance, strategies, and market snapshots are
  cached in the local JSE data store.
