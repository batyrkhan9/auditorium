# Ramp: a file Ramp lists but cannot serve does not fail the connection

Decided 2026-11-10, board task #16.

**What happened.** On demo-api.ramp.com, 116 card transactions name a receipt.
Ramp serves 115 of the files. Receipt `89a22d6c-a4d1-41ce-bec3-7ba52d6f05fc`
(a $11.78 Jamba transaction) links to `/.DEMO_RECEIPTS/jamba_1.pdf` on
demo-shared.ramp.com. That host answers 404 `NoSuchKey`, with or without our
token, and also for a newly signed link. The file is not there. Our connector
refused the whole Ramp read over this one file and fell back to local data.

**What.** A file whose host answers 404 or 410 is recorded as unavailable. It is
taken off its transaction's `document_ids`, so the receipt check sees no receipt and
asks for one. `raw` still lists the receipt id, and `describe()` names the document
and the status. Any other failure still fails the connection: a 403 (an expired or
refused signature), a 5xx, a timeout, or a body that is not the document. If more
than 5% of files (at least two) are gone, the connection fails too, because that
looks like a broken host and not missing receipts.

**Considered.**
- Fail the connection, as before. One missing demo file turned 1,413 live card
  transactions into local data. That says less about Ramp than the truth does.
- Keep the id in `document_ids` with no file behind it. The contract requires every
  id to resolve to a document we hold, and the agent could not open it.
- Drop the transaction. Then the spend would be missing from the audit.

**Why.** The auditor cannot get this receipt either, so asking for it again is the
right follow-up. It is not a false alarm. The transaction still says a receipt was
attached (`raw.receipts`), and the run says which file was missing. The cap stops one
outage from making every transaction look like it has no receipt.

**Also.** A bill's `EMAIL` attachment is the raw e-mail the invoice came in, served
as `text/plain`. We keep it as a text document. Every other file must still be a PDF
or an image, and text that starts with `<` is refused, so a login page cannot get in
that way.

**What would change our mind.** A contract field for "listed but unavailable", which
would let the receipt check answer `needs_review` instead of asking for the receipt.
Or Ramp telling us that a 404 there means something other than a missing file.
