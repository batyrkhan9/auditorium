# The viewer on contract 0.2: name what evidence was checked against, pin what was signed

**Decided:**

- Evidence is checked against one basis, and the screen names it. First the
  run's snapshot (`sources/`), if it matches the fingerprints the run logged.
  If not, a local dataset, but only when every source was local. Otherwise
  nothing. Labels are "Verified against snapshot", "Verified against dataset",
  "Unchecked: live run" or "Not checked", never just "Verified".
- A snapshot that does not match the logged fingerprints is not used, and a
  banner lists what differs. A live run with a good snapshot is checked against
  it. A live run without one stays unchecked, as before.
- The dataset comes from `--dataset`, or else from run.json's `dataset_path`.
  Either is used only when its `dataset_id` matches the run's. When it isn't
  used, the screen says why.
- A per-result sign-off pins the `result_hash` taken when the page rendered,
  and passes `result=` too, so a result that changed while the reviewer read it
  is refused. The run sign-off form carries the hashes of `results.jsonl` and
  `run.json` as shown, and the screen refuses if either changed before calling
  `sign_off_run`.
- Sign-off problems show as a banner on every page: a result changed after its
  latest sign-off, a run signed off before it finished, and results or run.json
  changed after the run sign-off. The step log page runs `validate_run` on the
  whole folder; a test checks that both report the same problems.
- A log line that is not an event no longer crashes the page. The page shows the
  events before it, says how to repair, and refuses sign-off. A `LogDamaged`
  from the writer is shown as the refusal message.
- Sign-off is still refused until the run has finished. The contract's lock
  makes it safe to write, but a reviewer should sign a finished run.
- "DEMO RUN" (results made up by the demo builder, `demo-` ids) and
  "Synthetic data" (`SourceUse.synthetic`) are separate banners. A real agent
  run on our dataset shows only the second.

**Considered:**

- Calling `validate_run` on every page. It re-reads every document, and it
  stops before checking sign-offs when there is neither a snapshot nor sources.
  So the screen works out sign-off states itself with the contract's
  `result_hash` and `sha256_file`, using the same rules (only the latest
  sign-off counts), and runs `validate_run` only on the step log page.
- Preferring the dataset over the snapshot when both exist. The snapshot is
  what the run actually read; the dataset may have moved on since.

**Contract friction left (for a later `shared/` change, not blocking):**

- `validate_run` gives up before the sign-off checks without sources or a
  snapshot.
- The check that ties a snapshot to the logged fingerprints is private
  (`_snapshot_problems`). The screen repeats it, about ten lines, using the
  public `fingerprint_sources`.
- `log_source_fingerprints` takes no `ts`, so the demo writes the same events
  itself to keep its clock. A test checks they match.
- `sign_off_run` has no "as shown" hash like `sign_off`. The screen compares
  before calling it, which leaves a short race between the check and the write.
- `snapshot_sources` does not copy `samples/`, so sample lines on the packets
  page still need the dataset.
- There is no public way to ask whether the log can be appended to without
  appending. The screen catches `LogDamaged`.

**What would change our mind:** if the contract adds these (a public snapshot
check, `results_sha256=` on `sign_off_run`, sign-off checks that run without
sources), the screen should call them and drop its own copies.
