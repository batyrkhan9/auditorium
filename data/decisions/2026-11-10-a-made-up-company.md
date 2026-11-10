# The scored dataset is one made-up company, 200 transactions

**Decided.** The dataset in `data/company/` is Copperline, Inc.: a Series B
company with 42 employees that sells route-planning software to trucking
fleets. Fiscal year is calendar 2025. Records run to 28 February 2026. It holds
130 card charges and 70 vendor bills, 12 people, one spend policy, the ledger,
about 200 PDFs and one auditor sample request.

**Everything is invented.** People, vendors, merchants, the bank and the
auditor do not exist. Emails use `copperline.example`. The manifest is marked
synthetic.

**Why this size.** Small enough to read end to end and to run the agent on in
minutes. Large enough that most transactions are ordinary, so false alarms can
be counted against a real base.

**Why records run into 2026.** Whether a cost landed in the right year can only
be tested if the weeks after the year end are there to look at.

**Why the policy numbers are what they are.** Receipts from $50, card approval
from $250, bill approval from $1,000 and again from $10,000. They differ on
purpose from the numbers in the agent's own development sample, so a threshold
written into code instead of read from the policy would show.

**Considered.** Real vendor names (Delta, AWS). More realistic at a glance, but
this repo is public and we would be publishing invented invoices under real
companies' names.

**What would change our mind.** A Ramp sandbox login. Then the company should
be rebuilt around the transactions Ramp's demo account already holds.
