# JSE Review: Significant Improvements

Reviewed 2026-07-30 against the current tree (post Funnel Insights, local doc authoring).
Two lenses: what makes the tool convert to interviews faster, and what keeps the codebase
workable. Ordered by expected payoff.

## A. Job-outcome improvements

### 1. Add a hard-blocker gate that says "skip", not "here's an angle"
The documented failure mode of the whole search is the adjacent-candidate trap, and the
known limitation of JSE is that the analyser optimises to find a reframing rather than
admit a domain mismatch. The staged flow (triage, full analysis, deep gatekeeper) scores
fit but nothing in it is built to return a decisive negative.

Suggestion: before full analysis, run a cheap deterministic + LLM check for hard blockers:
mandatory credentials not held, core domain mismatch, seniority misalignment. Output one of
`skip / stretch-with-named-gaps / clear-fit` and persist it on the job. `skip` should
short-circuit document generation entirely. Prompt the gatekeeper with an explicit
instruction that "no" is a valid and valuable answer and that reframing a mismatch is a
failure. This single change attacks the first-stage conversion problem directly.

### 2. Make channel warmth a first-class job field
Warm channels (referrals, named contacts, recruiters) outperform cold portals by a wide
margin in observed outcomes, but the pipeline ranks by composite score, and the funnel
prior is bounded to ±10. A 70-score job with a warm contact is worth more than an
85-score cold SEEK submission.

Suggestion: add `channel` (cold_portal / named_contact / referral / recruiter / speculative)
to jobs, set from contact research or manually. Rank the campaign plan and pipeline with
warmth as a primary sort dimension, and have the dashboard nag when the applied mix skews
cold. The hidden-market contact research already finds people; connect it to triage by
surfacing "possible warm path: X" on job cards before an application is built.

### 3. One-click "triage packet" export for the survivors
The real daily loop is JSE overnight sweep, then survivors go to Claude for go/no-go and
positioning. Today that handoff is manual. Add a `jobs:exportShortlist` command that emits
a single markdown/JSON file per sweep: JD text, extracted metadata, scores with evidence,
blocker-gate verdict, any warm-path hits. Drop it in a watched folder. This removes the
most friction from the workflow that actually produces applications.

### 4. Explicit two-track document strategy
Overqualification screening on coordinator/support roles is a proven rejection cause, and
the fix (stripped-back CV) exists as a manual practice. Lanes already carry a document
strategy; make the track selection explicit: classify each role as
overqualification-risk vs senior-track during analysis, and have document generation
select the stripped-back or full-senior template accordingly, with the cover letter
addressing positioning directly on the stripped track.

## B. Engineering improvements

### 5. Break up the monoliths
Current sizes: `database_manager.py` 8,105 lines / 266 top-level defs, `llm_handler.py`
3,270, `python_bridge.py` 3,026, `src/main.jsx` 5,487 lines holding ~50 components and
121 useState hooks. Beyond human navigation cost, these files are actively hostile to the
AI-assisted development this project is built with: edits collide, context windows fill,
and unrelated functions churn in every diff.

Suggested split, mechanical and low-risk:
- `db/` package: `connection.py`, `jobs.py`, `lanes.py`, `outcomes.py` (funnel),
  `intel.py` (hidden market), `dashboard.py`, `settings.py`. Keep `database_manager.py`
  as a re-exporting facade so nothing else changes.
- `bridge/commands/` modules grouped by prefix (jobs, lanes, scrapers, intel), with the
  `COMMANDS` dict assembled from them.
- `llm/` package: `providers.py` (the _call_* family), `analysis.py`, `documents.py`,
  `research.py`.
- `src/components/` with main.jsx as composition root. Add one React error boundary at
  the top so a render bug degrades to a message instead of a white window.

### 6. Run the tests in CI, and add linting
`.github/workflows/build-installers.yml` builds installers on three platforms but never
runs pytest. There are only ~755 lines of tests against ~21k lines of Python, and zero
frontend tests, while the changelog shows heavy churn (concurrency fixes, funnel loop,
scraper builder). Add a fast test job that gates the build, then grow coverage where
regressions have actually happened: bridge command dispatch (golden request/response
tests), dedupe, funnel insights math, composite_score_with_prior bounds. Add `ruff` and
a minimal eslint config; both are near-zero maintenance and catch the swallowed-error
class cheaply (there are ~85 `except Exception` blocks in the core files, several of
which will hide real logic errors, not just scraper flakiness).

### 7. Per-task cancellation instead of global events
`concurrency.py` exposes module-level `paused` and `cancel_event` shared by everything.
That works while one long task runs at a time, but the app now parallelises analysis
workers, refresh, and scraping; a cancel intended for one task flags them all, and the
recent history of concurrency firefights (readonly-database errors, serialized LLM
requests, the keepalive-connection workaround) suggests this area is under strain.
Move to a `TaskContext` object (cancel/pause events + log callback) created per task and
passed down. Also consider funnelling all SQLite writes through a single writer-queue
thread in the bridge worker; it eliminates the whole class of cross-process write races
more robustly than the WAL keepalive pin.

### 8. Fix the packaged-personal-data inconsistency
electron-builder's `files` list ships `scraper_plugins/**/*` and `search_terms.json`,
but both are gitignored as personal/runtime data. Consequence: an installer built on the
dev machine embeds your personal search terms and local council/university scrapers,
while a CI build from a clean checkout ships neither, so the two builds behave
differently. Decide which plugins are product (commit them, e.g. seek/linkedin) and which
are personal (exclude from packaging), and ship a neutral default `search_terms.json`
generated at first run instead of packaging the live one.

### 9. Pin Python dependencies
`requirements.txt` uses only lower bounds, and the CI python-runtime cache is keyed on
its hash, so a transitive release can change behaviour between builds with no diff. Add a
lock (pip-compile or a pinned requirements.lock) used by the runtime-prep scripts.

### 10. Small hygiene items
- Stale `job_applications.db` (June) sits at repo root while the live DB is under
  `settings/`; delete or archive it to avoid a wrong-DB accident.
- `asar: false` ships loose sources and slows startup; enable asar with an unpack rule
  for the Python runtime.
- `styles.css` at 5,051 lines: split per component area when main.jsx is split, not
  before.

## Suggested order

1. Hard-blocker gate (#1) and channel warmth (#2): direct hit on the conversion problem.
2. Triage packet export (#3): cheap, removes daily friction.
3. CI test gate + ruff (#6): protects everything else you change.
4. Monolith split (#5): do it before the next big feature, not after.
5. The rest as maintenance passes.
