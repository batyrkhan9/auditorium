# Ramp: load the dataset's bills into the sandbox, and serve them back live

Decided 2026-11-10, board task #19. Writing to the sandbox waits on the PRD change in
`shared/ramp-sandbox-writes` (draft decision "The Ramp sandbox holds our dataset's
bills"). Until both of us agree it, nobody runs `--write`.

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
Ramp, so nothing is scheduled; `enable_accounting_sync` is off. Not carried, and read
back as Ramp has them: category (needs ERP accounting-field ids; the sandbox has no
accounting connection), department, spender, approvals, and paid date (we send
`payment_date`; Ramp sets `paid_at`). Skipped: unpaid bills (any other payment method
needs vendor bank details we don't have; 2 of 70 in copperline-fy2025-v1), a bill with
two invoices, or a name two Ramp vendors share. Ramp requires a vendor contact email
and the dataset has none, so the loader uses `ap+<name>@example.com`, a reserved
domain. It never comes back into the contract.

**Reading back.** A live bill keeps its Ramp id; `raw["memo"]` keeps the stamp, cut
from `memo`. Ramp records without our stamp (Save Time's) are left out. A dataset bill
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
