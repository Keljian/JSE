"""Bridge command implementations, split out of the former 3,509-line python_bridge.

`python_bridge.py` stays the entrypoint — Electron spawns it directly — and holds
only the stdout protocol loop and the merged dispatch table. Everything it used
to implement lives here, grouped by command prefix:

    runtime     the stdout protocol, workspace paths, shared row helpers
    documents   document extraction and application document generation
    lanes       lanes/profiles, resumes, search terms, candidate memory
    jobs        job listing and updates, the blocker gate, shortlist export
    scrapers    scraper plugins and search runs
    intel       hidden market, warm contacts, company research
    insights    dashboard, funnel, targeting, campaign, calendar
    corpus      context library and memory fragments
    settings    app settings, AI credentials, database maintenance

Each module declares its own `COMMANDS` mapping and `python_bridge.py` merges
them, refusing duplicate keys. Adding a command means editing one module.

This package is deliberately not imported for its side effects: import
`python_bridge` if you want the assembled dispatch table.
"""
