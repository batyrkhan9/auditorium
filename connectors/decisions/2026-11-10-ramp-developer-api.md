# Ramp: read the Developer API too, and prefer it when its keys are set

Decided 2026-11-10, board task #16.

**What.** `ramp_api.py` reads Ramp through its Developer API (REST) with the
client-credentials grant: client id and secret, no browser. It reads transactions,
bills, receipts, users (including suspended ones), departments and approvals, and
maps them to contract v0.1. `connect_sources` uses it when `RAMP_CLIENT_ID` and
`RAMP_CLIENT_SECRET` are set. Otherwise it uses the MCP server when `RAMP_MCP_URL`
and `RAMP_MCP_TOKEN` are set, and otherwise the local dataset. The Ramp `detail`
says which path was taken and why the others were not. A failed path falls back
to local data. It does not try the next path, so the run says plainly what failed.

**Why both paths.**
- The PRD promises Ramp's MCP server, so that path stays.
- The MCP path needs an OAuth login in a browser, and a redirect URI that Ramp
  allowlists for a custom client. The Developer API runs unattended: a demo or a
  CI job can connect with no person present.
- `GET /developer/v1/approvals` returns the approval history of card transactions
  and bills. Ramp's MCP server gives us no route from a transaction to its
  approvals. Without approvals the approval check can only say `needs_review`.

**Considered.**
- MCP only. Approvals would stay unknown, and every run would need a browser login.
- Developer API only. That breaks what we told Ramp, and the agent may still want
  MCP tools in its loop later.
- After a failed API read, falling through to MCP. We rejected this because the
  result would depend on which failure happened, and a run that quietly switched
  paths is harder to explain.

**How it stays honest.** It is the same set of rules as `ramp.py`, and it reuses
that code.
- Every page is read. We follow `page.next`, but only its query string is used,
  against our own host, so the token is never sent anywhere else. A stated
  `total_count` larger than what we read is refused. So is any other "more" marker,
  a repeated page, or more than 1000 pages.
- Money comes from `entity_amount` (integer cents, signed, so a refund is
  negative). Anything not in USD is left out and counted. Pending transactions
  are left out and counted, and declined ones are dropped.
- Approvals: steps with `step_status` `APPROVED` or `REJECTED` and a `user_id`
  become contract `Approval`s. Every other step is left out and counted in
  `describe()`, and so is a chain that is not approved yet. If the app lacks
  `approvals:read`, or the endpoint answers 403 or 404, approvals stay empty and
  `describe()` says "approvals unavailable". A 500 fails the connection.
- The token lives in memory for one process. It is never written to disk. The
  secret goes only in the Basic header, and any error text has the secret, the
  client id and the token cut out.
- A card's department stays empty, as on the MCP path, because `card_holder` gives
  today's department, not the one at the time of purchase. A person's `role` stays
  empty too, because Ramp's role is a permission level.

**Not confirmed.** Every payload follows the OpenAPI spec (read 2026-11-10). None
has been captured. The values of `step_status` are a guess, because the spec types
it as a free string. We have not checked that a client-credentials app on
demo.ramp.com is granted `approvals:read`, or that the sandbox plan allows bills
(a 403 on bills fails the connection, because missing bills means missing spend).

**What would change our mind.** A real capture whose shapes differ from the spec.
A `step_status` vocabulary that has other decisions in it. Ramp's MCP server
exposing approvals for card transactions, which would let us drop one path. Or
Ramp telling us that the demo must run on MCP only.
