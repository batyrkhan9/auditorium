# An accrual is matched on its journal line, not only on the entry's total

**Decided.** When the period check looks for a year-end accrual that already
booked a bill, it compares the bill's amount with each debit line of the journal
entry, and with the entry's total only as a fallback. The evidence cites the
line that matched.

**Why.** Two reasons, one practical and one about accounting.

- A board task from the data side (connectors, #36) suggests journal entries
  read live from QuickBooks come back with a total of zero while their lines
  are right. Comparing totals, no accrual would ever match its bill in a live
  run, and every accrued bill would drop to `needs_review`. The pitch example's
  counterpart, the bill that was accrued properly, depends on this.
- A real year-end journal entry often accrues several vendors at once. Its
  total is the sum; one bill's amount is one line.

**Effect.** On the dataset folder: no result changes, 800 of 800 the same. The
difference shows only when totals are wrong or entries are bundled.

**Not checked.** A live QuickBooks read. This machine has no credentials, so the
zero-total case is covered by a test built from the description in #36, not by a
real payload.

**What would change our mind.** If the connectors fix the total at the source,
this stays as it is: the line is still the more exact place to look.
