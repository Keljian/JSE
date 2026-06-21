# Changelog

All notable changes to JSE are documented here.

## Unreleased

### Added

- Rebuilt the former Hidden Market area as an Intelligence workspace with
  Market Signals, ranked Targets, Outreach, and Outcomes views.
- Added explainable opportunity scores using lane fit, recurrence, recency,
  momentum, identity confidence, contactability, and observed outcomes.
- Added auditable source-job evidence, classification reasons,
  counter-evidence, confidence, freshness, and data-coverage reporting.
- Added daily local market snapshots and period comparisons for title families,
  skills, salary bands, locations, work modes, and sources.
- Added structured, persistent outreach strategies with positioning, contact
  persona, channel, opening message, evidence, questions, follow-ups, and
  cautions.
- Added response, meeting, and conversion learning by target type, outreach
  channel, and opportunity-score band.
- Added public-source contact enrichment before person-specific strategy
  generation. JSE reconciles contacts across advertisements, checks publicly
  indexed organisation and professional-profile results, retains provenance,
  and pauses for user selection when identities conflict.
- Added integration coverage for market ranking, durable strategies, outcome
  learning, contact conflicts, provenance, and selected-person prompting.

### Changed

- Renamed the main Hidden Market navigation item to Intelligence.
- Build Strategy now uses a resolved, evidence-backed person when available and
  safely falls back to an organisation-level approach when no reliable person
  can be found.
- Leadership-gap targets are explicitly treated as confidence-rated hypotheses
  rather than confirmed vacancies.
- Scraped position-description text is now attached to the Application
  workspace without replacing a document uploaded by the user.
- Document upload and path handling is more reliable in Electron.

### Privacy and safety

- Contact enrichment uses public search metadata and organisation pages only.
  It does not authenticate to or scrape LinkedIn profiles.
- Contact research, source provenance, strategies, and market snapshots are
  cached in the local JSE data store.
