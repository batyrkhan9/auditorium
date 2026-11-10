# The agent's sample documents are laid-out PDFs, and wrapped lines are read together

**Decided.** Every document in the agent's sample is now a real PDF with a
layout like the real thing: a heading, amounts right-aligned in their own
column, bank statement rows as a table, long lines wrapped. The service-date
reader follows a date range onto the next line when a line stops mid-range.

**Why.** Text files hid the one thing PDFs do to us: a long line wraps, and
"Service period: December 1 -" ends up on a different line from "December 31,
2025". On the first wrapped invoice the reader found no dates and a clean
transaction dropped to `needs_review`. Tables and right-aligned amounts turned
out fine: each row comes out as one line.

**How the quote still verifies.** The quoted evidence is the two lines joined
with a space. The contract compares quotes with whitespace collapsed, so a
quote that spans a line break matches the page.

**Considered.** Keeping text files for speed and trusting that PDFs behave the
same. They did not, and the scored dataset and the demo will use PDFs.

**Limits.** These PDFs are generated with a text layer and simple layouts. A
scanned receipt, a two-column invoice, or a table whose cells come out on
separate lines has not been tried.

**What would change our mind.** If the scored dataset's PDFs come out of
extraction with rows split across lines, row reconstruction belongs in the
contract's text extraction, where the eval reads the same text, not here.
