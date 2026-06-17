# JSE — Job Application Assistant (AKA Job Search Engine)

A local-first desktop assistant for the whole job hunt: find listings, score them against one or more career lanes, manage the application pipeline on a Kanban board, research employers, generate tailored application material in your own voice, and read the market around you — all on your own machine.

Your data stays yours. JSE runs locally, stores everything locally, and can run its matching and assessment entirely against a local LLM with no cloud calls. It also gets smarter the more you use it: every application you write becomes part of a private knowledge base it draws on to match and pitch you better next time.

> [!NOTE]
> Free and staying that way. If it saves you some time or sanity, you can [buy me a coffee](https://ko-fi.com/keljian) — it keeps me caffeinated and the commits coming.

---

## Contents

- [Features](#features)
- [What You Need First](#what-you-need-first)
- [Install Dependencies](#install-dependencies)
- [Start The App](#start-the-app)
- [First-Run Setup In The UI](#first-run-setup-in-the-ui)
- [Local LLM Setup](#local-llm-setup)
- [Cloud AI Setup](#cloud-ai-setup)
- [Scraper Setup](#scraper-setup)
- [Data Folders](#data-folders)
- [Privacy & Data](#privacy--data)
- [Typical Workflow](#typical-workflow)
- [Troubleshooting](#troubleshooting)
- [More Documentation](#more-documentation)
- [Support](#support)

---

## Features

### Discovery & matching

- **Plugin-driven scraping.** Pull listings from multiple job sites via per-source scraper plugins, enabled globally or per lane with configurable location and page limits.
- **Multiple lanes, run in parallel.** Maintain several career pathways (lanes) and search, score, and manage them all at the same time, each against its own resume and rules.
- **Tiered local-LLM assessment.** A local model triages jobs in stages — quick initial match first, then a deeper fragment/full match with approach notes covering candidate strengths, weaknesses, and how to position against the role — so cheap passes filter before expensive ones run.

### Candidate knowledge base (Fragment / RAG)

- **Your applications become a corpus.** Past applications and documents are broken into fragments and indexed, so the system actually learns about you as a candidate rather than re-reading a single static resume.
- **Better-fitting matches.** That accumulated knowledge feeds matching, surfacing roles that genuinely fit your background, not just keyword overlap.

### Application generation

- **Company research, including questions to ask.** Pull together employer context before you apply, with suggested questions for the interview.
- **Documents in your own voice.** Application material is generated through the cloud LLM of your choice (e.g. Gemini, which is cheaper than Claude for this), written in your own words and style by drawing on your fragments and older applications — not generic boilerplate.

### Pipeline & tracking

- **Full Kanban workflow.** Move jobs through every stage on a board, from found to outcome.
- **Interview & feedback tracking.** Record interviews and capture interview feedback against each application.
- **Follow-up scheduling.** Keep a follow-up schedule so nothing goes cold by accident.
- **Auto-archiving.** Applications with no direct follow-up from the employer over a set period are archived automatically, keeping the active board clean.
- **Database management.** Local SQLite store with backup and management tooling.

### Market intelligence

- **Hidden-market analysis.** See which recruiters are posting the matching jobs in your area, where your resume has gaps against demand, and which companies are hiring most heavily.
- **Current-market analysis.** A rolling overview of how many available jobs match you over the last week/month, how many applications are out there, and how your own applications are progressing (cut-through rates).

### By design

- **Local-first.** Settings, documents, database, browser profiles, and backups all live on your machine, and matching/assessment can run with no outbound calls.

---

## What You Need First

Before first launch, have these ready:

- Node.js and npm for the Electron/React desktop app.
- Python 3.11+ with the packages in `requirements.txt`.
- Google Chrome installed for Selenium-based scraping.
- A resume file for your first lane, preferably DOCX or PDF.
- Optional DOCX templates for resume and cover-letter generation.
- Either a local OpenAI-compatible LLM server or cloud API credentials.

The app stores working data locally. Generated documents, settings, databases, browser profiles, and backups should be treated as private user data.

---

## Install Dependencies

From the project folder:

```powershell
npm install
pip install -r requirements.txt
```

If you are using a packaged build, dependencies may already be bundled. For development or a first source checkout, install both the Node and Python dependencies.

---

## Start The App

On Windows:

```powershell
.\Run.bat
```

On macOS:

```bash
./Run.command
```

Both launchers run:

```bash
npm run start
```

This starts the Vite frontend and then launches Electron.

---

## First-Run Setup In The UI

1. Open **Settings**.
2. In the **Lane/Profile** area, choose or import your resume.
3. Set your preferred location, work modes, page limits, and matching rules.
4. In **AI & Credentials**, choose your Document AI provider.
5. Configure either a local endpoint or cloud provider credentials.
6. In **Scrapers**, enable the sources you want for the active lane.
7. Run a small search first, with a low page limit, to confirm everything works.

---

## Local LLM Setup

JSE uses a local model for job assessment and matching. The Local provider expects an OpenAI-compatible chat completions endpoint:

```text
POST <base_url>/chat/completions
```

Configure it in **Settings -> AI & Credentials**:

- `Document AI`: `Local endpoint`
- `Local base URL`: your local server's OpenAI-compatible `/v1` URL
- `Local model`: the model name your server expects
- `Local API key`: optional; leave blank for local servers that do not require one

These Local LLM values are stored in the settings directory as `local_llm_settings.json`.

Common examples:

| Runtime | Typical base URL | Model field example |
| --- | --- | --- |
| LM Studio | `http://localhost:1234/v1` | loaded model name |
| Ollama | `http://localhost:11434/v1` | `llama3.1:8b`, `qwen2.5:14b` |
| llama.cpp server | `http://localhost:8080/v1` | served model alias |
| vLLM | `http://localhost:8000/v1` | model name passed to vLLM |
| Unsloth/vLLM-style local server | `http://localhost:8888/v1` | served model name |

Notes:

- The local endpoint must be running before analysis or document generation.
- The model must support chat-style prompts.
- JSON-mode support is strongly recommended, because JSE asks for structured outputs in several workflows.
- Larger context windows improve analysis and document quality. A practical target is 16k–32k context or higher.

---

## Cloud AI Setup

Cloud providers are optional, and mainly used for application-document generation (the heavier "write it in my voice" step). In **Settings -> AI & Credentials**:

- **ChatGPT/OpenAI**: set the API key, optional OpenAI-compatible base URL, and model.
- **Claude**: set the API key and model.
- **Gemini**: set the API key and model.

> [!TIP]
> Document generation runs against whichever cloud model you pick. Gemini is a cost-effective choice here relative to Claude for the volume of generation JSE does.

> [!WARNING]
> Do not put live API keys in source files. Store them through Settings or another private local mechanism.

---

## Scraper Setup

Scrapers are plugin-driven.

1. Open **Settings -> Scrapers**.
2. Confirm the desired scraper plugins are registered.
3. Enable each scraper globally and for the current lane.
4. Configure location and page limits per scraper where available.
5. Run a small search to check selectors, login/session requirements, and duplicate handling.

For building new scrapers, see `SCRAPER_PLUGIN.md`.

---

## Data Folders

These folders/files are local runtime data and may contain private information:

| Path | Contents |
| --- | --- |
| `settings/` | App settings, `local_llm_settings.json`, local database, browser/session profiles, context cache |
| `applications/` | Generated resumes, cover letters, prompts, and JSON content |
| `older_applications/` | Prior documents used as evidence/corpus input for the fragment knowledge base |
| `Application templates/` | Local DOCX templates |
| `Resumes/` | Managed resume copies |
| `Backups/` | Database/document backups |
| `.electron-data/` | Electron runtime profile/cache |
| `job_applications.db` | Local SQLite database when stored in the project folder |

---

## Privacy & Data

JSE is local-first. Settings, generated documents, the database, browser profiles, and backups all live on your machine, and matching/assessment can run entirely against a local LLM with no outbound calls.

A few things to keep in mind:

- The folders listed above can contain resumes, application history, your fragment knowledge base, and session cookies. Do not share them unless you intend to share their contents.
- If you enable a cloud provider for document generation, the job and resume/fragment content for those workflows is sent to that provider. Keep generation local-only if you want everything on-device.
- Keep API keys out of source control. Store them through Settings.

---

## Typical Workflow

1. Configure one or more lanes, each with its own resume.
2. Configure your local model (assessment) and, optionally, a cloud model (document generation).
3. Enable scrapers for each lane.
4. Generate or edit search terms.
5. Run search across your active lanes.
6. Let the local LLM triage new jobs — initial match first, then deeper fragment/full match with approach notes.
7. Move promising jobs across the Kanban board.
8. Research companies, including questions to ask, where needed.
9. Generate tailored application documents in your own voice via your cloud model.
10. Track applications, interviews, interview feedback, follow-ups, outcomes, and feedback.
11. Work your follow-up schedule; stale, un-actioned applications auto-archive.
12. Review market intelligence — hidden-market and current-market analysis — to steer where you focus next.

---

## Troubleshooting

**Local model does not respond**

- Confirm the local server is running.
- Confirm the base URL ends with `/v1`.
- Confirm the model name exactly matches what the server exposes.
- Try a small model/prompt in the server's own UI first.

**Scraping returns no jobs**

- Lower the page limit and test one source.
- Check whether the site layout changed.
- Check whether the site requires login, cookies, or manual verification.
- Confirm the scraper is enabled for the active lane.

**Document generation fails**

- Confirm the selected cloud AI provider is configured.
- Confirm the resume path is valid.
- Confirm template paths are valid if using DOCX templates.
- Try the external prompt workflow if the model struggles with long JSON output.

**App starts but data looks missing**

- Check **Settings -> Local Folders**.
- Confirm the settings directory points to the expected app data location.
- Confirm you are using the intended database, not an empty fresh one.

---

## More Documentation

- `ARCHITECTURE.md` — workflows, dataflows, and system overview.
- `CODE_REFERENCE.md` — app-owned source map.
- `SCRAPER_PLUGIN.md` — scraper plugin contract.
- `JSE_FUNCTIONALITY_BRIEF.md` — product/workflow summary.

---

## Support

JSE is open-source and free to use. If it saved you time or sanity on the job hunt, a coffee keeps the project caffeinated and the commits coming:

**☕ [ko-fi.com/keljian](https://ko-fi.com/keljian)**
