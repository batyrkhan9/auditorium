# Ramp: map the real sandbox MCP tools, and leave approvals empty

Decided 2026-11-10, board task #15. This replaces the guessed tool names and shapes
in `2026-11-09-ramp-endpoint.md`.

**What.** Noriaki signed in to `https://demo-mcp.ramp.com/mcp`. We captured
`tools/list` (216 tools) and the answers of 19 read-only tools. The capture, cleaned
of cursors, e-mail addresses and URL queries, is
`connectors/tests/fixtures/ramp_sandbox_capture.json`, and the tests now run on it.
`ramp.py` calls these tools:

| Need | Tool | Arguments besides `rationale` | Next page |
|---|---|---|---|
| card transactions | `ramp_get_transactions` | `transactions_to_retrieve=all_transactions_across_entire_business`, `state=all`, `page_size=200`, `details_to_include_in_response=[submitted_items]` | `next_page_cursor`, sent back as `next_page_cursor` |
| bills | `ramp_list_bills` | `include_paid`, `include_archived=false`, `limit=50` | `next_page_cursor`, sent back as `page_cursor` |
| bill files | `ramp_get_bill_invoices` | `bill_id` | none |
| receipts | `ramp_get_receipt_details` | `receipt_uuids`, 50 per call | none |
| people | `ramp_list_users` | `page_size=100` | `next_page` (a user id), sent back as `page_cursor` |
| managers | `ramp_get_org_chart` | `user_id`, once per user | none |

Most tools require `rationale`, so every call sends one. A test checks every call
against the captured input schemas.

**How the real shapes differ from the Developer API guess.** The amount is a
decimal string `amount_value` plus `amount_currency`, not integer cents. The id is
`transaction_uuid`, the spender is `spent_by_user_uuid`, the memo is
`reason_or_justification` and the purchase time is `transaction_time`. Users carry
`department_name` but no manager. Managers come from each user's org chart
(`direct_reports`). Some failures come back as a normal answer,
`{"error": true, "message": ...}`, and we refuse those like any other error.

**Choices.**
- **Money.** We parse `amount_value` with `Decimal` into exact cents. Anything
  that is not a whole number of cents is refused. A transaction not settled in USD
  is left out, and `describe()` counts it. We don't convert currencies (the PRD
  covers USD only). We rejected two alternatives: refusing the whole connection,
  because one foreign card would keep Ramp from ever being live, and converting,
  because that is out of scope and the books hold the settled amount. A foreign
  purchase that settled in USD is kept. For example, Air Canada CAD 608.98 settled
  as USD 431.41, and the CAD figure stays in `raw`.
- **States.** Pending transactions are left out and counted, because their amount
  can still change and they are on no statement yet. Ramp leaves out declined
  transactions by default, and we exclude them again.
- **Dates and time zone.** `transaction_time` and `accounting_date` are UTC
  datetimes. We take the calendar date as Ramp wrote it, because the MCP server
  doesn't expose the business's time zone. So a purchase on the evening of Dec 31
  in San Francisco is dated Jan 1. The period check should use `accounting_date`,
  and should return `needs_review` rather than `fail` for a purchase within a day
  of a month end. `settlement_date` is a plain date and becomes `paid_date`.
- **Category.** The contract wants "the accounting category coded in Ramp", so we
  read the GL account from `accounting_categories` (requested through
  `submitted_items`). The sandbox has no accounting provider, so the field is null
  and so is our category. `merchant_category` ("Airlines") is Ramp's merchant
  type. It stays in `raw`.
- **Department.** A transaction has none. We don't copy the spender's current
  department onto past spending.
- **Approvals.** `ramp_get_approval_chain_details` needs an
  `approval_instance_uuid`, and no captured answer contains one. Passing a
  transaction id returns "not found", and the full transaction metadata has no
  approval data either. So approvals stay empty, and `describe()` says that
  empty means unknown, not unapproved. The earlier rule, "a missing chain fails
  the connection", would have kept Ramp from ever being live, so we dropped it.
  Contract v0.2 (`shared/contract-v0.2`, not merged) lets `approvals` be `None`
  for "the source could not say". Once it merges, the connector should send
  `None`. `ramp_resolve_approval_target` has not been tried yet and may lead
  from a transaction to its approval.
- **Partial reads are still refused.** We follow each tool's own cursor. Any
  other "more" marker, a stated total larger than what we read, a repeated cursor
  or more than 1000 pages makes the connection fail.

**Still unconfirmed** (synthetic fixture, Developer API shapes). The sandbox has
no bills or receipts, so we have not seen bill fields, the answer of
`ramp_get_bill_invoices`, or the answer of `ramp_get_receipt_details`. We also
don't know whether the receipt tool returns a file URL at all: its description
lists extracted fields and doesn't mention a file. It also returns a coworker's
receipts only when receipt search is enabled, and it is not enabled in the
sandbox. A transaction that names a receipt Ramp doesn't return still fails the
connection, because checking against a missing file would raise a false alarm.
The shape of `accounting_categories` and the order of `reporting_chain` are also
unconfirmed. The sandbox has two legal entities (Save Time Inc, Save Money Inc),
while the PRD assumes one. We don't filter by entity, and `entity_uuid` stays in
`raw`.

**Running it live from Python.** `RAMP_MCP_TOKEN` must be an OAuth access token
issued to our own client. Claude Code's token belongs to Claude Code's OAuth client,
and we can't reuse it. Getting our own token means registering a client (dynamic
registration at `/register`) and running the authorization-code + PKCE flow. Ramp
says a custom client's redirect URI must be allowlisted by Ramp. Until then the
live test (`AUDITORIUM_LIVE_RAMP=1`) stays skipped.

**What would change our mind.** A bill or a receipt in the sandbox: we would record
it and replace the synthetic fixture. An approval reachable from a transaction. The
business's time zone exposed by Ramp.
