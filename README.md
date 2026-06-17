# JSE — Job Application Assistant

A local-first desktop assistant for the whole job hunt: find listings, score them against one or more resume lanes, manage the application pipeline, research employers, and generate tailored application material — all on your own machine.

Your data stays yours. JSE runs locally, stores everything locally, and can run entirely against a local LLM with no cloud calls.

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

- **Lane-based matching.** Maintain multiple resume "lanes" and score each job against the most relevant one, rather than one generic profile.
- **Plugin-driven scraping.** Enable per-source scrapers globally or per lane, with configurable location and page limits.
- **Local or cloud AI.** Drive analysis and document generation from a local OpenAI-compatible endpoint (LM Studio, Ollama, llama.cpp, vLLM) or from OpenAI, Claude, or Gemini.
- **Tailored document generation.** Produce resumes and cover letters from your own DOCX templates, tuned to each listing.
- **Pipeline tracking.** Move jobs through stages and track applications, interviews, follow-ups, outcomes, and feedback in a local SQLite database.
- **Employer research.** Pull together company context where you need it before applying.
- **Local-first by design.** Everything — settings, documents, database, browser profiles, backups — lives on your machine.

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

JSE's Local provider expects an OpenAI-compatible chat completions endpoint:

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

Cloud providers are optional. In **Settings -> AI & Credentials**:

- **ChatGPT/OpenAI**: set the API key, optional OpenAI-compatible base URL, and model.
- **Claude**: set the API key and model.
- **Gemini**: set the API key and model.

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
| `older_applications/` | Prior documents used as evidence/corpus input |
| `Application templates/` | Local DOCX templates |
| `Resumes/` | Managed resume copies |
| `Backups/` | Database/document backups |
| `.electron-data/` | Electron runtime profile/cache |
| `job_applications.db` | Local SQLite database when stored in the project folder |

---

## Privacy & Data

JSE is local-first. Settings, generated documents, the database, browser profiles, and backups all live on your machine, and the app can run entirely against a local LLM with no outbound calls.

A few things to keep in mind:

- The folders listed above can contain resumes, application history, and session cookies. Do not share them unless you intend to share their contents.
- If you enable a cloud provider, job and resume content for those workflows is sent to that provider. Use a local endpoint if you want to keep everything on-device.
- Keep API keys out of source control. Store them through Settings.

---

## Typical Workflow

1. Configure a lane and resume.
2. Configure a local or cloud AI provider.
3. Enable scrapers for the lane.
4. Generate or edit search terms.
5. Run search.
6. Analyse new jobs.
7. Move promising jobs through the pipeline.
8. Research companies where needed.
9. Generate tailored application documents.
10. Track applications, interviews, follow-ups, outcomes, and feedback.

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

- Confirm the selected AI provider is configured.
- Confirm the resume path is valid.
- Confirm template paths are valid if using DOCX templates.
- Try the external prompt workflow if the local model struggles with long JSON output.

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
