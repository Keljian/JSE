# Commute & pay screening — integration handoff

Status as of 11 Aug 2026. Written for whoever picks this up next.

> **Update, 11 Aug 2026 (later the same day).** `geo.py` and `salary.py` are now
> on disk and `tools/verify_screening.py` passes every check. Items 1, 2, 3, 5, 6
> and 7 of "Not started" are done; only the employer-portal question remains, and
> it is a decision rather than a task. See "Current state" below.

## What this is

Deterministic screening of scraped jobs on **commute** and **pay**, running in
plain Python *before* any LLM call. Two problems it solves:

1. Every sweep returned roles that were never viable — wrong side of the city,
   interstate, or below the pay floor — and a model was being asked to judge
   each one. Location and salary are facts about a posting, not judgements.
2. The previous filtering had no location logic at all. No commute, postcode,
   suburb or distance handling existed anywhere in the codebase.

## Design constraints that drove the shape

**JSE ships to other people.** The first attempt hardcoded Melbourne suburb
sets and bundled an Australia Post CSV. That was thrown away. Nothing in the
current code knows what a suburb is called in any country.

**Search radius and commute radius are different numbers.** `search_radius_km`
is how wide to cast the net at scrape time; `max_commute_km` is what is
acceptable to travel. Search is normally *wider*: a fully-remote role
advertised in another city is worth returning, and a search clamped to the
commute radius would never surface it.

**Blocked never means hidden.** A screened-out job keeps its row, its verdict
and a human-readable reason. It is skipped for analysis and left out of the
shortlist, nothing more. The engine's brief is explicit that false negatives
cost more than false positives, so a role vanishing with no explanation is the
worst available outcome.

**Anything unresolved passes.** Geocoder outage, unparseable location, missing
salary — all yield "unknown", which never blocks. Absence of evidence about a
job is a gap in our data, not a fact about the job.

## Modules

| File | Layer | Purpose |
|---|---|---|
| `geo.py` | leaf | Commute model. Pure coordinate math, no place names. |
| `salary.py` | leaf | Currency- and locale-aware pay parsing. |
| `screening.py` | leaf | Adapter: builds a model from lane settings, screens a job. |
| `db/geocode.py` | db | SQLite geocode cache + Nominatim provider. |
| `tools/verify_screening.py` | tool | End-to-end check. Run this first. |
| `tools/backfill_screening.py` | tool | Re-parse existing rows. Dry run by default. |

`geo.py`, `salary.py` and `screening.py` import nothing from `db/`, `llm/` or
`bridge/`, so the layering test stays green.

### How the commute model works

Three signals, all derived from coordinates:

- `distance_km` — great-circle, home to job.
- `sector` — compass bearing from the **metro centre** to the job, bucketed to
  eight points. "Eastern, north-eastern and northern suburbs" is
  `accepted_sectors = "E,NE,N"`, a user setting rather than a code constant.
- `crosses_centre` — detour ratio. If travelling via the centre is barely
  longer than going direct, the centre is on the path. Cross-town commutes are
  far slower than their straight-line distance suggests. Purely geometric, so
  it holds in any city without knowing its road network.

The metro centre discovers itself: reverse-geocode home, take the city from the
returned admin hierarchy, forward-geocode that name. Two lookups once per
profile, both cached. The user never names their nearest city.

### Why geocoding is affordable

A real 40-job sweep resolved to **20 distinct location strings**, and 21 of
those jobs shared just two of them. Those strings recur every sweep, so steady
state is almost entirely cache hits. That is what makes a 1-request-per-second
public geocoder viable for a 200-job run. Failed lookups are cached too, as
rows with NULL coordinates — without that, an unresolvable string is retried
forever.

### Salary parsing

Everything scales off a per-currency reference wage (`REFERENCE_WAGE`).
¥6,000,000 is an ordinary annual salary; $6,000,000 is not. Hardcoded
thresholds in one currency cannot express that.

Handles European decimal separators, Indian lakh grouping, `k`/`M` suffixes,
superannuation both inclusive and on-top, ranges, and pay-period inference with
prior probabilities.

## Bugs already found and fixed — do not reintroduce

- **Employer name carries location.** Melton City Council advertises its jobs
  as "Melbourne VIC". Ranked first until the gate also read the advertiser.
- **Do not scan the `analysis` field for hybrid signals.** It is our own prose
  about hybrid arrangements, so everything looked flexible.
- **Geocode precision matters.** "Melbourne VIC" resolves to a city centroid
  that may be 30km from the office. Coarse matches downgrade to review, never
  block.
- **Sector is a tie-breaker, not a veto.** Vetoing on sector blocked a role
  23km away while passing one at 38km. Sector only applies beyond
  `preferred_km`.
- **A job at the centre does not "cross" the centre.** Handled by
  `centre_radius_km`.
- **Prose salary extraction must require a salary keyword.** Without
  `strict=True` it invented a $54,000 contract rate for a role whose ad quotes
  no salary at all, and produced the same figure for two unrelated employers by
  reading reference numbers and dates. This one is dangerous: it would have
  filtered out the best role on the list.
- **Period inference needs priors.** `$1300 - $1400` inferred *weekly*, because
  a contractor day rate annualises well above a median wage. Weekly quoting is
  rare; daily is not.
- **Seniority penalties must yield to stated pay.** A `seniority_below` flag
  fired on a role advertising $131k–$146k. Money is harder evidence than title
  shape.
- **The date guard must not eat ranges.** The rule that skips numbers inside
  dates and serial numbers first matched the gap in `$1300 - $1400`, silently
  discarding the low end of every range. The separator has to be flush against
  the digits (`12/03/2026`) to count.
