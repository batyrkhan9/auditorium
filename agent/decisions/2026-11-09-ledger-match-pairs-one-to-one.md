# Ledger matching pairs every transaction with one entry, all at once

**Decided.** Before any check runs, all transactions are paired with ledger
entries in one pass, and each entry can belong to only one transaction. Pairs
are made strongest evidence first:

1. same vendor and same invoice number (if the amount differs: amount mismatch)
2. same vendor and same amount, closest dates first
3. same invoice number and amount under another vendor (vendor mismatch)
4. same vendor within 10 days, different amount (amount mismatch)
5. same amount within 3 days under another vendor name, only when neither side
   has any other candidate. This one is `needs_review`, never `fail`.

A transaction left with no entry is `ledger_match_missing`. An entry left with
no transaction that repeats a paired one is `ledger_match_duplicate`.

**Why.** Looking up each transaction on its own gets recurring bills wrong: two
monthly charges for the same amount can both claim the same entry, and a real
duplicate entry is invisible because nothing is ever "left over". Pairing
one-to-one fixes both. The period check uses the same pairing, so the two
checks cannot disagree about which entry is the transaction's.

**Why step 5 is only needs_review.** Card statements and QuickBooks often name
the same vendor differently ("AMZN Mktp US" and "Amazon"). Same amount and date
is suggestive but two unrelated purchases can share them. Calling it a failure
would be a false alarm; calling it a pass would be a guess.

**Considered.** Asking a model to match each transaction. It will be useful for
step 5 later, but one-to-one assignment is a global constraint that plain code
enforces exactly, and it needs no API key.

**What would change our mind.** If real QuickBooks entries carry the Ramp id in
a memo or reference field, match on that first. That needs the connectors to
show us a real payload.
