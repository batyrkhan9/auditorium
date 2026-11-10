# Scored runs take Ramp's records from the dataset, and there is no second dataset

Answer from the data area to board #18. Ramp's sandbox holds Ramp's own demo
company, Save Time Inc. Our dataset is Copperline, which we invented. Read both
live and nothing matches.

**Decided.**

1. **Scored runs.** Everything Ramp would hold (transactions, approvals,
   attachments, people) comes from `data/company`, read from the folder or
   through the stand-in server. QuickBooks is read live from the sandbox, which
   now holds the dataset's ledger. The answer key applies to these runs as it is.
2. **No second dataset built on Save Time's records.** Not now.
3. **If the dataset's bills are loaded into the Ramp sandbox** (#19, which needs
   the PRD change both of us must agree): the key still applies to a bill read
   live from Ramp for what the loader carries, which is vendor, amount, invoice
   number, invoice date and the attached files. It does not apply to `approval`,
   because the loader cannot carry who requested the bill, who approved it and
   when, or the day it was paid. A scored run must take those from the dataset
   record of the same bill, found through the stamp, or the eval must leave
   `approval` on live bills out. Otherwise every live bill reads as never
   approved.

**Why 1.** The key is written about Copperline's records and is keyed on the
dataset's transaction ids. The run says which system was live and which was the
dataset, which is PRD rule 6.

**Why not 2.** Save Time's records are Ramp's. We cannot change them, so we
cannot plant anything, and they come with no spend policy in our shape and no
job roles. A second dataset means a second ledger, a second QuickBooks company
and a second answer key, to demonstrate a connection that reading Save Time
read-only already demonstrates.

**Version 1 is frozen.** The QuickBooks sandbox holds it. Transaction, document
and ledger ids will not change. The rerun after contract v0.2 only adds fields.

**Considered.** Rebuilding Copperline around Save Time's transactions, which is
what the PRD's phrase "matched to the Ramp demo transactions" asked for. That
throws away version 1, its documents and the loaded ledger, and still cannot
plant anything on Ramp's side.

**What would change our mind.** Ramp's engineers asking to see the four checks
run on their own demo company. Or a way to create card transactions in the
sandbox, which would let the whole dataset live in Ramp.

**What happened next.** Both of us agreed the PRD change (pull request #20,
merged 2026-11-10). The connectors loaded 68 of the 70 bills into the Ramp
sandbox; the 2 unpaid ones stay in the dataset. For a bill read live,
requester, approvals and paid date come from the dataset through the stamp, so
the key applies to all four checks (board #19).