- **Never block on the cross-town penalty alone.** The travel-time uplift for a
  commute through the centre could push a job inside the stated maximum over the
  line. The user set that number; a modelled penalty may raise a question about
  a trip within it, never overrule it. Blocking is decided on real distance.
- **A read-side default must not be written back.** `home_location` falling back
  to `preferred_location` on read meant the next settings save persisted the
  copy, freezing the anchor so a later change to the search location stopped
  moving it. The fallback lives in `screening.Screener`, and the settings UI
  shows it as a placeholder.
- **Scraper junk annualises to nonsense.** The `salary` column holds truncated
  values like `$8` and `$4`. The plausibility band's floor sits at a quarter of
  the reference wage, below which no lawful full-time rate exists in any
  currency.
- **The analysis query did not select the columns the screen reads.**
  `get_jobs_to_analyze` fetched seven columns, none of them `location`, `salary`
  or the company pair, so in the real pipeline the screener read None for every
  field and passed every job. It failed silently and looked correct, because
  passing on missing data is exactly what it is supposed to do. `verify_screening.py`
  could not catch it: that script issues its own SELECT. Both analysis queries
  now share `_ANALYSIS_COLUMNS`. **If you add a signal to the screen, add its
  column there.**
- **An employer name is not a place name.** Geocoders match one to a street or
  hamlet anywhere on earth. A consultancy advertising in "Sydney or Melbourne or
  Tokyo" — a string that resolves to nothing — matched a European address and
  the role was set aside on a commute of 16,614km. The employer is only trusted
  when it agrees with the posted location, or, when nothing was posted, when it
  is at least in the same country as home.

## Environment traps (PowerShell 5.1)

- `Set-Content -Encoding UTF8` writes a **BOM**, which breaks `ast.parse`. Use
  `[System.IO.File]::WriteAllText` with `New-Object System.Text.UTF8Encoding($false)`.
- Multi-line string anchors passed to `.Replace()` **silently fail** against
  CRLF files. Use `[regex]::Replace` with `\r?\n`, and always verify the edit
  landed rather than trusting the exit code.
- Interpreter is `.venv\Scripts\python.exe`. Bare `python3` is not on PATH.

## Current state

### Done and verified
- Schema: 11 `jobs` columns, 10 `profiles` columns, `geocode_cache` table.
  Migrations are idempotent via `_add_column` and have been run.
- `db/geocode.py`, `screening.py`, `tools/verify_screening.py` written.
- Lane settings resolve. `home_location` falls back to the existing
  `preferred_location`, so no profile needs re-entry.
- `save_job_screening` in `db/jobs.py`, exported through `db/__init__.py` and
  `database_manager.py`.
- Screen wired into `_perform_analysis_loop` — the single choke point both
  `analyze_jobs` and `analyze_specific_jobs` share. Wrapped in try/except: if
  screening breaks, everything is analysed rather than silently dropped.
- SEEK manifest exposes `radius_km`.

- `geo.py` and `salary.py` are on disk. `verify_screening.py` passes every
  check, the full suite is green (262 passed), and the renderer builds.
- **Search radius reaches every source.** One lane setting drives all three:
  SEEK snaps it to a distance SEEK actually honours (0, 5, 10, 25, 50, 100km),
  LinkedIn and HiringCafe receive miles. A manifest entry declares the unit its
  source speaks and `convert: "km_to_miles"` bridges, so nobody types the same
  distance twice — see `CONFIG_CONVERTERS` in `scraper_plugins.py`.
- **Settings UI.** "Commute & Pay Screening" under Search: enable, home
  location, unit, comfortable and maximum commute, eight direction toggles, pay
  floor and currency. Search radius sits with Locations. `_screening_values` in
  `db/settings.py` normalises on the way in and is deliberately lenient: a
  radius nobody can express degrades to screening nothing, never to a filter
  nobody asked for.
- **Shortlist export** carries the screening fields and renders a commute line
  and a salary line that shows the raw ad text beside what the parser made of
  it. Blocked rows are left out by default; `include_screened_out` asks for
  them. The screening columns are in `PIPELINE_SUMMARY_COLUMNS` too, so the
  list UI can show a set-aside job's reason.
- **Backfill:** `tools/backfill_screening.py`. Dry run by default, `--apply` to
  write, `--commute` opt-in because it geocodes. `save_job_screening` now writes
  only the keys it is given, so re-parsing pay does not erase a commute result.
  Raw `salary` text is never touched.

Run:

```
.venv\Scripts\python.exe tools\verify_screening.py
```

### The one open decision

**Employer portals** (Deakin, Monash, Knox, Maroondah, LaTrobe, Swinburne) have
one fixed location each, so a radius means nothing to them. The original plan
was to geocode the employer once and decide whether to run the source at all.

That is worth thinking about before building. Every job those portals return is
already screened individually, and the employer name resolves precisely, so
source-level skipping buys scrape time and nothing else. What it risks is
dropping an entire employer on one geocode — the largest possible false
negative, against a brief that says false negatives cost more than false
positives. A university 60km away still advertises roles that are remote, or on
a campus nearer than its registered address.

The safe version is advisory: each portal manifest declares its location, and a
sweep logs "Deakin is 62km from home, past your 45km limit" while still running.
Auto-skip only if the user turns it on.

### Not yet configured

Screening is live but anchored on `preferred_location` ("Melbourne VIC"), which
is a city centroid, so every Melbourne job currently measures 0km and passes.
It starts discriminating once a real home suburb is set in Settings → Search →
Commute & Pay Screening. Nothing is wrong; it is unconfigured.

## Suggested settings for this profile

`accepted_sectors = "E,NE,N"`, `max_commute_km = 40`,
`search_radius_km = 50`, `salary_floor = 120000`.

Screening runs with defaults regardless; these sharpen it.
