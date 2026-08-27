# JSE MCP server

Puts JSE's Python command bridge behind MCP so Claude can drive the app directly:
query the job database, move pipeline stages, export and read triage packets, and
start scrapes and analysis runs.

## Layout

    C:\JSE\mcp\jse_mcp_server.py   the server
    C:\JSE\mcp\.venv               its own virtualenv (only dependency: mcp)

The server does not import JSE. It spawns `C:\JSE\.venv\Scripts\python.exe
python_bridge.py --serve` and speaks the same newline-delimited JSON protocol
Electron uses, so JSE's own dependencies stay in JSE's venv and nothing here can
drift from the app.

Short commands share one persistent worker process, which pays the import and
SQLite warmup once. Long, cancellable commands (`scrape:run`, `analysis:run`,
`docs:*`) get their own process, the same way the app runs them, and are exposed
as background tasks.

## Registration

Registered in `%APPDATA%\Claude\claude_desktop_config.json` under `mcpServers.jse`.
Restart Claude Desktop after changing it. A timestamped backup of the previous
config sits beside it.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `JSE_ROOT` | `C:\JSE` | application root |
| `JSE_PYTHON` | `<root>\.venv\Scripts\python.exe` | interpreter that runs the bridge |
| `JSE_SHORTLISTS_DIR` | `<root>\shortlists` | where triage packets land |
| `JSE_MCP_ALLOW_DESTRUCTIVE` | `0` | `1` unlocks delete, compact and clear commands |
| `JSE_MCP_MAX_CHARS` | `60000` | response size cap |
| `JSE_MCP_TIMEOUT` | `180` | default per-call timeout, seconds |

## Tools

**Reading** `jse_health`, `jse_search_jobs`, `jse_get_job`, `jse_dashboard`,
`jse_stats`, `jse_lanes`, `jse_list_commands`

**Triage packets** `jse_list_packets`, `jse_packet_summary` (coverage, data-quality
audit, real deadlines, top roles), `jse_packet_jobs` (paged rows with filters),
`jse_export_packet`

**Writing** `jse_set_stage`, `jse_update_job`, `jse_add_note`, `jse_add_flag`,
`jse_set_channel`, `jse_log_external_application`

**Long-running** `jse_start_task`, `jse_task_status`

**Escape hatch** `jse_command` reaches any of JSE's ~117 bridge commands.

## Safety

Destructive commands (`jobs:delete`, `jobs:cleanupArchive`, `jobs:resetRejected`,
`jobs:clearFlags`, `database:compact`, `corpus:clear*`, `scrapers:remove`,
`lanes:delete`, `profiles:delete`, and the rest) are refused unless
`JSE_MCP_ALLOW_DESTRUCTIVE=1`.

`jse_update_job` writes only the fields `db.update_job_application` accepts and
reports anything else back as rejected rather than dropping it silently.

Responses strip ad text, PD text and the company-intelligence blobs, and cap the
stored analysis, so a page of results cannot swamp the context window. Over the
cap the server returns an explicit error with a preview rather than half a JSON
object.

## Concurrency with the app

SQLite runs in WAL mode, so the server can read while JSE is open. Writes from
both at once serialise through JSE's own retry handling. Running a scrape or an
analysis pass from here while one is running in the app is not a good idea.
