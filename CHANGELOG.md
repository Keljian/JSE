# Changelog

All notable changes to JSE are documented here.

## Unreleased

## 1.0.0-beta.2 - 2026-07-31

### Added

- Added **job flags**. Triage now scores a role and raises flags on it in the
  same call, where a flag is a specific, checkable concern that names the ad's
  own requirement and why the resume does not meet it. Five types:
  `credential_gate`, `domain_mismatch`, `seniority_below`, `seniority_above` and
  `evidence_gap`.
  - **Flags never gate anything.** No code path branches on them. They do not
    block document generation, cap a score, or drop a role from a listing or a
    shortlist packet. Everything they carry is shown next to the score so the
    person deciding can see it.
  - **Deterministic pre-pass.** `_extract_mandatory_requirements` pulls the ad's
    own "must have / essential / mandatory" lines out of the prose and marks the
    subset naming a credential, registration, or eligibility gate. Items framed
    as *preferred*, *desirable* or *advantageous* can never be a credential
    flag. Triage also now receives the full advertisement instead of a
    3,500-character extract, so a registration buried at the bottom still gets
    seen.
  - **No extra LLM call.** Flagging was folded into triage because nothing
    branches on it, so a separate pass bought nothing while doubling the per-job
    round trips against a single-slot local model. Folding it in also widened
    coverage: triage runs on every job, where a post-triage stage would only see
    the ones that already cleared the threshold.
  - **Rules that keep flags readable.** A flag must name the ad's requirement or
    it is dropped, since unevidenced flags are noise and noise teaches people to
    skim past the real ones. Low confidence is kept and labelled rather than
    discarded, because the reader decides. An unparseable triage response yields
    no flags and the job continues.
  - **Flags feed the analysis prompt**, which is instructed to answer each one
    directly and let unanswerable ones lower the score rather than paper over
    them.
  - Stored per job (`job_flags_json`, `job_flags_types`, `job_flags_checked_at`),
    shown as chips on job cards and a panel in the workspace. You can dismiss a
    flag or add your own; yours are marked manual and survive re-analysis, since
    nothing can re-derive "the recruiter would not name the client". Bridge
    commands: `jobs:addFlag`, `jobs:dismissFlag`, `jobs:clearFlags`.

  This replaced a first attempt that returned a skip / stretch / clear verdict
  and refused to generate documents on a skip. That version put a model in the
  position of overruling its user on a judgement about their own career, which
  is worse than offering no help at all. The detection was worth keeping; the
  authority was not.

- Added **channel warmth** as a first-class dimension on jobs, not just on
  outcome snapshots. Warmth was only known after an application was sent, which
  is exactly too late to act on:
  - New `jobs.channel` column. The existing four-channel vocabulary
    (`board` / `recruiter` / `warm_referral` / `direct_outreach`) is reused
    rather than forked, so Funnel Insights keeps reporting against the channels
    it has already backfilled. An explicit channel always beats the derivation.
  - **Derived warmth rank** on top of it: warm (a referral or direct outreach,
    where the application is not judged side by side against a better-matched
    candidate), named contact (a human is identified but the route is still the
    portal), or cold. Derived rather than stored so the finer distinction can
    inform ranking without splitting the reporting dimension.
  - **Warmth outranks the score** in the campaign plan and the attack queue: a
    moderate-scoring role with a real contact behind it now beats a
    higher-scoring cold board submission. On the pipeline board it sits above
    the score but below priority and due dates, so an overdue action still
    leads.
  - **Possible warm paths surface before the application is built.** A single
    grouped query matches each job's employer against the `warm_contacts` book
    (real employer first, so a recruiter-listed role still finds the contact at
    the end client) and the card shows "Possible warm path: X".
  - **Dashboard nudge on a cold mix**, distinct from the existing idle-channel
    nudge: activity is not allocation, and a week can contain plenty of
    hidden-market work while every application still went out cold. Names the
    live roles that already have a contact behind them.

