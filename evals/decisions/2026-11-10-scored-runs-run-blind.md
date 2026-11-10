# Scored runs: the agent runs blind, and the eval snapshots what it read

**Decided.**

- `python -m auditorium_evals run` copies the public dataset to a temp folder
  outside the repo and starts the agent in a child process whose working
  directory is that folder. It refuses a dataset path inside `sealed/`.
- Sources: Ramp's side from the dataset, QuickBooks live, per
  data/decisions/2026-11-10-scored-runs-read-ramp-from-the-dataset.md. The
  harness clears the Ramp variables for the child and calls
  `auditorium_connectors.connect_sources`, so QuickBooks falls back to the
  dataset only if it cannot be reached, and `run.json` says which. `--ramp live`
  and `--qbo dataset` exist for other runs. Live ids are mapped back to dataset
  ids through the loaders' `auditorium-ref:` stamp.
- The harness calls the agent's `run_preflight` with that `Sources`, because the
  agent's own command only takes a dataset folder. Right after the run it saves
  the same `Sources` instance with `snapshot_sources` and runs `validate_run`
  against it. Scoring verifies evidence against the snapshot.
- Outputs: the aggregate `Scorecard` and a metrics file in `evals/scorecards/`;
  the run folder and everything per transaction in `sealed/eval/`. The
  snapshot (`sources/`) stays out of git.
- Braintrust is used only when `BRAINTRUST_API_KEY` is set and the package is
  installed. Otherwise the records it would get are written to
  `sealed/eval/scores/<run_id>/braintrust_records.jsonl` and the scorecard
  says Braintrust was not used.

**Considered.** Running the agent's command line unchanged: it reads the dataset
folder only, so QuickBooks would never be live. Waiting for the agent to take
live sources: blocks the eval.

**Known gap.** The agent does not yet log source fingerprints, so the snapshot
cannot be tied to the fetch through the hash-chained log; `validate_run` reports
that, and the scorecard counts it. Asked of the agent area on the board.

**What would change our mind.** The agent's command taking a `Sources` choice
and writing its own snapshot. Then the harness calls that command and drops its
own snapshot step.
