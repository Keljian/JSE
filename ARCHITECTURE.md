# Architecture, Workflows, And Dataflows

JSE is a local-first desktop application for job discovery, fit analysis,
pipeline management, employer research, and tailored application document
generation. The app is split into an Electron + React frontend and a Python
backend reached only through a JSON command bridge.

JSE is distributed under the MIT License; see `LICENSE` in the repository root.
If it saved you time or sanity on the job hunt, a coffee keeps the project
caffeinated and the commits coming: https://ko-fi.com/keljian

## System Overview

```mermaid
flowchart LR
    User["User"] --> UI["React UI\nsrc/main.jsx"]
    UI --> Preload["Preload API\nwindow.jobAssistant"]
    Preload --> Electron["Electron Main\nmain.cjs"]
    Electron --> Worker["Persistent Python Worker\npython_bridge.py --serve"]
    Electron --> TaskProc["Per-Task Python Process\npython_bridge.py command"]
    Worker --> Bridge["Bridge Dispatch\npython_bridge.py"]
    TaskProc --> Bridge
    Bridge --> Logic["Workflow Logic\napp_logic.py"]
    Bridge --> DB["Database Layer\ndatabase_manager.py"]
    Bridge --> LLM["LLM Layer\nllm_handler.py"]
    Bridge --> Docs["Document Engines\napplication_doc_builder.py\nrich_application.py\nhybrid_renderer.py"]
    Bridge --> Scrapers["Scraper Registry\nscraper_plugins.py\nscraper_dispatcher.py"]
    Scrapers --> Web["Job Boards / PDFs"]
    LLM --> Providers["Local LLM / OpenAI-compatible / Gemini / Claude / OpenAI"]
    DB --> SQLite["SQLite DB\nsettings/job_applications.db"]
    Docs --> Files["Generated DOCX / JSON / Markdown\napplications/"]
```

## Source Boundaries

- End-user setup: `README.md`.
- Frontend: `src/main.jsx` and `src/styles.css`.
- Desktop shell: `electron/main.cjs` and `electron/preload.cjs`.
- Bridge: `python_bridge.py`.
- Workflow orchestration: `app_logic.py`.
- Persistence: `database_manager.py` and `db_setup.py`.
- LLM integration: `llm_handler.py`.
- Evidence retrieval: `context_library.py` and `corpus_miner.py`.
- Document generation: `application_doc_builder.py`, `rich_application.py`,
  `hybrid_renderer.py`, and `generate_application.py`.
- Scraping: `scraper_plugins.py`, `scraper_dispatcher.py`,
  `scraping_helpers.py`, `scraper_plugins/`, and legacy built-ins under
  `scrapers/`.
- Runtime/generated data: `settings/`, `applications/`, `older_applications/`,
  `Application templates/`, `Resumes/`, `Backups/`, `.electron-data/`, `dist/`,
  `build/`, `release/`, `installer/`, and `node_modules/`.

## Process Model

The application uses two Python execution paths.

```mermaid
sequenceDiagram
    participant UI as React UI
    participant Main as Electron Main
    participant Worker as python_bridge.py --serve
    participant Task as Fresh Python Task

    UI->>Main: invoke(command, payload)
    Main->>Worker: framed JSON request with id
    Worker-->>Main: framed JSON result/error
    Main-->>UI: Promise resolves/rejects

    UI->>Main: startTask(command, payload)
    Main->>Task: spawn one process for task
    Task-->>Main: JSON log/progress/result frames
    Main-->>UI: streaming events
    UI->>Main: cancel task
    Main->>Task: terminate process
```

- One-shot calls use the persistent worker so imports and DB warmup happen once.
- Long-running work uses a fresh process so cancellation is reliable.
- In worker mode, stdout is protocol-only newline-delimited JSON. Diagnostics
  must go to stderr.
- `concurrency.py` provides shared pause/resume/cancel primitives for loops, LLM
  calls, and scrapers.

## Primary Workflows

### 1. App Startup

