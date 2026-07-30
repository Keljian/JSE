# Packaged defaults

Everything in this directory is committed, shipped inside the installer, and
copied into the user's data directory on first launch by
`prepareWritableWorkspace` in `electron/main.cjs`. Existing files in the
destination always win, so upgrading never overwrites a user's own data.

This exists to fix a specific problem. `search_terms.json` and
`scraper_plugins/` are gitignored personal runtime data, but the
electron-builder `files` list used to package them from the repo root. An
installer built on a developer machine therefore embedded whatever personal
search terms and local scrapers happened to be sitting there, while a CI build
from a clean checkout shipped neither — the same commit produced two different
applications depending on who built it.

## Rules

- **Only neutral, shareable content belongs here.** No personal search terms, no
  employer-specific or region-specific scrapers, no credentials, no resumes.
  Anything placed here is committed to version control and distributed to every
  user of the installer.
- **The live copies stay untracked.** `/search_terms.json` and
  `/scraper_plugins/` at the repo root remain gitignored personal data and are
  no longer packaged.

## Contents

- `search_terms.json` — a neutral starter keyword list.
- `scraper_plugins/` — scraper plugins that are genuinely product rather than
  personal. Add a plugin here only after checking it contains no personal
  configuration, hardcoded employer lists, or credentials.
