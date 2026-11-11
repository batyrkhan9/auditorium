# A refund is not a purchase, and a tipped receipt under a coded name is not a mismatch

**How these were found.** The public scorecard said the agent raised six false
alarms: five on receipts and one on approvals. It does not say which. So the
agent's own failures on the public dataset were read one by one against the
documents and records, looking for reasoning that is wrong in general. The
answer key was not seen, and nothing here is specific to a transaction.

**Decided.**

1. **Refunds.** A transaction with a negative amount is money coming back. The
   receipt check and the approval check return `not_applicable` for it. Ledger
   match and period still apply: the books must record the refund, in the right
   year.
2. **Tipped receipts under a coded name.** The receipt check now ties a receipt
   to its charge through the name the books record the charge under, whether or
   not the receipt shows the charged amount. So a receipt from "Harborview
   Kitchen" for a charge the statement calls "TST* HARBORVIEW KITCHEN" is
   recognised as the right restaurant, and a different amount falls under the
   existing rule for card charges: `needs_review`, because a tip or tax can
   explain it.

**Why refunds were wrong before.** The thresholds were compared with the size of
the amount, ignoring its sign. A $356 refund from an airline was reported as a
$356 charge with no receipt and no approval. Nobody approves a refund, and the
company's receipt rule is about what it spends.

**Why the second was wrong before.** The receipt named the restaurant in full,
the statement used a payment processor's code, and the amounts differed by the
tip. With neither the name nor the amount matching, the agent concluded the
receipt was for something else. The books already had the restaurant's proper
name against that charge.

**Effect on the scored dataset** (the agent's own output; not scored yet):
receipt failures 14 to 9, approval failures 13 to 12. Two of the receipt results
moved to `needs_review`, three to `not_applicable`, and the approval result to
`not_applicable`.

**Considered.** Requiring a credit memo for a refund. An auditor may ask for
one, but that is a different rule from the company's receipt policy, and the
policy data has no such rule.

**What would change our mind.** A policy that states documents are required for
refunds, or scoring that shows a refund the key expects to fail.
