# JSE

A local-first desktop assistant for the whole job hunt: find listings, score them against one or more career lanes, manage the application pipeline on a Kanban board, research employers, generate tailored application material in your own voice, and read the market around you — all on your own machine.

Your data stays yours. JSE runs locally, stores everything locally, and can run its matching and assessment entirely against a local LLM with no cloud calls. It also gets smarter the more you use it: every application you write becomes part of a private knowledge base it draws on to match and pitch you better next time.

> [!NOTE]
> Free and staying that way. If it saves you some time or sanity, you can [buy me a coffee](https://ko-fi.com/keljian) — it keeps me caffeinated and the commits coming.

> [!IMPORTANT]
> The current packaged betas are unsigned. Download them only from the official
> JSE release page. Windows users should follow the
> [Windows installation and checksum guide](INSTALL_WINDOWS.md).

## Download JSE

| Platform | Download |
| --- | --- |
| Windows x64 | [Open the latest JSE release](https://github.com/Keljian/JSE/releases) and download the `.exe` installer. |
| macOS Apple Silicon | [Open the latest JSE release](https://github.com/Keljian/JSE/releases) and download the `arm64` `.dmg`. |
| macOS Intel | [Open the latest JSE release](https://github.com/Keljian/JSE/releases) and download the `x64` `.dmg`. |
| Ubuntu x64 | [Open the latest JSE release](https://github.com/Keljian/JSE/releases) and download the `.deb` or `.AppImage`. |

[View all releases, release notes, and published checksums](https://github.com/Keljian/JSE/releases).

---

## Contents

- [Download JSE](#download-jse)
- [Features](#features)
- [Quick Start](#quick-start)
- [What You Need First](#what-you-need-first)
- [Run From Source](#run-from-source)
- [Automated Installer Builds](#automated-installer-builds)
- [First-Run Setup In The UI](#first-run-setup-in-the-ui)
- [Local LLM Setup](#local-llm-setup)
- [Cloud AI Setup](#cloud-ai-setup)
- [Scraper Setup](#scraper-setup)
- [Intelligence Workspace](#intelligence-workspace)
- [Data Folders](#data-folders)
- [Privacy & Data](#privacy--data)
- [Typical Workflow](#typical-workflow)
- [Troubleshooting](#troubleshooting)
- [More Documentation](#more-documentation)
- [License](#license)
- [Support](#support)

---

## Features

### Discovery & matching

- **Plugin-driven scraping.** Pull listings from multiple job sites via per-source scraper plugins, enabled globally or per lane with configurable location and page limits.
- **Self-repairing searchers.** Diagnose an unhealthy scraper, have the configured local LLM produce and verify a replacement in an isolated dry run, and roll back the applied repair if needed.
- **Multiple lanes, run in parallel.** Maintain several career pathways (lanes) and search, score, and manage them all at the same time, each against its own resume and rules.
- **Tiered local-LLM assessment.** A local model triages jobs in stages — quick initial match first, then a deeper fragment/full match with approach notes covering candidate strengths, weaknesses, and how to position against the role — so cheap passes filter before expensive ones run.

### Candidate knowledge base (Fragment / RAG)

- **Your applications become a corpus.** Past applications and documents are broken into fragments and indexed, so the system actually learns about you as a candidate rather than re-reading a single static resume.
- **Better-fitting matches.** That accumulated knowledge feeds matching, surfacing roles that genuinely fit your background, not just keyword overlap.

### Application generation

- **Company research, including questions to ask.** Pull together employer context before you apply, with suggested questions for the interview.
- **Documents in your own voice.** Application material is generated through the cloud LLM of your choice (e.g. Gemini, which is cheaper than Claude for this), written in your own words and style by drawing on your fragments and older applications.

### Pipeline & tracking

- **Full Kanban workflow.** Move jobs through every stage on a board, from found to outcome.
- **Interview & feedback tracking.** Record interviews and capture interview feedback against each application.
- **Follow-up scheduling.** Keep a follow-up schedule so nothing goes cold by accident.
- **Auto-archiving.** Applications with no direct follow-up from the employer over a set period are archived automatically, keeping the active board clean.
- **Database management.** Local SQLite store with backup and management tooling.

### Market intelligence

- **Hidden-market analysis.** See which recruiters are posting the matching jobs in your area, where your resume has gaps against demand, and which companies are hiring most heavily.
- **Current-market analysis.** A rolling overview of how many available jobs match you over the last week/month, how many applications are out there, and how your own applications are progressing (cut-through rates).
- **Explainable opportunity ranking.** Recruiters, employers, and possible leadership gaps are ranked using lane fit, recurrence, recency, momentum, identity confidence, contactability, and observed outreach outcomes, with source jobs retained for audit.
- **Durable outreach intelligence.** Structured local-LLM strategies, channels, opening messages, evidence, questions, follow-ups, and cautions are saved locally with each target.

### By design

- **Local-first.** Settings, documents, database, browser profiles, and backups all live on your machine, and matching/assessment can run with no outbound calls.

---

## Quick Start

This is the shortest path for someone who wants to use JSE, not develop it.

1. **Download JSE.** Open the [latest releases and installers](https://github.com/Keljian/JSE/releases). Choose the Windows `.exe`, the appropriate macOS `.dmg`, or the Ubuntu `.deb`/`.AppImage`, and check the published SHA-256 checksum before running it.
2. **Install the unsigned beta.** On Windows, if SmartScreen appears, confirm the file and checksum, select **More info**, then **Run anyway**. See the [Windows installation guide](INSTALL_WINDOWS.md) for the full safe-install process. On macOS, confirm that the image came from the official JSE release before approving any unsigned-app warning. On Ubuntu, open the `.deb` with Software Install, or make the AppImage executable with `chmod +x` and run it.
3. **Complete the three-step setup.** JSE checks its bundled runtime and Chrome, asks for a lane name and base `.docx` resume, then asks you to choose LM Studio or Ollama for local matching.
4. **Start your local model.** Install only one local runtime, download a chat/instruct model that suits your hardware, start its server, then use **Settings -> AI & Credentials** to confirm the endpoint, model name, and connection.
5. **Enable a searcher.** In **Settings -> General -> Searchers**, enable one available plugin globally and for your lane. If none are available, import a compatible scraper plugin or create one with **Build A Scraper Plugin**.
6. **Run a small search.** Start with a one- or two-page limit. When listings arrive, run analysis and confirm that jobs are being scored before widening the search.
7. **Add your history when ready.** Past resumes, cover letters, and applications are optional, but adding them to the candidate knowledge base makes matching and generated documents more representative of you.

Cloud AI credentials are optional. Add one later if you want to use a hosted
provider for employer research or application-document generation.

---

## What You Need First

For the packaged Windows, macOS, or Ubuntu beta, you need:

- 64-bit Windows, an Apple Silicon/Intel Mac, or Ubuntu 22.04+ x64, with enough free space for the application, local data, and any local AI models you download.
- [Google Chrome](https://www.google.com/chrome/) for browser-based job searchers.
- A base resume in `.docx` format for your first search lane.
- One local AI runtime—[LM Studio](https://lmstudio.ai/download) or [Ollama](https://ollama.com/download)—plus a downloaded chat/instruct model for private, high-volume matching.
- Internet access when downloading JSE or models and when visiting employer and job-search sites.

You do **not** need to install Node.js, npm, Python, Electron, or Python packages
when using a packaged installer; they are bundled. A cloud AI account is also
not required for local matching.

Optional extras include past applications to seed the candidate knowledge base,
DOCX templates for generated documents, and Gemini, OpenAI, or Claude credentials
for cloud-assisted research and document generation.

The app stores working data locally. Generated documents, settings, databases, browser profiles, and backups should be treated as private user data.

---

## Run From Source

Source setup is intended for developers and for platforms without a published
installer. Install Node.js, npm, Python 3.11+, and Google Chrome, then run the
platform launcher from the repository root:

On Windows:

```powershell
.\Run.bat
```

On macOS:

```bash
./Run.command
```

The launcher creates the required local folders and Python virtual environment,
installs missing Python and JavaScript packages, starts Vite, and launches
Electron. The first run takes longer; later starts reuse the installed
dependencies.

Manual setup is still supported:

```powershell
npm install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

After manual setup, start JSE with:

```bash
npm run start
```

---

## First-Run Setup In The UI

1. Open **Settings**.
2. In the **Lane/Profile** area, choose or import your resume.
3. Set your preferred location, work modes, page limits, and matching rules.
4. Seed the candidate knowledge base: copy any past applications, cover letters, or older resumes into the `older_applications/` folder. JSE indexes these into fragments so matching and generated documents draw on your real history, not just the single resume above. This is optional but recommended — it's the difference between keyword overlap and matching that understands you as a candidate. You can add to this folder at any time and re-index later.
5. In **AI & Credentials**, choose your Document AI provider.
6. In **Searchers**, import scraper plugins or use **Build A Scraper Plugin** to
   generate one with your configured local LLM, then run a dry run before using
   it in searches.
7. Configure either a local endpoint or cloud provider credentials.
8. In **Searchers**, enable the sources you want for the active lane.
9. Run a small search first, with a low page limit, to confirm everything works.

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

Scrapers are plugin-driven. Each source can be enabled globally and separately
for the active lane.

1. Open **Settings -> General -> Searchers**.
2. Confirm the desired scraper plugins are registered.
3. Enable each scraper with **Available** and **This lane**.
4. Configure location, page limits, or source-specific fields where available.
5. Run a small search to check selectors, login/session requirements, and duplicate handling.

### Build A Scraper Plugin

If JSE does not already have a scraper for a job board or employer careers page,
use **Build A Scraper Plugin** in **Settings -> General -> Searchers**.

The builder asks for:

- source name and company name;
- careers/search URL;
- mode: keyword search or sweep all listings;
- platform hint, such as PageUp, Workday, SmartRecruiters, or custom;
- default location, test keyword, and page limit;
- any notes you know about listing cards, pagination, detail pages, or fields to capture.

The builder uses your configured local OpenAI-compatible LLM to generate a local
plugin under `scraper_plugins/`. Generated plugins stay local and are ignored by
Git by default.

After generation, click **Dry run**. A dry run tests the scraper with a low page
limit and `dry_run=True`, so it can fetch and parse sample listings without
writing jobs to the database. Review the dry-run output before enabling the
plugin for real searches.

### Diagnose And Repair A Scraper

Searcher health and repair controls live beside each plugin in **Settings ->
General -> Searchers**:

- **Diagnose** runs a low-page test against a disposable database and records
  whether the scraper is healthy, degraded, or broken.
- **Repair** gives the configured local LLM the existing plugin, its manifest,
  current page reconnaissance, and diagnostic evidence. JSE tries up to three
  candidate corrections and applies one only after its dry run passes.
- **Roll back** restores the exact plugin version saved before the most recent
  repair.

Normal searches also update scraper health. A search returning no jobs is
tracked as an empty result rather than an error; repeated exceptions and failed
diagnostics are the stronger broken-scraper signals. Verified repairs are
installed as local data-directory overrides, leaving the shipped plugin source
untouched and retaining a private local backup for rollback.

For the full plugin contract and manual build instructions, see `SCRAPER_PLUGIN.md`.

---

## Intelligence Workspace

Open **Intelligence** from the main navigation. It is divided into four views:

- **Market Signals** compares the current and prior half of the selected 30,
  60, or 90-day window across title families, skills, salary bands, locations,
  work modes, and sources. Daily local snapshots retain a longer trend history.
- **Targets** ranks recruiter, direct-employer, and possible leadership-gap
  opportunities. Expand a target's evidence to inspect the contributing jobs,
  classification reasons, counter-evidence, confidence, freshness, and score
  explanation.
- **Outreach** tracks contacts separately from applications, including channel,
  status, notes, touchpoints, next-step dates, saved strategy, and conversion to
  the application pipeline.
- **Outcomes** compares response, meeting, and conversion performance by target
  type, outreach channel, and opportunity-score band. JSE uses these observed
  outcomes to calibrate future target rankings.

**Build strategy** uses the configured local model to create a structured, editable
outreach strategy: positioning, contact persona, recommended channel, opening
message, evidence to reference, questions, follow-up sequence, and cautions.
Strategies and market snapshots remain in the local JSE data store. JSE shows
coverage percentages because sparse salary, contact, or structured-role data
should reduce confidence rather than masquerade as certainty.

Before writing a person-specific strategy, JSE reconciles names, email addresses
and phone numbers across the source advertisements, then checks publicly indexed
organisation/team pages and professional-profile search results. Candidate
contacts retain source URLs, freshness and confidence. If a name conflicts with
an email owner—or two people remain similarly plausible—strategy generation
pauses until you select the intended person. Public LinkedIn result metadata may
be used for discovery, but JSE does not authenticate to or scrape LinkedIn
profiles. Public research requires internet access; failures fall back safely to
advertisement evidence or an organisation-level strategy.

Leadership-gap targets are hypotheses, not confirmed vacancies. Their cards
show counter-evidence and confidence; validate the reporting structure before
making a speculative approach.

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
2. Seed `older_applications/` with past applications so the fragment knowledge base has something to learn from.
3. Configure your local model (assessment) and, optionally, a cloud model (document generation).
4. Enable scrapers for each lane.
5. Generate or edit search terms.
6. Run search across your active lanes.
7. Let the local LLM triage new jobs — initial match first, then deeper fragment/full match with approach notes.
8. Move promising jobs across the Kanban board.
9. Research companies, including questions to ask, where needed.
10. Generate tailored application documents in your own voice via your cloud model.
11. Track applications, interviews, interview feedback, follow-ups, outcomes, and feedback.
12. Work your follow-up schedule; stale, un-actioned applications auto-archive.
13. Review market intelligence — hidden-market and current-market analysis — to steer where you focus next.

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

- `CHANGELOG.md` — notable product and behaviour changes.

- `ARCHITECTURE.md` — workflows, dataflows, and system overview.
- `CODE_REFERENCE.md` — app-owned source map.
- `SCRAPER_PLUGIN.md` — scraper plugin contract.
- `JSE_FUNCTIONALITY_BRIEF.md` — product/workflow summary.

---

## License

JSE is open source under the [MIT License](LICENSE). You can use, copy, modify,
merge, publish, distribute, sublicense, and sell copies of the software, provided
the MIT copyright and permission notice are included with copies or substantial
portions of the software.


---

## Support

JSE is open-source and free to use. If it saved you time or sanity on the job hunt, a coffee keeps the project caffeinated and the commits coming: **☕ [ko-fi.com/keljian](https://ko-fi.com/keljian)**
