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
- `src/main.jsx` contains the application UI: dashboard, campaign plan, pipeline,
  activity, job workspace, document actions, and settings.
- `src/styles.css` contains the app theme, layout, responsive rules, and component
  styling.

## Python Bridge And Workflows

- `python_bridge.py` is the JSON command dispatcher. In `--serve` mode it accepts
  framed newline-delimited requests from Electron; as a one-shot command it reads
  JSON from stdin and writes one result/error frame to stdout.
- `app_logic.py` coordinates long-running workflows: keyword generation, scraping,
  job analysis, live analysis, and application preparation.
- `concurrency.py` provides shared pause, resume, and cancel events used by LLM
  calls, scrapers, and task loops.
- `db_setup.py` creates and migrates the SQLite schema.
- `database_manager.py` owns SQLite access, settings, jobs, pipeline stages,
  profiles/lanes, scraper metadata, application kits, candidate memory, campaign
  planning, and dashboard/activity queries.

## LLM And Document Generation

- `llm_handler.py` talks to local OpenAI-compatible servers and optional cloud
  providers. It performs resume triage, job scoring, deep gatekeeping, company
  research, document content generation, and memory-fragment extraction.
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
- `scraper_plugins/seek/` is the local Seek scraper plugin when present.
- `scraper_plugins/linkedin/` is the local LinkedIn scraper plugin when present.
- `scrapers/` contains built-in or legacy scraper modules retained for source
  compatibility, including Deakin, NGA.NET, and PageUp-powered boards.

## Configuration And Runtime Data

- `config.py` contains non-secret local defaults only. Personal details and API
  keys should be entered through app settings or an untracked local override.
- `settings/local_llm_settings.json` stores Local endpoint URL, model, and
  optional local API key.
- `requirements.txt`, `package.json`, and `vite.config.js` describe runtime and
  build dependencies.
- Generated search terms are stored per lane in the database (`lane_terms`
  table, via `database_manager.save_lane_terms` / `get_lane_terms`).
  `search_terms.json` is a legacy seed file that the Electron shell copies into
  the writable workspace; the Python backend no longer reads it.
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
