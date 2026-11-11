# Results show one row per transaction, its four checks side by side

**Decided:**

- The results page lists transactions, not results. Each row has the vendor,
  amount and date, then one cell per check (receipt, approval, ledger_match,
  period), then the finding of its worst exception. A real run is 200 rows
  instead of 800, and an auditor reads a transaction as one thing anyway.
- Status is told by colour and by shape: fail is a solid red cell with a cross,
  needs_review an amber dashed cell with a question mark, pass a quiet tick,
  not_applicable a faint dash. Fail and needs_review stay apart even in
  greyscale, because a false alarm counts against us.
- Rows sort exceptions first, then by amount. Search, status, check and sort
  run in the browser over rows the server has already rendered; the same
  query string filters on the server, so links and the page without
  JavaScript give the same result. Filtered-out rows are rendered `hidden`.
- The result page steps through the exceptions in that same order (Previous,
  Next, `j`/`k`), lists the transaction's other checks, and keeps sign-off in
  a column beside the evidence.
- Fonts are IBM Plex Sans and Mono, pinned on jsDelivr. Without the network
  the system fonts take over and nothing breaks. Dark mode follows the system.

**Considered:**

- One row per result, as before. Faithful to `results.jsonl`, but 800 rows of
  mostly "pass" bury the 88 that matter, and the four checks of one bill end
  up on four rows.
- Server-only filtering (each click reloads). Simple, but a real run's page is
  about 700 KB of HTML and takes a few hundred milliseconds to render, too slow
  for a live demo. Rendering everything once and filtering in the browser is
  instant at this size.
- A front-end framework or a virtualised table. Not needed for 200 rows, and
  it would bring the build step `2026-11-09-ui-stack.md` chose to avoid.

**What would change our mind:** runs of many thousands of transactions (then
paginate on the server), a check that is not one of the four (the grid's
columns come from `CheckName`, so a new check adds a column, but many more
would not fit), or auditors asking to work result by result rather than
transaction by transaction.
