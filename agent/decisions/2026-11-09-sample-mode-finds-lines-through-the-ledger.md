# Sample mode: how a request line becomes a packet, and what gets a draft

**Decided.**

1. **Finding the transaction.** An auditor's line is compared with both the Ramp
   transaction and the ledger entry paired with it. Every detail the auditor
   gave (vendor, amount, date, reference) must agree with one side or the
   other. Exactly one transaction must fit. If several fit, or none, the packet
   says so and no transaction is chosen.
2. **The packet** is built from the four checks' own evidence, sorted by what
   an auditor asks to see: receipt or invoice, approval, the policy rule, the
   ledger entry. For a paid bill it adds the bank statement line showing the
   payment.
3. **Missing** lists what a failed check means is absent: the receipt, the
   right receipt, the approval, the ledger entry, the bank payment.
4. **Drafts** are written only for what a coworker can supply: a receipt or
   invoice (to the spender) and evidence of an approval (to the person the
   rule names). Slack for a card receipt or an approval, email for a bill's
   invoice. Nothing is ever sent.

**Why compare with the ledger too.** Auditors pick their sample from the
general ledger and copy what it says. When the books are wrong, say $1,580
recorded for a $1,850 bill, the auditor's line carries the wrong amount, and
that is exactly the transaction they most need found.

**Why never guess between several matches.** Handing an auditor the evidence
for the wrong month's bill is worse than asking which one they meant.

**Why the approval draft asks for evidence, not for approval.** Asking the CFO
to approve now would manufacture the very thing our own check flags as
approval after the fact. The honest question is whether they approved it
somewhere Ramp does not show.

**Why no draft for a missing ledger entry or bank payment.** Those are the
finance team's own records. There is nobody else to ask; they go in `missing`
for the person running the agent.

**Considered.** Having a model write each message. The words would be nicer,
but what is asked, of whom, and through which channel is policy, and that
should not vary run to run. A model can polish the wording later.

**What would change our mind.** Real auditor requests arriving as free text
with no structured fields. Then a model has to read the line first, and this
matcher works on what it extracts.
