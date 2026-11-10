# Ramp: a local stand-in MCP server until we have a Ramp login

Decided 2026-11-10, board task #11.

**What.** `connectors/src/auditorium_connectors/ramp_standin.py` is a local MCP
server (official `mcp` SDK, streamable HTTP) that serves a contract dataset in
the payload shapes `ramp.py` reads, which follow Ramp's Developer API spec. It
has the same tool names as `DEFAULT_TOOLS`, and it serves the receipt and invoice
files over HTTP, so the connector's whole path runs for real: tool calls,
mapping, download, content check, sha256, cache. It names itself
`auditorium-ramp-standin` in the MCP `initialize` answer. When `ramp.py` sees that
name, `describe()` reports Ramp as `local` ("Ramp stand-in MCP server at <url>,
serving dataset <id>; not Ramp"), never `live`.

**Why we need it.** Ramp's sandbox MCP server needs a demo.ramp.com login, and
Ramp only gives accounts to registered businesses. Nobody on the team has one.

**Considered.**
- Wait for access. That blocks the demo and leaves the MCP client path untested.
- Ask Ramp for a sandbox login. Still worth doing, but we can't plan around it.
- Record-replay fixtures. There is nothing real to record, and replay skips the
  transport, the download and the file checks.
- A stand-in server (chosen). It exercises everything except Ramp itself.

**Why the stand-in.** It runs the same client code that real Ramp would, so most
failures show up before we get a login. Rule 6 of the PRD ("say what is live")
holds because the label comes from what the server says about itself, not from a
setting someone could get wrong. The payloads are the reverse of our own
mapping, so they show the mapping is consistent. They do not show that it
matches Ramp. Fields that do not survive the round trip are listed at the top of
`connectors/tests/test_ramp_standin.py`. Any bearer token is accepted, and the
server listens on 127.0.0.1 by default.

**What would change our mind.** A Ramp sandbox login. Then we point
`RAMP_MCP_URL` at Ramp, record real payloads, fix the mapping where they differ,
and keep the stand-in only for offline tests.
