# AI Job Application Assistant - Project Context

## Overview

JSE is a local-first desktop application for job discovery, fit analysis,
pipeline tracking, employer research, and tailored application document
generation. The UI is Electron + React (Vite). All business logic runs through a
Python bridge so the renderer never imports Python modules directly.

## Architecture

```text
electron/main.cjs      Electron main process, IPC, dialogs, downloads, worker supervision
electron/preload.cjs   Safe contextBridge API exposed as window.jobAssistant
src/main.jsx           React UI: dashboard, campaign, pipeline, workspace, settings
src/styles.css         Application styles and responsive layout
python_bridge.py       JSON command bridge; persistent worker for one-shot invokes
app_logic.py           Long-running workflow orchestration
llm_handler.py         LLM analysis, research, document content, memory extraction
database_manager.py    SQLite CRUD, settings, jobs, lanes, dashboard, campaign logic
db_setup.py            Database schema creation and migrations
concurrency.py         Shared pause/resume/cancel primitives
scraper_plugins.py     Scraper plugin registry, validation, import, execution helpers
scraper_dispatcher.py  Source-to-plugin dispatch
scraping_helpers.py    Selenium, HTTP, PDF, and scraper lifecycle helpers
context_library.py     Candidate evidence indexing and retrieval
corpus_miner.py        Candidate-memory fragment mining
application_doc_builder.py / rich_application.py / hybrid_renderer.py / generate_application.py
                       Application document generation and DOCX rendering
config.py              Non-secret local defaults only
```

## Key Dependencies

- `electron`, `react`, `vite` - desktop shell and frontend.
- `selenium` and Chrome WebDriver - browser-based scraping.
- `openai` - local or hosted OpenAI-compatible client.
- `google-generativeai` - Gemini provider support.
- `requests`, `pdfplumber`, `python-docx` - document/PDF extraction and DOCX generation.
- `sqlite3` - local data store, with WAL mode and retry handling.

## Runtime Model

- One-shot `bridge:invoke` calls use a persistent `python_bridge.py --serve`
  worker supervised by `electron/main.cjs`.
- Long-running cancellable tasks still spawn fresh Python processes so cancel can
  safely terminate the task.
- Bridge stdout is reserved for newline-delimited JSON protocol frames. Route
  diagnostic output to stderr.
- All LLM calls should check `concurrency.cancel_event` and `concurrency.paused`
  before executing.
- Scrapers should fail gracefully and return/log failure rather than crashing the
  app.

## Data And Privacy Rules

- Do not commit personal details, API keys, generated resumes, generated cover
  letters, browser profiles, local databases, or backups.
- API keys belong in Settings or an untracked local override, not in `config.py`.
- `settings/`, `applications/`, `older_applications/`, `Application templates/`,
  `Resumes/`, `Backups/`, `.electron-data/`, `dist/`, `build/`, `release/`,
  `installer/`, and `node_modules/` are runtime, generated, packaged, or
  third-party artifacts.
- Keep GPU acceleration disabled in Electron; the local LLM may need the GPU
  memory.

## Current Frontend

Electron/React is the only supported frontend. The legacy Tkinter UI has been
removed.

## Documentation

See `ARCHITECTURE.md` for workflows and dataflows, `CODE_REFERENCE.md` for the
app-owned source map, and `JSE_FUNCTIONALITY_BRIEF.md` for the product/workflow
summary.