```mermaid
flowchart TD
    Start["Run.bat / Run.command"] --> Bootstrap["First-run bootstrap: folders, .venv, npm packages"]
    Bootstrap --> Npm["npm run start"]
    Npm --> DevServer["Vite dev server"]
    Npm --> Electron["Electron app"]
    Electron --> Window["Create BrowserWindow"]
    Electron --> Worker["Start persistent Python worker"]
    Window --> React["Load React UI"]
    React --> Init["invoke app:init"]
    Init --> Bridge["python_bridge.py dispatch"]
    Bridge --> DBSetup["Ensure schema and defaults"]
    Bridge --> Snapshot["Return profiles, settings, jobs, dashboard data"]
```

### 2. Search And Scrape

```mermaid
flowchart TD
    User["User selects lane, sources, search options"] --> UI["Search modal"]
    UI --> Task["task:start scrape/search"]
    Task --> Logic["app_logic.execute_scraping_and_analysis"]
    Logic --> Registry["scraper_plugins registry"]
    Registry --> Plugin["Selected scraper plugin"]
    Plugin --> Web["Job board pages / PDFs"]
    Web --> Extract["scraping_helpers detail extraction"]
    Extract --> Normalize["metadata extraction and source normalization"]
    Normalize --> Store["database_manager.add_job"]
    Store --> Dedupe["URL and identity dedupe"]
    Dedupe --> Jobs["jobs table"]
    Jobs --> OptionalAnalysis["optional fit analysis"]
```

Key data captured:

- title, company, advertiser/source, location, URL
- description and extracted PDF text
- closing date, contact details, work mode, salary signals
- source, keyword, profile/lane association

### 3. Job Fit Analysis

```mermaid
flowchart TD
    Request["Run analysis"] --> Jobs["Fetch jobs to analyze"]
    Jobs --> Resume["Load lane resume/context"]
    Resume --> TriageCache["Resume triage cache"]
    TriageCache --> FastTriage["Fast keep/discard triage"]
    FastTriage -->|discard| Reject["Auto reject or skip"]
    FastTriage -->|keep| Full["Evidence-anchored full analysis"]
    Full --> Gate{"Score >= deep gate threshold?"}
    Gate -->|yes| Deep["Strict deep gatekeeper"]
    Gate -->|no| Structured["Structured analysis result"]
    Deep --> Structured
    Structured --> Fragments["Candidate-memory alignment"]
    Fragments --> Score["Composite score"]
    Score --> DB["Update job analysis fields"]
    DB --> UI["Refresh dashboard/pipeline"]
```

The analysis layer uses `llm_handler.py` and may call:

- local OpenAI-compatible models
- OpenAI-compatible remote APIs
- Gemini
- Claude
- deterministic fallback paths where configured or required

### 4. Pipeline Management

```mermaid
flowchart LR
    UI["Pipeline / Workspace UI"] --> Update["jobs:update / events:add / interviews:add"]
    Update --> Bridge["python_bridge.py"]
    Bridge --> DB["database_manager.py"]
    DB --> Tables["jobs\napplication_events\ninterviews\napplication_kits"]
    Tables --> Refresh["app:refresh / jobs:detail / dashboard:get"]
    Refresh --> UI
```

Pipeline stages include:

- `new`
- `interested`
- `applied`
- `interviewing`
- `offer`
- `rejected`
- `rejected_by_company`
- `archived`

Tracked state includes next actions, due dates, priority, application date,
notes, feedback, interviews, rejection reasons, generated documents, and timeline
events.

### 5. Company Research

```mermaid
flowchart TD
    Trigger["Research single job or stage"] --> Detail["Load job details"]
    Detail --> Classify["Classify advertiser vs employer signals"]
    Classify --> LLM["llm_handler company research"]
    LLM --> Evidence["Evidence, confidence, summary"]
    Evidence --> DB["jobs/company profile cache"]
    DB --> UI["Company tab and hidden-market views"]
```

The research flow is intentionally cautious. It should distinguish recruiter or
advertiser information from the likely hiring company when the ad provides
enough evidence.

### 6. Application Document Generation