- Added **triage packet export** (`jobs:exportShortlist`). The daily loop is a
  sweep followed by a human go/no-go pass elsewhere, and that handoff was manual
  copying. One markdown and/or JSON file per sweep now carries the whole
  shortlist: ad text, position description, extracted metadata, scores with the
  stored analysis, any flags, and warm-path hits, written to a configurable
  (watchable) folder. Warm roles lead the packet. Flagged roles stay in it,
  because filtering them out would make the go/no-go call the packet exists to
  support; `exclude_flags` narrows it on request.

- Added an explicit **two-track document strategy**. Overqualification screening
  on support-grade roles is a measured rejection cause, and the fix existed only
  as a manual practice:
  - Triage now reports `seniority_direction` (`below` / `aligned` / `above`)
    whether or not it raises a seniority flag, because a role below the resume's
    ceiling changes how the application must be written.
  - `document_track` derives **stripped back** vs **full senior** from signals
    already computed: that direction, the title band, and the salary band.
    Triage's judgement is decisive on its own; the keyword heuristics
    need two agreeing signals, so one support-grade word in a manager title
    cannot strip a senior resume. A manual override persists on the job.
  - On the stripped track the resume is written to the ad's actual scope (same
    real employers, titles and dates — emphasis changes, facts never do) and the
    cover letter answers the level question directly in one honest sentence
    instead of leaving the screener to answer it.

- Added **Targeting**, which acts on what Funnel Insights measured. Analysis of
  156 recorded outcomes showed seniority band is the dominant predictor of an
  interview (bridging titles 25%, manager-lead 1.2%, against a 5.8% baseline)
  while `match_score` separates outcomes not at all, and that 71% of
  applications were landing in bands that convert below baseline:
  - **Band-weighted conversion priors.** Priors now carry a per-dimension clamp
    and scale (`PRIOR_CLAMP_BY_DIMENSION` / `PRIOR_SCALE_BY_DIMENSION`):
    `seniority_band` gets ±25, every other dimension keeps ±10. Contributing
    deltas are combined as an average weighted by each dimension's clamp, so the
    band evidence is not diluted back to ±10 by three narrower neighbours.
    The auto-reject guard is unchanged — a prior still can never, on its own,
    push a job across the threshold in either direction, and band alone never
    rejects a job.
  - **Composite rebalanced from 80/20 to 60/40** in favour of fragment
    alignment over JD similarity, as named constants
    (`COMPOSITE_MATCH_WEIGHT` / `COMPOSITE_FRAGMENT_WEIGHT`) shared by the
    scoring path and mirrored in the renderer.
  - **Band-aware triage.** The observed rate for a job's band is written into
    its stored analysis and exposed via `targeting:explainScore`, so a demotion
    is visible rather than silent. Bridging coverage extended from the titles
    that actually produced interviews, with a labelled fixture set in
    `tests/test_targeting_strategy.py` so band assignment is tested, not assumed.
  - **Near-miss outcome states.** `final_round` and `runner_up` rank above
    `interview`, alongside new `interview_stage_reached` and `loss_reason`
    fields. A first-round screen-out and a "second by a very small margin" no
    longer collapse to the same result. The outcome nudge now asks how far an
    interview went, not just whether it happened, and Funnel Insights reports
    two conversion rates: application → interview and interview → final round.
  - **Channel attribution.** Every outcome snapshot records a channel
    (`board` / `recruiter` / `warm_referral` / `direct_outreach`), backfilled for
    existing rows; externally-logged applications are left unattributed for the
    user to set rather than guessed. Conversion is reported per channel.
  - **Warm-channel activation.** A lead can now be created against a named
    target employer with no advertised role behind it — the entry point the
    hidden-market modules were missing, and the reason those tables held zero
    rows. Adds a `warm_contacts` contact book (seedable from existing contact
    research and company profiles) and a dashboard nudge after seven days with
    no warm-channel activity.
  - **Targeting dashboard card** showing, for the trailing 90 days, applications
    by seniority band, conversion by band and channel, and the share of
    applications landing in below-baseline bands. Placed above the scraper
    statistics: discovery is not the constraint, allocation is.

