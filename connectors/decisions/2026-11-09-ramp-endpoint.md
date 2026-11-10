# Ramp endpoint: the sandbox MCP server, with OAuth, and local fallback until we have a token

**Decided.** We read Ramp through `https://demo-mcp.ramp.com/mcp`, Ramp's sandbox
(demo.ramp.com) MCP server, with an OAuth access token from `RAMP_MCP_TOKEN`.
Without a token, or if anything fails to load, `connect_sources` serves the local
dataset and `describe()` reports Ramp as `local` with the reason.

**What we probed on 2026-11-09** (POST `initialize`, streamable HTTP):

| URL | Result |
|---|---|
| `https://mcp.ramp.com/mcp-apps/demo/mcp` (from our research) | 404 Not Found |
| `https://mcp.ramp.com/mcp` (production) | 401, OAuth |
| `https://demo-mcp.ramp.com/mcp` (sandbox, named in agents.ramp.com docs) | 401, OAuth (auth code + PKCE, dynamic registration at `/register`) |
| `https://mcp.ramp.com/developer/mcp` (a directory listing said no auth) | 404 |
| `mcp.ramp.com/{demo,sandbox,anything}/mcp` | 401 for any path ending `/mcp`: the auth check runs before routing, so a 401 does not prove a route exists |

No public no-auth Ramp demo server exists that we could find. The sandbox
server is the closest thing, and it needs a demo.ramp.com login. Ramp's docs also
say custom clients need their OAuth redirect URI allowlisted by Ramp.

**Considered.**
- Ramp Developer API (REST, client credentials). Documented and scriptable, but
  we promised Ramp's MCP server, and it needs a developer app on a Ramp account.
- The open-source `ramp-public/ramp_mcp` server. Archived on 2026-07-17 in favour
  of the hosted server.
- Production `mcp.ramp.com/mcp`. Real company data: out of scope.

**Why.** It is the server we promised, it holds demo data and not a real
company's books, and the fallback keeps the agent unblocked while we wait for a
token. Nothing is ever reported live unless it loaded completely.

**Not verified yet.** No MCP payload has been seen. The mapper follows Ramp's
Developer API OpenAPI spec, the tool names in `DEFAULT_TOOLS` are guesses (only
`GetAllReducedUsers` is named in Ramp's docs), and the test fixtures are
synthetic. A missing tool, a second page of results, or a receipt that Ramp names
but does not return all fail the connection rather than silently drop records.

**Contract mismatches to raise as a `contract-change`.**
1. `Person.role`: Ramp's `role` is a permission level (`BUSINESS_ADMIN`), not a
   job role like `cfo`. We leave it empty, so role-based approval rules cannot be
   judged from Ramp alone.
2. `Document.path` and `sha256`: Ramp gives presigned URLs that expire in an hour.
   We download every file when connecting, and the path is relative to a cache
   folder, not a dataset folder.
3. `Document.origin`: Ramp has no such field. We derive it from the attachment
   type (receipt, invoice: external; email, file: internal).
4. Approvals: Ramp has a chain with a status (`PENDING`, `APPROVED`). The contract
   only has decisions, so "still waiting" and "never routed" look the same.
5. Card `paid_date`: Ramp's `settlement_date` exists, but the contract does not
   define paid for a card. Left empty, kept in `raw`.
6. Bills have no department, and `category` is one string where a bill can carry
   several GL accounts (we leave it empty then).
7. `SpendPolicy`: Ramp exposes no receipt threshold or approval rules in this
   shape. Policy stays local.

**What would change our mind.** A no-auth demo server from Ramp; or the sandbox
token flow proving impossible for a custom client (then the Developer API on the
sandbox); or the real MCP payloads differing from the Developer API shapes.
