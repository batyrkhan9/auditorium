# The dataset is generated, and the same command always gives the same bytes

**Decided.** `data/company/` is written by a seeded generator. Running it again
gives byte-identical files, PDFs included. Nobody edits the dataset by hand.
The generator is in `sealed/datagen/` and runs only in the sealed clone:

    uv run --directory sealed/datagen python -m datagen

**How the bytes stay the same.** One fixed seed. PDFs are written without
compression and with one fixed creation date, so they do not depend on the
machine or the day. The PDF library version is pinned.

**Ids and file names carry no meaning.** Transaction, person and document ids
are random, in the same `ramp:<kind>:<id>` scheme the Ramp connector produces,
so they survive the stand-in server unchanged. Ledger ids are random numbers in
QuickBooks' `qbo:<Entity>:<Id>` scheme. Files are named by their random id.
Records are sorted by date and then id.

**Documents are PDFs with a text layer**, laid out like the real thing. A few
invoice and receipt layouts exist, and the layout follows who issued the
document.

**Considered.** Writing the records by hand. Fine for 20, not for 200 with
matching PDFs, and a hand-made set cannot be regenerated when the contract
changes.

**What would change our mind.** Needing scanned receipts with no text layer.
Those cannot be byte-stable the same way and the contract cannot quote them yet.
