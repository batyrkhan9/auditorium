# Ramp: load the dataset's bills into the sandbox, and serve them back live

Decided 2026-11-10, board task #19. Writing the dataset's bills (and the vendors they
need) to the sandbox, demo-api.ramp.com only, was agreed in issue #18.

**What.** `ramp_loader.py` creates the dataset's bills in Ramp through the Developer
API: `POST /developer/v1/vendors` for vendors missing by name, `POST /developer/v1/bills`,
then `POST /developer/v1/bills/{id}/attachments` (multipart: `file`, `attachment_type`
INVOICE or FILE) for each invoice file the dataset links. Each memo ends with
`auditorium-ref: <dataset transaction id>`, the stamp the QuickBooks loader uses.
`GET /bills` returns `memo`, so a second run skips stamped bills (and uploads only the
files they lack), and a reader maps a live bill back. `connect_sources` then serves
stamped bills live from Ramp and everything else Ramp-side from the dataset.

**How it keeps from writing by accident.** Dry run is the default. `--write` is
required, and the client then refuses any host but `https://demo-api.ramp.com`, checked
when it is built and again on every POST. A dry-run client asks Ramp for read scopes
only and refuses POST itself. Without credentials the dry run is offline and says so.

**Fields.** A bill is a direct bill, not a draft (drafts never appear in `GET /bills`).
Kept: vendor, invoice number, invoice date (`issued_at`), due date, amount (one line
item), memo, the files. `payment_method` `PAID_MANUALLY` records it as paid outside
Ramp, so nothing is scheduled; `enable_accounting_sync` is off. Not carried:
category (needs ERP accounting-field ids; the sandbox has no accounting connection) and
department, read back as Ramp has them; spender, approvals and paid date (we send
`payment_details.payment_date`; Ramp sets `paid_at`), read back from the dataset. Skipped: unpaid bills (any other payment method
needs vendor bank details we don't have; 2 of 70 in copperline-fy2025-v1), a bill with
two invoices, or a name two Ramp vendors share. Ramp requires a vendor contact email
and the dataset has none, so the loader uses `ap+<name>@example.com`, a reserved
domain. It never comes back into the contract.

**Vendor owner.** Ramp refuses a bill whose vendor has no owner (`BILL_PAY_7001`,
seen on the first real load), and only `POST /vendors` takes `vendor_owner_id`; PATCH
answers 200 and ignores it. New vendors are owned by the business's one active
`BUSINESS_OWNER` (in the sandbox the account holder, who also owns Save Time's own
vendors); the loader stops if there is not exactly one. An existing vendor without an
owner skips its bills with that reason. The 25 vendors the first load made without an
owner, with no bills, were deleted and recreated. Ramp then makes that user the bill
owner: Ramp bookkeeping, never the requester (see Reading back). Considered: an admin
(three, no rule to pick one), the app itself (not a Ramp user). `PAID_MANUALLY` also
needs `payment_details` (`manual_payment_method` `OTHER`, since the dataset doesn't say
how a bill was paid, and the paid date), or Ramp answers `PAYMENTS_7002`. Errors print
one line per distinct reason, without Ramp's per-request error id.

**Reading back.** A live bill keeps its Ramp id; `raw["memo"]` keeps the stamp, cut
from `memo`. Requester (`spender_id`), approvals and paid date come from the dataset
record the stamp names (Batyrkhan's condition for scored runs, #18): Ramp cannot hold
them. Ramp's values stay in `raw` (`bill_owner`, `paid_at`, and the mapped values under
`auditorium_replaced_from_dataset`), and `describe()` says so. Ramp records without our stamp (Save Time's) are left out. A dataset bill
not found in Ramp, and every card transaction, comes from the dataset. People are the
dataset's plus the Ramp users the live bills name. No stamped bill: unchanged, Ramp
holds another company and the dataset is used.

**Contract question for #7.** v0.1 has one `SourceUse` per system, `live` or `local`.
We report Ramp `live`, because the run did read Ramp, and put the split in `detail`:
"bills: 68 of 70 from Ramp (stamped), 2 from dataset ...; card transactions: 130 from
dataset ... (Ramp cannot create card transactions)". A per-record origin, or a
`mixed` mode, would say it in a field instead of prose.

**Considered.** Draft bills plus submit: same fields, two calls, and a submit that
fails leaves a draft we never read. The stamp in `remote_id`: needs an accounting
connection. In `vendor_memo`: that text is meant for the vendor. Serving only the
live bills and dropping the rest: the eval would lose the bills Ramp cannot hold.

**What would change our mind.** A first real load where `memo` loses the stamp, the
multipart upload is refused, or `paid_at` lands far from the dataset's paid date.
A sandbox with an accounting connection, which would let category through.
A contract field for values taken from the dataset. v0.2 deferred it, so
`raw["auditorium_replaced_from_dataset"]` stays, and `describe()` now names it.
