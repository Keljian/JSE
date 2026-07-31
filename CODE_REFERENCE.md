# Code Reference

For first-run setup, see `README.md`. For the higher-level architecture,
workflow, and dataflow view, see `ARCHITECTURE.md`.

This project is an Electron + React desktop app backed by Python business logic.
The frontend calls the Python bridge; it does not import Python modules directly.
This reference covers app-owned source files, not generated installer/vendor
copies.

JSE is distributed under the MIT License; see `LICENSE` in the repository root.
If it saved you time or sanity on the job hunt, a coffee keeps the project
caffeinated and the commits coming: https://ko-fi.com/keljian

## Entry Points

- `Run.bat` / `Run.command` prepare first-run dependencies and start the desktop
  app with `npm run start`.
- `tools/start-dev.cjs` creates required runtime folders, ensures npm
  dependencies are present, starts Vite, waits for it, then launches Electron.
- `electron/main.cjs` owns the Electron main process, app windows, IPC handlers,
  file dialogs, downloads, persistent Python worker supervision, and per-task
  subprocesses for cancellable work.
- `electron/preload.cjs` exposes the safe `window.jobAssistant` bridge used by
  React.
- `src/main.jsx` is the renderer composition root: it owns application state and
  wires the panels together, and mounts everything inside
  `src/components/ErrorBoundary.jsx` so a render bug degrades to a readable
  message instead of a blank window.
- `src/lib/` holds non-visual code — `constants` (vocabularies and layout
  constants), `format` (pure formatting/parsing/classification), and `dialogs`
  (`appConfirm` / `appNotice` / `appPrompt`; the native `window.confirm` family
  freezes input in this Electron build).
- `src/components/` holds the UI, layered: `primitives`, `chips`, `panels`,
  `modals`, `workspace`, `dashboard`, `campaign`, `hiddenMarket`, `settings`.
  `tests/test_frontend_structure.py` asserts the shape (composition root stays
  thin, the boundary stays mounted, no native dialogs).
- `src/styles.css` contains the app theme, layout, responsive rules, and component
  styling.

## Python Bridge And Workflows

- `python_bridge.py` is the entrypoint and dispatch table. In `--serve` mode it
  accepts framed newline-delimited requests from Electron; as a one-shot command
  it reads JSON from stdin and writes one result/error frame to stdout. The
  command implementations live in `bridge/`, grouped by command prefix
  (`runtime`, `documents`, `lanes`, `jobs`, `scrapers`, `intel`, `insights`,
  `corpus`, `settings`). Each module declares its own `COMMANDS` mapping and
  `python_bridge.py` merges them, refusing duplicate keys — adding a command
  means editing one module. `bridge/runtime.py` owns the stdout protocol;
  `use_protocol_stream` exists because assigning `_OUTPUT_STREAM` from
  `python_bridge` would bind the name on the wrong module and send every
  protocol frame to stderr.
- `facade.py` backs the `database_manager` and `llm_handler` facades. It forwards
  attribute writes into the package so monkeypatching still reaches the code
  that runs, and proxies `DB_FILE` / `DATA_DIR` / `_wal_enabled` to
  `db.connection` so exactly one binding exists. Read its module docstring
  before changing either facade.
- `app_logic.py` coordinates long-running workflows: keyword generation, scraping,
  job analysis, live analysis, and application preparation.
- `concurrency.py` provides shared pause, resume, and cancel events used by LLM
  calls, scrapers, and task loops.
- `db_setup.py` creates and migrates the SQLite schema.
- `database_manager.py` is the facade over the `db/` package, which owns SQLite
  access, settings, jobs, pipeline stages, profiles/lanes, scraper metadata,
  application kits, candidate memory, campaign planning, and dashboard queries.
  Layer order (a module imports only from earlier layers): `connection`,
  `constants`, `text`, `companies`, `settings`, `scrapers`, `lanes`, `outcomes`,
  `jobs`, `campaign`, `intel`, `dashboard`. Import `database_manager`, not `db`.
