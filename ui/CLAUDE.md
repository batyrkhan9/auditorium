# Area: ui

Owner: Noriaki. Branches: `ui/<topic>`, in the build clone.

The screen: results, the evidence behind each one, the step log, reviewer sign-off.

- Read run folders with the contract's models, as `auditorium_contracts.load_run` does.
  The viewer's `_load_run` also skips a half-written last log line while the agent
  is still writing. JSON Schemas for other
  languages are in `contracts/schema/`.
- Record sign-offs with `LogWriter.sign_off(..., result=, result_sha256=)` (pinned to
  the result as shown) and `LogWriter.sign_off_run`, so the hash chain stays intact.
- Show whether each source was live or local, and synthetic (`RunManifest.sources`).

Run it:

```
uv run python -m auditorium_ui <run_dir> --dataset <dataset_dir> [--port 8000]
uv run python -m auditorium_ui --demo [preflight|sample]   # synthetic data, labelled DEMO
```

Evidence is checked against the run's snapshot (`sources/`) when it matches the
logged fingerprints, else against a local dataset (`--dataset`, or run.json's
`dataset_path` when its `dataset_id` matches) if every source was local. The label
always names which. A live run without a snapshot shows "Unchecked: live run".
Demo runs have `demo-` run and dataset ids and show DEMO RUN; synthetic data shows
its own banner. Sign-off is disabled until the run has finished.
See `decisions/2026-11-10-viewer-on-contract-v0.2.md`.

Record decisions in `ui/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