### Changed

- **Split the four monoliths into layered packages.** They had become hostile to
  work in — edits collided, unrelated functions churned in every diff, and a
  single file no longer fit in a review. Each keeps a facade or entrypoint at its
  original path, so no caller changed:
  - `database_manager.py` 9,191 lines → `db/` (12 modules, largest 2,264) behind
    a 481-line facade.
  - `llm_handler.py` 3,644 → `llm/` (7 modules, largest 1,217) behind a 139-line
    facade.
  - `python_bridge.py` 3,509 → `bridge/` (9 modules, largest 762). Each module
    declares its own `COMMANDS` mapping and the entrypoint merges them, refusing
    duplicate keys, so adding a command means editing one file.
  - `src/main.jsx` 6,072 → `src/lib/` + `src/components/` (13 modules, largest
    863) with main.jsx as a composition root.

  Packages are layered: at module scope a module may import only from earlier
  layers. The domain is genuinely cyclic in a few places (a stage transition
  writes an outcome; building an outcome snapshot reads the job), and those
  crossings use a function-local import with a comment explaining why — eight in
  `db/`, five in `llm/`. Asserted by tests, so a module-scope back-reference
  fails the build instead of becoming a runtime import cycle.

- **Added `facade.py`,** because splitting a namespace breaks two things that
  fail silently:
  - *Monkeypatching.* `llm_handler._call_unsloth = stub` used to affect every
    caller; after a split it rebinds the facade while the real function still
    runs. Not hypothetical — when the `llm/` split landed, three tests began
    making live network calls and the suite went from 4s to 42s.
  - *Mutable state.* `database_manager.DB_FILE` is repointed at a throwaway
    database by every test. A plain re-export creates a second binding, so the
    assignment moves the facade's copy while `get_db_connection()` keeps opening
    the real file — tests passing while writing to production data.

  Facade attribute writes are now forwarded into every module binding that name,
  and `DB_FILE` / `DATA_DIR` / `_wal_enabled` are proxied to `db.connection` so
  exactly one binding exists. `facade.install` refuses to run if a proxied name
  gets re-exported by mistake.

- Added a **React error boundary** at the root. JSE has no address bar and no
  reload button, so an uncaught render error left a blank window with no way to
  recover or report it; it now degrades to a message with the component stack, a
  reload button, and a copy-details button.

- **CI now gates the installer build on tests and linting.** All three platform
  builds previously ran regardless of whether the code passed its own tests,
  which the suite never ran in CI at all. A fast Ubuntu job runs `ruff`,
  `eslint`, `pytest`, and the renderer build first; the Windows, macOS, and
  Linux jobs depend on it.
- Added `ruff` (via `pyproject.toml`) and a flat `eslint` config. Both start
  narrow on purpose — undefined names, unused variables and imports, mutable
  default arguments, bare excepts, hook-ordering violations — rather than style
  opinions that would produce a large, low-value first diff. The React Compiler
  rules in `eslint-plugin-react-hooks`' recommended preset are deliberately left
  off: they flagged ~20 optimisation notes and no defects, and a linter that is
  red on day one trains people to ignore it. Fixed everything the first run
  found (a dead `deleteJob` handler, an unused driver log callback, unused
  imports, f-strings with no placeholders, a swallowed exception binding).
- **Pinned the full Python dependency tree** in `requirements.lock` (56
  packages, transitives included), regenerated by
  `tools/write_requirements_lock.py`. `requirements.txt` used only lower bounds
  while the CI runtime cache was keyed on its hash, so a transitive release
  could change what an installer shipped with no diff anywhere in the repo. The
  runtime-prep scripts and the CI cache keys now use the lock.

### Fixed