- The **Funnel feedback loop** lives in `database_manager.py`:
  `ensure_application_outcome` / `set_application_outcome` capture and advance the
  immutable `application_outcomes` snapshots on stage transitions;
  `backfill_application_outcomes` reconstructs history once on migration;
  `_compute_role_key` collapses re-advertised roles; `compute_funnel_insights`
  produces the conversion analytics and the `funnel_conversion_priors` used by
  `composite_score_with_prior`; `mine_interview_validated_fragments` mines
  interview-validated candidate fragments. Bridge commands: `funnel:insights`,
  `funnel:mineInterviewFragments`, and `jobs:logExternal` (external-application
  capture). `get_interview_hygiene_nudges` powers the dashboard outcome nudges.
- **Near-miss outcomes and channel attribution** extend that loop:
  `record_application_outcome_detail` writes the `final_round` / `runner_up`
  states plus `interview_stage_reached` and `loss_reason`;
  `application_channel` derives `board` / `recruiter` / `warm_referral` /
  `direct_outreach`; `backfill_outcome_channels` and
  `repair_orphaned_outcome_snapshots` are the flag-gated one-shot migrations
  (`_recover_job_dimensions` rebuilds a deleted job's dimensions from
  `job_postings`, then `application_events`). Bridge commands:
  `funnel:outcomeDetail`, `funnel:outcomeVocabulary`.
- **Targeting** (`database_manager.py`): `PRIOR_CLAMP_BY_DIMENSION` /
  `PRIOR_SCALE_BY_DIMENSION` give `seniority_band` more authority than the other
  prior dimensions; `COMPOSITE_MATCH_WEIGHT` / `COMPOSITE_FRAGMENT_WEIGHT` are
  the named composite weights (60/40, mirrored in `llm_handler._compose_score`
  and `src/main.jsx`); `seniority_band_yields` and `band_triage_note` expose the
  observed rate per band for the triage gate; `explain_composite_score` makes a
  band demotion legible; `get_targeting_summary` powers the Targeting dashboard
  card. Bridge commands: `targeting:summary`, `targeting:explainScore`.
- **Warm channel** (`database_manager.py`): `warm_contacts` CRUD
  (`list_warm_contacts`, `upsert_warm_contact`, `seed_warm_contacts`) is the
  contact book — deliberately *not* the `people` table, which is the candidate's
  own identity for `candidate_fragments`. `get_warm_channel_activity` drives the
  weekly idle nudge. Bridge commands: `hiddenMarket:addTarget` (create a lead
  against a named employer with no scraped job), `warmContacts:list|save|delete|seed`,
  `warmChannel:activity`.
- **Channel warmth on jobs** (`database_manager.py`): `jobs.channel` stores an
  explicit channel that overrides `application_channel`'s derivation, set via
  `set_job_channel`. `channel_warmth` derives the ranking dimension
  (`WARMTH_WARM` / `WARMTH_NAMED` / `WARMTH_COLD`); `warm_contact_index` is the
  single grouped lookup and `warm_path_for_job` matches a job's real employer
  against it; `annotate_channel_warmth` attaches channel, warmth and warm path
  to a batch of job dicts. `_sort_campaign_candidates` ranks warmth ahead of the
  campaign score. `get_channel_mix` powers the cold-mix dashboard nudge and
  lists live roles with an untapped contact. Bridge command: `jobs:setChannel`.
- **Job flags** (`db/jobs.py`): `update_job_flags` / `get_job_flags` /
  `add_job_flag` / `dismiss_job_flag` / `clear_job_flags` store the flags raised
  at triage. `job_flags_json` holds the record; `job_flags_types` is a
  denormalised comma-separated type list so the board can filter without parsing
  JSON per row. Manual flags carry `source: "manual"` and survive re-analysis.
  Nothing branches on a flag: `bridge.jobs.report_job_flags` only writes them to
  the task log during generation. Bridge commands: `jobs:addFlag`,
  `jobs:dismissFlag`, `jobs:clearFlags`, `jobs:setDocumentTrack`.
- **Triage packet export** (`bridge/jobs.py`): `command_jobs_export_shortlist`
  writes one markdown and/or JSON packet per sweep (ad text, position
  description, metadata, scores with the stored analysis, flags, and warm-path
  hits) via `_shortlist_entry` and `_shortlist_markdown` into `shortlists_dir()`,
  settable with the `shortlists_dir` app setting so it can be a watched folder.
  Flagged roles stay in the packet by default; callers may narrow it with
  `exclude_flags`. Bridge command: `jobs:exportShortlist`.

## LLM And Document Generation

