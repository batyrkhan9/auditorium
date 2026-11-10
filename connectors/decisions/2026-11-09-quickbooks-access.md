# QuickBooks: call the REST Accounting API from Python, not Intuit's MCP server

Decided 2026-11-09, board task #4.

**What.** `connectors/src/auditorium_connectors/qbo.py` talks to the QuickBooks
Online Accounting API directly with `httpx`. It uses the OAuth 2.0 refresh-token
grant, `GET /v3/company/{realm}/query` to read data, `POST /v3/company/{realm}/{entity}`
to create records, and `minorversion=75`. It reads Bill, Purchase and
JournalEntry into `LedgerEntry`, and loads a dataset's `ledger.jsonl` into the
sandbox. Loading refuses to run unless `QBO_ENVIRONMENT=sandbox`.

**What else we considered.** Intuit's official MCP server
(github.com/intuit/quickbooks-online-mcp-server). It is TypeScript on Node 18+,
runs locally as a stdio subprocess and wraps the same REST API (CRUD on 29
entities, including bills, purchases and journal entries). The PRD names it.

**Why REST.**
- The connector is Python behind `Sources`. Using the MCP server would add Node
  and a subprocess speaking JSON-RPC, and in return we would get the same REST
  calls with less control over paging, errors and the raw payload. `raw` has to
  be exactly what QuickBooks returned.
- Mapping must be deterministic and testable offline. Tests run the client
  through `httpx.MockTransport` and never touch the network.
- The MCP server keeps rotated refresh tokens by writing them back into `.env`.
  We never touch `.env`. We keep the newest token in
  `~/.cache/auditorium/qbo_token.json` (mode 600, outside the repo), tied to a
  hash of the `.env` token it came from.
- The agent can still use the MCP server as a tool later. Both use the same app
  keys and sandbox company.

**Mapping calls worth knowing.**
- Bills and Purchases list only their expense lines. The credit side is the
  header `APAccountRef` or `AccountRef`, which QuickBooks posts the total to. It
  becomes the balancing line only when the payload names it.
- A Purchase with `Credit: true` is a card refund. Its `total_cents` is
  negative and its sides are flipped.
- Item-based lines name an item, not an account, so they stay in `raw` only.
- A journal entry has no header vendor. `vendor_name` is set only when its lines
  name exactly one vendor entity.
- To make loading idempotent, the loader adds an `auditorium-ref: <ledger id>`
  line to PrivateNote. Mapping strips that line from `memo`, and `raw` keeps it.
  Ids come back as QuickBooks' own ids, `qbo:<Entity>:<Id>`, not the dataset's.
- The loader skips an entry rather than invent anything: no lines, lines that
  don't add up to the total, a bill with no vendor, an expense with no
  paid-from account, or a type other than bill, expense or journal entry.
  New accounts get an `AccountType` guessed from the name. Only sandbox test data
  is affected, and the type never comes back into the contract.

**Contract mismatches found (contract-change candidates, not edited here).**
1. `LedgerLine` has no account type or number. The loader has to guess types,
   and the agent can't tell an accrual liability from an expense except by name.
2. `department` is per line in the contract. In QuickBooks, `DepartmentRef`
   (the "Location") is per transaction, and per-line `ClassRef` has no home.
3. `LedgerEntry` has no vendor per line. Journal entries carry a vendor on each
   line in QuickBooks.
4. There is no `PaymentType` (CreditCard, Cash or Check) on expenses, and no
   due date on bills. Both are in `raw` only.
5. Live ledger ids differ from a dataset's ledger ids once the data is loaded. The
   eval must verify evidence against the same `Sources` the run used.
6. Dataset ledger entries need `lines` for the loader to work. The contract's
   own example entry has none.

**What would change our mind.** The agent needing QuickBooks as a live tool in
its loop rather than as a fetched ledger. A hosted Intuit MCP endpoint that
manages tokens for us. Or the REST client growing past what a thin wrapper
should be.

Sources:
- https://github.com/intuit/quickbooks-online-mcp-server
- https://blogs.intuit.com/?p=35282 (Intuit MCP server early preview)
- https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0
- https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries
- https://blogs.intuit.com/?p=34635 (minor version 75 is the minimum)
- https://developers.intuit.com/app/developer/qbo/docs/release-notes/general-release-notes
- https://www.nango.dev/blog/quickbooks-oauth-refresh-token-invalid-grant (refresh-token rotation)
