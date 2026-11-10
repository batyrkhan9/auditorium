# The period check judges by fiscal year, and fails only on a clear cut-off error

**Decided.** The period check fails a transaction only when all three hold:
the cost was incurred in one fiscal year, the books first record it in a later
one, and no journal entry in the earlier year names the vendor (no accrual).
Everything less clear is `needs_review`, not `fail`:

- the work ran across the year end
- it was recorded before the work was done (possible prepayment)
- an accrual for the vendor exists but for a different amount
- it was recorded just after year end and the invoice does not say when the work was done

A cost recorded in a different month of the same fiscal year passes.

**Why.** The auditor's year-end question is cut-off: did this year's costs land
in this year's books. A November cost booked in December does not change the
annual numbers, and flagging it would be a false alarm, which the PRD counts
against us. The accrual search is what "nobody booked it in December another
way" means in practice.

**Considered.** Judging by calendar month. Stricter, and it matches the word
"period" literally, but it flags timing differences no auditor of annual
statements would raise.

**Also decided here.**

- When the work was done is read from the invoice by plain code for now
  (`service_period.py`): a labelled line with dates. A model-based reader
  replaces it behind the same return type. Plain code first so the pitch example
  works with no API key and the rules are testable.
- Before a result is recorded, each piece of evidence is run through the
  contract's `verify_evidence`. Evidence that does not resolve is dropped and
  the result becomes `needs_review`. With no evidence left, no result is
  recorded.

**What would change our mind.** If the answer key treats same-year month slips
as failures, switch the comparison to months. It is one function, `Books.side`.