- `llm_handler.py` is the facade over the `llm/` package, which talks to local
  OpenAI-compatible servers and optional cloud providers. Layer order:
  `providers` (the concurrency gate, HTTP transport, the `_call_*` family),
  `parsing` (reasoning-block stripping, JSON recovery), `prompts`, `analysis`
  (triage, full analysis, deep gatekeeping), `documents`, `memory`, `research`.
  Import `llm_handler`, not `llm`.
  Flagging lives inside triage: `_triage_job` scores the role and raises flags in
  one call, `_extract_mandatory_requirements` is the deterministic pre-pass that
  hands it the ad's own "must have" lines, `_normalise_job_flags` drops any flag
  that fails to name a requirement, and `_persist_flags` writes them without ever
  raising. Triage receives the full advertisement rather than an extract.
- `rich_application.py` carries the two-track document strategy:
  `resume_task(track)` and `cover_task(today, name, track)` swap in the
  stripped-back briefs, and `generate_rich(..., document_track=...)` selects
  them. The track itself is resolved by `database_manager.document_track` /
  `resolve_document_track` from the `seniority_direction` triage reported, the
  title band, and the salary band, with `jobs.document_track` as a manual
  override.
- `context_library.py` indexes resumes, cover letters, KSC responses, PDFs, and
  other candidate evidence into a local TF-IDF retrieval store.
- `corpus_miner.py` mines reusable candidate-memory fragments from indexed
  evidence.
- `generate_application.py` is a standalone markdown-first generation path used
  for testing or manual runs.
- `rich_application.py` is the richer context-driven document generation engine.
- `application_doc_builder.py` renders structured generated content into DOCX
  templates by replacing known placeholders and template sections.
- `hybrid_renderer.py` renders markdown/plain-text resume and cover-letter drafts
  into DOCX.

## Scraping

- `scraper_plugins.py` discovers, validates, installs, stores, and loads scraper
  plugins.
- `scraper_dispatcher.py` resolves a source name to a scraper plugin and executes
  it.
- `SCRAPER_PLUGIN.md` documents how to build and validate a scraper plugin.
- `scraping_helpers.py` contains shared Selenium, HTTP, PDF, and scraper lifecycle
  helpers.
- `scraper_plugins/` contains custom scraper plugin folders, including Seek,
  LinkedIn, Deakin, NGA.NET, and PageUp-powered boards when present.

## Configuration And Runtime Data

- `config.py` contains non-secret local defaults only. Personal details and API
  keys should be entered through app settings or an untracked local override.
- `settings/local_llm_settings.json` stores Local endpoint URL, model, and
  optional local API key.
- `requirements.txt`, `package.json`, and `vite.config.js` describe runtime and
  build dependencies. `requirements.lock` is the fully pinned tree the
  runtime-prep scripts and the CI cache actually use; regenerate it with
  `tools/write_requirements_lock.py`. `pyproject.toml` holds the ruff and pytest
  configuration; `eslint.config.mjs` holds the renderer lint rules.
- `defaults/` is the only first-run content packaged into the installer, and it
  must stay neutral and committed. `search_terms.json` and `scraper_plugins/` at
  the repo root are gitignored personal runtime data and are not packaged; see
  `defaults/README.md` for why, and `tests/test_packaging_manifest.py` for the
  assertions that keep it that way.
- Generated search terms are stored per lane in the database (`lane_terms`
  table, via `database_manager.save_lane_terms` / `get_lane_terms`).
  `defaults/search_terms.json` is a legacy seed file that the Electron shell
  copies into the writable workspace; the Python backend no longer reads it.
- `settings/`, `applications/`, `older_applications/`, `Application templates/`,
  `Resumes/`, `Backups/`, `.electron-data/`, `dist/`, `build/`, `release/`,
  `installer/`, and `node_modules/` are runtime, generated, packaged, or
  third-party artifacts. They are not application source code and should be
  reviewed separately before sharing a repository or installer.

## Maintenance Notes

- Keep stdout from `python_bridge.py --serve` reserved for JSON protocol frames;
  route diagnostics to stderr.
- Keep GPU acceleration disabled in Electron because the local LLM needs GPU
  memory.
- Do not store live API keys, personal contact details, generated resumes, or the
  local SQLite database in source files or packaged defaults.
- Run `python -m pytest tests -q`, `python -m ruff check .`, and `npm run lint`
  before pushing; CI gates all three platform installer builds on them.
