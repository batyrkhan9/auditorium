# Test the Ramp connector on a stand-in server, because we have no Ramp account

**Decided.** The Ramp connector runs its full MCP path against a stand-in
server we build in `connectors/`. The stand-in serves our fake company in the
payload shapes of Ramp's public Developer API spec. Runs against it report Ramp
as `local` ("stand-in, not Ramp"), never `live`. The connector reads
`RAMP_MCP_URL` and `RAMP_MCP_TOKEN`, so pointing it at Ramp's sandbox server,
`https://demo-mcp.ramp.com/mcp`, is a change of two values.

**Why.** Ramp's sandbox needs an OAuth login to a demo.ramp.com account, and
Ramp requires a registered business to open one. Neither of us has one. The URL
from our research, `https://mcp.ramp.com/mcp-apps/demo/mcp`, returned 404. A
stand-in is the only way to exercise the real MCP path (connect, list tools,
call tools, parse payloads) today.

**What this does not prove.** That Ramp's server behaves like our stand-in. We
copy the documented shapes, not Ramp's live behaviour, and we cannot see Ramp's
demo transactions. The README and every run say so.

**Considered.**
- Read the dataset from local files only. Honest, but the MCP path would go
  untested, and that path is what we promised Ramp.
- Ask someone with a registered business to open an account. Possible later; it
  depends on a person we have not found.
- Report the stand-in as live. No. PRD rule 6: say what is live.

**What would change our mind.** Access to a Ramp account. Then we run against
`https://demo-mcp.ramp.com/mcp`, fix whatever the stand-in got wrong, and
update the PRD, the README and this file.
