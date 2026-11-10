# The approval check follows the company's policy rules, and judges who and when

**Decided.** For each transaction:

1. Which policy rules apply? A rule applies when it covers this kind of spending
   and the amount is at or above its threshold. If none applies: `not_applicable`.
2. Every rule that applies must be met. For each one:
   - no approval on record: `fail`, `approval_missing`
   - approved, but not by the person the rule names: `fail`, `approval_wrong_approver`
   - approved by the spender themself: `fail`, `approval_wrong_approver`, even if
     the spender holds the required role
   - a vendor bill approved after it was paid: `fail`, `approval_after_the_fact`
   - the records do not say who the required approver is (no manager on file,
     nobody holds the role): `needs_review`

"Manager" means the spender's own manager from the people records. Any other
approver is matched on role.

**Why the rules come from the policy.** "Was it approved, if the rules required
approval" is a question about the company's rules, not ours. Hard-coded
thresholds would be wrong for every company but one.

**Why late approval is only judged on bills.** A bill is approved and then
paid, so approval after payment means the control did not work. A card charge
happens at the till and is reviewed afterwards by design, so approval after the
purchase is normal and not flagged.

**Why self-approval fails.** The point of approval is a second person. A CFO
approving their own bill satisfies the letter of "approved by the CFO" and not
the control.

**Considered.** When several rules apply, using only the highest tier. Fewer
failures, but the contract says a rule "applies at or above this amount" with
no notion of tiers replacing each other, so every applicable rule is tested.

**What would change our mind.** If the dataset's policy is written as tiers
where the higher one replaces the lower, test only the highest. That is a
question for whoever writes the policy data, and it should be settled in the
contract rather than guessed here.
