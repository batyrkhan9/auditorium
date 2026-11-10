# Area: ui

Owner: Noriaki. Branches: `ui/<topic>`, in the build clone.

The screen: results, the evidence behind each one, the step log, reviewer sign-off.

- Read run folders through `auditorium_contracts.load_run`. JSON Schemas for other
  languages are in `contracts/schema/`.
- Record sign-offs with `LogWriter.sign_off` so the log's hash chain stays intact.
- Show whether each source was live or local (`RunManifest.sources`).

Run it:

```
uv run python -m auditorium_ui <run_dir> --dataset <dataset_dir> [--port 8000]
uv run python -m auditorium_ui --demo [preflight|sample]   # synthetic data, labelled DEMO
```

Without `--dataset`, evidence shows as "not checked" and documents cannot be opened.

Record decisions in `ui/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