- **Installers no longer embed personal runtime data.** The electron-builder
  `files` list packaged `scraper_plugins/**/*` and `search_terms.json` from the
  repo root, but both are gitignored personal data — so a build on a developer
  machine shipped personal search terms and local council/university scrapers
  while a CI build from a clean checkout shipped neither, and the same commit
  produced two different applications depending on who built it. Neutral,
  committed first-run content now lives in `defaults/`, is what gets packaged,
  and is seeded into the user's data directory on first launch. The `.gitignore`
  patterns were anchored with a leading slash so the personal root copies stay
  ignored without also ignoring the shipped defaults. Asserted in
  `tests/test_packaging_manifest.py`, since this class of bug is invisible in a
  diff.
- Fixed two path bugs the split surfaced before they shipped: `db/connection.py`
  and `bridge/runtime.py` both derive the application root from `__file__` and
  needed `parents[1]` rather than `parent` once they moved a directory deeper.
  Left uncorrected, the first silently relocated the entire data directory to
  `db/settings/` — a fresh, empty database beside the real one. Both are now
  asserted in tests.
- Archived the stale root `job_applications.db` (21 jobs, last touched 16 June)
  into `Backups/`. The live database is under `settings/` with 16,157 jobs, and
  having both at hand invited a wrong-database accident.

- Orphaned outcome snapshots (applications whose job row was hard-deleted by the
  old lane cascade) are now reconstructed from `job_postings` and
  `application_events` instead of being stored as dimensionless stubs. All 26
  such rows in the reference database recovered their title, company, and
  seniority band, eliminating the `unknown` band entirely. Rows that still
  cannot be resolved are excluded from dimension breakdowns and reported as a
  visible count, rather than bucketed as `unknown` where they diluted every
  dimension they appeared in.

- Added **Funnel Insights**, an outcome-driven feedback loop that learns which
  applications actually convert to interviews:
  - New immutable `application_outcomes` snapshots capture the dimensional state
    of every application at the moment it reaches **Applied** (title, company,
    advertiser, employer type, source, salary band, match/fragment/composite
    scores, seniority band, lane, document method). Snapshots survive job
    deletion, so lane cleanup and duplicate removal can never erase interview
    history. Existing history is backfilled once on migration, including orphan
    interview rows whose job was hard-deleted by the old cascade.
  - **Role-entity linking**: a role re-advertised under two titles (same
    advertiser + high description-fingerprint similarity, or an identical
    normalized title within 90 days) collapses to one `role_key`, so conversion
    statistics no longer double-count.
  - A **Funnel Insights** dashboard card shows the baseline interview rate and
    the best- and worst-converting segments (with sample sizes) across source,
    advertiser, employer type, match-score band, salary band, seniority band,
    and lane. Segments below three applications are suppressed as noise.
    Recompute on demand; results are cached.
  - **Log external application**: a Pipeline action to record applications made
    outside JSE (the previously invisible off-platform interviews), created at
    the Applied stage with their own outcome snapshot.
  - **Interview-validated fragments**: when a job reaches an interview, JSE mines
    its job description and submitted documents and weights the resulting
    candidate-memory fragments above merely-submitted evidence in lane affinity
    and keyword generation.
  - **Conversion prior in scoring**: composite scores receive a bounded (±10)
    nudge from observed per-dimension conversion rates. The prior needs at least
    five outcomes in a bucket to take effect and can never, on its own, push a
    job across the auto-reject threshold.
  - **Outcome hygiene nudges**: a dismissible dashboard prompt asks how a past
    interview went, writing the result back to the interview and the outcome
    snapshot.
  - **Interview Learnings tab**: a dedicated view listing every interviewed role
    with a one-click Mine / Re-mine action, and cards for the resulting
    interview-validated fragments (claim, keywords, reuse guidance, source
    roles). For roles interviewed without an in-app-generated resume/cover, the
    miner falls back to the candidate's most job-description-relevant evidence
    from the context library, so historical interviews can still be mined.
    Fragment provenance now merges across roles, so evidence shared by several
    interviews stays attributed to all of them.
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

