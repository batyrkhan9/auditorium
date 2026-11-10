# In Copperline's policy the approval rules add up

**Decided.** A vendor bill of $10,000 or more needs two approvals: the
requester's manager (the $1,000 rule) and the CFO (the $10,000 rule). The
higher rule does not replace the lower one. When the requester's manager is the
CFO, one approval covers both. The written policy PDF says this in words.

**Why.** The contract's `ApprovalRule` has a minimum amount and no maximum. A
rule "applies at or above this amount", so both rules apply to a large bill.
Writing the policy any other way would need a contract change. It is also how
approval chains work in card platforms: steps are added, not swapped.

**Considered.** Tiers that replace each other (manager up to $10,000, CFO
above). Common in real policies, but the contract cannot say "up to".

**Also decided.** Nobody approves their own spending, and the CEO has no spend
that needs approval, because the contract has no way to say who approves the
person at the top.

**What would change our mind.** A contract change that lets a rule carry an
upper bound or name a fallback approver.
