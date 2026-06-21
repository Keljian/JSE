# JSE Functionality Brief

For the full architectural overview, workflow diagrams, and dataflows, see
`ARCHITECTURE.md`.

JSE is a local job-search and application assistant. It helps a user find roles,
score them against one or more resume profiles, manage an application pipeline,
research employers, and generate tailored application material.

## Core Purpose

JSE combines job scraping, AI-assisted fit analysis, application tracking,
company research, candidate-memory retrieval, and document drafting in one
desktop app. The user can maintain multiple job-seeking lanes, each with its own
resume, search settings, matching rules, templates, and preferences.

## Main Workflows

1. Search for jobs
   - Generates or stores search terms per lane.
   - Runs enabled scraper plugins across sources such as Seek, LinkedIn,
     universities, councils, NGA.NET, PageUp-powered boards, and other supported
     job boards.
   - Captures title, company, location, URL, description, source, closing date,
     contact details, and metadata.
   - Deduplicates by URL and by title/company/location style matching.

2. Analyse job fit
   - Uses local OpenAI-compatible models, Gemini, Claude, OpenAI, or compatible
     APIs depending on settings.
   - Runs a staged scoring flow: resume triage cache, fast keep/discard triage,
     evidence-anchored full analysis, and strict deep gatekeeping for high-scoring
     roles.
   - Blends fit analysis with candidate-memory fragments from prior validated
     documents.
   - Supports re-analysis by stage, by selected job IDs, or by full pipeline.

3. Manage a job pipeline
   - Tracks roles through `new`, `interested`, `applied`, `interviewing`,
     `offer`, `rejected`, `rejected_by_company`, and `archived` stages.
   - Stores priority, next action, due dates, application dates, notes, feedback,
     rejection reasons, generated document paths, and timeline events.
   - Provides dashboard counts, cleanup views, stale-job detection, calendar
     follow-ups, and campaign planning.

4. Work on a specific job
   - Opens a workspace with job details, company research, application material,
     interviews, feedback, notes, and timeline.
   - Allows editing job fields and moving jobs between stages or lanes.
   - Supports adding and updating interview rounds, people met, notes, outcomes,
     and next actions.

5. Research companies
   - Separates advertiser/recruiter information from the likely actual employer
     where evidence allows.
   - Stores company intelligence, employer type, confidence, evidence, and
     summaries.
   - Can research a single company or batch research jobs in a pipeline stage.

6. Generate application material
   - Uses the job ad, resume/lane context, fit analysis, candidate-memory
     fragments, and templates to generate tailored content.
   - Supports DOCX generation via structured template rendering and a
     markdown-first path.
   - Can produce an external-LLM prompt for manual drafting workflows.
   - Saves generated documents locally under the applications data folder.

7. Extract and attach document content
   - Imports resumes into managed profile storage.
   - Extracts text from dropped resumes, cover letters, PDFs, and position
     descriptions.
   - Stores extracted document text against jobs where relevant.

8. Configure lanes, providers, and maintenance
   - Supports lane-level resume paths, preferred locations, work modes, max pages,
     score thresholds, boost/penalty terms, matching rules, and document strategy.
   - Supports app-wide AI credentials and scraper/plugin management.
   - Allows database compaction and candidate-memory scans.

## User Interface

The app has an Electron/Vite frontend backed by Python commands. The current UI
includes:

- Dashboard with stage counts, upcoming actions, and cleanup prompts.
- Campaign plan view (today's prioritised actions).
- Intelligence workspace with Market Signals, Targets, Outreach, and Outcomes
  views. It mines recruiter / direct-employer / leadership-gap signals, retains
  auditable source-job evidence and confidence, ranks targets with explainable
  opportunity scores, stores structured local-LLM outreach strategies and daily
  market snapshots, learns from response/meeting/conversion outcomes, and can
  convert a successful lead into an applied job.
- Pipeline board with job cards, scores, priorities, due dates, source badges,
  and drag/drop stage movement.
- Search, manual-job, analysis, cleanup, and confirmation modals.
- Job workspace modal with detailed tabs.
- Settings panel for lane profile, resume, search, filtering, templates, AI
  provider configuration, scraper management, and maintenance.
- Logs/status display for long-running tasks such as scraping, analysis,
  document generation, company research, and memory scanning.

## Backend Components

- `python_bridge.py`: JSON command bridge used by the desktop UI.
- `database_manager.py`: SQLite CRUD, filtering, lanes, dashboard, calendar,
  events, interviews, company research, campaign planning, and job state.
- `db_setup.py`: database schema creation and migrations.
- `app_logic.py`: orchestration for scraping, search execution, analysis, and
  application preparation.
- `llm_handler.py`: AI analysis, document content, company research, and
  provider integration.
- `scraper_plugins.py`: scraper plugin registry, import, validation, and loading.
- `scraper_dispatcher.py`: routes scrape requests to the selected plugin.
- `scraping_helpers.py`: Selenium/WebDriver, HTTP, PDF, and detail-scraping
  utilities.
- `context_library.py` and `corpus_miner.py`: evidence indexing and candidate
  memory mining.
- `application_doc_builder.py`, `rich_application.py`, `hybrid_renderer.py`, and
  `generate_application.py`: application document generation and rendering.
- `src/main.jsx`: Electron frontend application.
- `electron/`: desktop shell and preload/main process integration.
- `scraper_plugins/`: custom scraper plugin folders and manifests.

## Data Stored Locally

JSE stores its working data in a local SQLite database and local folders. Major
data areas include:

- Profiles/lanes and settings.
- Search terms and scraper configuration.
- Jobs, descriptions, extracted documents, and analysis.
- Pipeline stages, notes, feedback, due dates, and application outcomes.
- Application events and timelines.
- Interviews.
- Company research and company profile information.
- Resume triage cache, candidate fragments, and lane fragment affinities.
- Generated document source tracking.

## Important Behavioural Notes

- Long-running scraping and analysis work is cancellable/pausable.
- UI updates are routed through Electron and the Python bridge, not direct worker
  mutation.
- Scrapers should fail gracefully and log errors instead of crashing the app.
- The app is local-first: database, resumes, generated documents, settings, and
  browser profiles live on the user's machine.
- API keys and personal data should stay in local settings or untracked data
  files, not in source files or packaged defaults.

## One-Sentence Summary

JSE is a local AI-assisted job application operating system: it finds suitable
roles, screens them through a staged evidence-aware pipeline, tracks
applications, researches employers, manages interviews and follow-ups, and drafts
tailored application documents.
