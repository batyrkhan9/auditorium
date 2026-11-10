# Area: connectors

Owner: Noriaki. Branches: `connectors/<topic>`, in the build clone.

Ramp (through its MCP server) and QuickBooks Online sandbox (through Intuit's MCP
server), and loading the dataset into QuickBooks.

- Deliver a live implementation of `auditorium_contracts.Sources`. Map source payloads
  to the contract records and keep the original in `raw`.
- Do not add fields the source does not really provide. If the contract does not fit
  what Ramp or QuickBooks return, ask for a contract change.
- If a live connection fails, fall back to `LocalSources` and report it through
  `describe()` as `mode: local`. Never pretend a source was live.
- First task: the researched Ramp demo URL returned 404 on 2026-11-09 (see README).

## Ramp stand-in (no Ramp login yet)

`ramp_standin.py` serves a dataset folder as a local MCP server in Ramp's payload
shapes. It is not Ramp, and `describe()` reports Ramp as `local` when connected to it.
See `decisions/2026-11-10-ramp-standin.md`.

```
uv run python -m auditorium_connectors.ramp_standin <dataset_dir> [--port 8765] [--host 127.0.0.1]
RAMP_MCP_URL=http://127.0.0.1:8765/mcp RAMP_MCP_TOKEN=anything   # any token is accepted
```

Ramp documents must be PDFs or images (a bill's raw `EMAIL` attachment may be plain
text), or the connector refuses them and falls back. A file Ramp lists but its host
answers 404 for is left off its transaction and named in `describe()`; see
`decisions/2026-11-10-ramp-files-ramp-cannot-serve.md`.

## Ramp Developer API (unattended, with approvals)

`ramp_api.py` reads Ramp with client credentials. `connect_sources` prefers it when
`RAMP_CLIENT_ID` and `RAMP_CLIENT_SECRET` are set (`RAMP_API_URL`, default
`https://demo-api.ramp.com`), then MCP, then local. See
`decisions/2026-11-10-ramp-developer-api.md`. Live test: `AUDITORIUM_LIVE_RAMP_API=1`.

Record decisions in `connectors/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