```mermaid
flowchart TD
    User["Generate docs for job"] --> Load["Load job, lane settings, resume, templates"]
    Load --> Context["Retrieve candidate evidence\ncontext_library.py"]
    Context --> Prompt["Build role-specific document prompt"]
    Prompt --> LLM["Generate structured content or markdown"]
    LLM --> Validate["Parse/repair/check JSON or markdown"]
    Validate --> Render["Render DOCX"]
    Render --> Save["applications/ output files"]
    Save --> Kit["application_kits and job document fields"]
    Kit --> UI["Document viewer/actions"]
```

Document paths:

- Structured template path: `llm_handler.py` -> `application_doc_builder.py`.
- Rich context path: `rich_application.py`.
- Markdown/plain-text render path: `hybrid_renderer.py`.
- Standalone/manual path: `generate_application.py`.

Outputs can include:

- tailored resume DOCX
- cover letter DOCX
- generated content JSON
- external-LLM prompt Markdown
- review/quality metadata

### 7. Candidate Memory And Context Library

```mermaid
flowchart TD
    Sources["Resumes, cover letters, KSCs, PDFs, prior applications"] --> Ingest["context_library.ingest"]
    Ingest --> Extract["DOCX/PDF/DOC/TXT extraction"]
    Extract --> Classify["Document type and role family classification"]
    Classify --> Corpus["context_documents"]
    Corpus --> Retrieve["TF-IDF retrieval for target job"]
    Retrieve --> Generation["Application generation context"]
    Corpus --> Mine["corpus_miner / memory extraction"]
    Mine --> Fragments["candidate_fragments / profile_memory_fragments"]
    Fragments --> Analysis["Fragment alignment and scoring"]
```

Candidate memory is used to:

- improve application generation grounding
- score alignment against reusable evidence fragments
- suggest lane-fragment affinities
- evolve search terms and targeting signals

## Data Stores

### SQLite

The main SQLite database lives in the configured data directory. Common table
families include:

- profiles/lanes and settings
- app settings and credentials metadata
- scraper plugins and lane scraper overrides
- jobs and job metadata
- application events and interviews
- company intelligence/profile cache
- candidate fragments and profile memory fragments
- context documents and resume triage cache
- generated application kits and local LLM tasks
- campaign actions, plans, and reporting data

### Filesystem

- `settings/`: local app data, `local_llm_settings.json`, browser profiles,
  context corpus cache, and DB when using the app data directory.
- `applications/`: generated application outputs.
- `Application templates/`: local DOCX templates.
- `Resumes/`: local managed resume files.
- `Backups/`: manual or automated backups.
- `.electron-data/`: Electron runtime profile/cache.
- `dist/`, `build/`, `release/`, `installer/`: generated build/package output.

## Command/Data Boundary

The frontend sends command names and JSON payloads to Electron. Electron forwards
them to Python and returns only JSON-serializable results/events.

```mermaid
flowchart LR
    React["React component"] --> Invoke["window.jobAssistant.invoke"]
    React --> StartTask["window.jobAssistant.startTask"]
    Invoke --> IPC["Electron IPC"]
    StartTask --> IPC
    IPC --> Python["python_bridge.py command dispatch"]
    Python --> Result["JSON result/error/log frames"]
    Result --> IPC
    IPC --> React
```

This boundary keeps UI rendering separate from scraping, LLM calls, database
access, and document generation.

## Privacy And Sharing Model

JSE is local-first and can contain sensitive data. Before sharing source or
build artifacts, review:

- API keys and provider settings
- local endpoint settings in `settings/local_llm_settings.json`
- local SQLite databases
- resumes, cover letters, and generated application documents
- browser/session profiles
- backups and packaged installers
- context corpus caches and extracted document text

Source files should remain free of personal details and live credentials.
Runtime data should stay ignored or be explicitly exported by the user.

## Operational Constraints

- Keep Electron GPU acceleration disabled so local LLMs can use GPU memory.
- Keep the Python worker stdout protocol clean.
- Prefer structured parsing and database helpers over ad hoc string manipulation.
- Keep scraper plugins optional and metadata-driven.
- Treat generated folders and installer copies as outputs, not source of truth.