- Job analysis now scores multiple jobs in parallel instead of one at a time.
  A new **Parallel analysis** control (Settings → AI, Job matching card) sets
  how many jobs run at once (default 2, up to 8) — local endpoints need
  server-side parallel slots to benefit, while hosted scoring providers can
  comfortably run 4–8 for a near-linear speedup. Pause and cancel still take
  effect immediately, and cancelling drops all queued jobs. Failed-search
  keyword retries also generalize and re-run concurrently, so one slow LLM
  call no longer serializes the whole retry pass.
- Board loading is roughly 10x faster after the first refresh (~2.6s down to
  ~0.25s on a ~5,000-job database). Every `app:refresh` — app boot, each
  pipeline action, each filter change — previously re-ran ~20 regex passes
  over every stored ad description to derive ad signals, re-scanned all
  scraper plugin manifests from disk, and fetched its seven data sets one
  after another. Ad-signal regex results are now cached in the persistent
  bridge worker and recomputed only for ads whose text changed (date-relative
  fields like urgency stay live), plugin registration runs once per session
  (the Searchers settings view still forces a fresh disk scan), and the
  refresh sub-queries run in parallel. Startup database maintenance (dedupe,
  backfills, auto-reject, retirement sweeps) moved to a background thread so
  it no longer blocks the first paint.
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

- Application document generation can now use the local endpoint. Selecting
  "Local endpoint" for Application documents previously had no effect —
  authoring silently diverted to whatever cloud key was configured (the real
  cause of the unexpected Gemini rate-limit failures). It now authors the
  resume, cover letter, and evidence review against the local OpenAI-compatible
  server, keeps all data on the device, strips Qwen3-style `<think>` reasoning,
  and discovers the loaded model when none is named. Cloud providers remain
  available and are used when explicitly selected.
- Fixed document generation failing with "HTTP Error 429: Too Many Requests"
  against Gemini on a free-tier quota. The REST retry helper ignored the
  server's own back-off hint and gave up after a short blind delay; it now
  waits the requested time (Gemini's `RetryInfo.retryDelay` in the body, or a
  `Retry-After` header, capped) so a per-minute rate window can actually clear
  before the next attempt. The optional evidence-cache creation no longer
  spends the rate-limit budget retrying — it fails fast to implicit caching so
  that budget goes to the calls that produce the documents. When a 429 still
  persists, the batch log now explains it is a provider quota limit and points
  to retrying a smaller batch or switching to a higher-limit model (a Gemini
  "flash" model rather than "pro-preview").
- Fixed "HTTP Error 429: Too Many Requests" from the local LLM endpoint during
  bulk matching. A single-slot local server rejects a second request while one
  is in flight, but the analysis worker pool, the concurrent keyword-retry
  pool, live analysis, and document generation could all reach it at once with
  no shared cap. Every outbound LLM request now passes through a global
  concurrency gate sized by one setting; matching against the local endpoint is
  forced to one request at a time (there is no reliable signal that a local
  runtime serves parallel requests), and the "Simultaneous LLM requests"
  control only raises concurrency for hosted / free OpenAI-compatible matching
  providers that genuinely support it. A queued request also unblocks promptly
  on cancellation instead of waiting behind an in-flight call.
- Fixed intermittent "attempt to write a readonly database" errors (and the
  resulting bridge-worker restart loop) that appeared while scraping after the
  refresh/analysis parallelization. The new concurrent connection fan-out
  exposed two latent issues in the SQLite connection helper: the one-time
  `journal_mode=WAL` confirmation was unsynchronised and ran before
  `busy_timeout` was applied, so it could fail fast under a momentary lock; and
  bursts of short-lived connections closing together could drop the process's
  open-connection count to zero, tearing down and rebuilding the WAL index at
  the same moment the separate scraper process held a write lock. The WAL
  confirmation is now lock-guarded, ordered after `busy_timeout`, and never
  fatal (the connection operates in WAL from the persisted header regardless),
  and each process now holds one idle keep-alive connection open to pin the WAL
  index for its lifetime. The keep-alive is safe against the file-swapping
  paths: database restore terminates every worker before replacing the file,
  and compaction VACUUMs in place.
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
