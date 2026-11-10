# The ledger is shaped so the QuickBooks loader takes every entry

**Decided.** Every ledger entry carries its lines, and debits equal credits.
A bill or an expense has exactly one balancing line equal to its total:
`Accounts Payable (A/P)` for a bill, `Ramp Card` for a card expense. A refund
is an expense with a negative total and the sides flipped. Board task #7,
items 8, 11 and 13.

**Contract v0.2 fields.** `account_type` on each line, `payment_type` on each
expense, the vendor on journal lines and `synthetic` on the manifest are held
by the generator and written as soon as the installed contract has those
fields. Regenerating after v0.2 merges fills them in; nothing else changes.
Checked against the `shared/contract-v0.2` branch.

**Until then** the loader guesses account types from names, so account names
were chosen to guess right: the card account is called `Ramp Card`, and no
expense account contains "card", "bank", "cash" or "payable". The connectors'
planner accepts all 255 entries of the v0.1 output and skips none.

**What is in the ledger.** Bills and expenses for the vendor bills and card
charges, plus journal entries: year-end accruals and their reversals, monthly
prepaid amortization, payroll and depreciation.

**What is not.** Bill payments. The loader does not load them and no check
reads them. Payments appear on the bank statements.

**What would change our mind.** A check that needs to see the payment in the
books, or the loader learning to load bill payments.
