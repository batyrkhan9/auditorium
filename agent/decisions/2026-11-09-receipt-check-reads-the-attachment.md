# The receipt check reads the attachment instead of trusting that one exists

**Decided.** The check has three steps.

1. Was one required? A vendor bill always needs its invoice. A card charge
   needs a receipt at or above the policy threshold; below it the result is
   `not_applicable`.
2. Is one attached? If not: `fail`, `receipt_missing`.
3. Is it the right one? The document is read and compared with the transaction:

| The document... | Card charge | Vendor bill |
|---|---|---|
| names the vendor (or the invoice number) and shows the amount | pass | pass |
| shows the amount but not the vendor | needs review | needs review |
| names the vendor, different amount | needs review | fail, `receipt_mismatch` |
| neither | fail, `receipt_mismatch` | fail, `receipt_mismatch` |
| has no readable text | needs review | needs review |

With several attachments, the best one counts.

**Why read it.** A file clipped to the wrong transaction satisfies "has a
receipt" in the card platform and fails when the auditor opens it. Finding that
first is the point of the product.

**Why a card charge with a different amount is only needs review.** Tips, tax
and currency conversion make an honest restaurant or travel charge differ from
its receipt. Failing those would be a false alarm. A bill is different: it is
paid for the amount on the invoice, so a different amount means the wrong
invoice.

**Why amount without vendor is needs review.** Card statements name merchants
differently from their receipts ("SQ *BLUEBTL" and "Blue Bottle Coffee"). Plain
code cannot tell that apart from a wrong receipt that happens to share the
amount. This is the first place a model call will earn its keep.

**Considered.** Treating "attached" as a pass. Simpler, matches what Ramp shows,
and misses the case auditors actually catch.

**What would change our mind.** If the real data has many correct receipts
landing in needs review, add the model-based reader before loosening the rule.
