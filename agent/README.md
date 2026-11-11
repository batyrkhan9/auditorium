# The audit agent

Checks every card expense and vendor bill four ways, and assembles the evidence
when an auditor picks transactions to test. Product brief: [docs/PRD.md](../docs/PRD.md).

## Run it

```bash
# Pre-flight: four checks on every transaction in a dataset folder
uv run python -m auditorium_agent preflight data/company --out runs/demo

# Sample request: evidence packets and follow-up drafts for the auditor's picks
uv run python -m auditorium_agent sample data/company \
  --request data/company/samples/sample-2026-02.json --out runs/demo-sample
```

Options, on both commands:

| Option | What it does |
|---|---|
| `--live` | Read Ramp and QuickBooks through the connectors where credentials are set; the dataset folder supplies the rest. Needs `uv run --env-file .env`. |
| `--claude` | Ask Claude where plain code cannot settle a result. Off by default. Needs `ANTHROPIC_API_KEY`. |
| `--max-claude-calls N` | Stop asking after N requests in a run. Default 50. |

A run writes a folder: `run.json`, `results.jsonl`, `log.jsonl`, a `sources/`
copy of what was fetched, and in sample mode `packets.jsonl` and `drafts.jsonl`.
Check any run folder with `uv run python -m auditorium_contracts validate-run <folder>`.

## How it works

1. **Open the run** (`runs.py`). Log `run_started`, log a fingerprint of what the
   sources returned, save a copy of it. All before any check.
2. **Pair transactions with ledger entries** (`matching.py`), all at once and
   one-to-one. Where the books and the card statement name a vendor differently,
   the attached receipt settles it if it can.
3. **Run the four checks** (`checks/`). Each returns one result with evidence:
   a document page and a quoted line, a record field and its value, or a search
   and what it returned.
4. **Verify the evidence** (`preflight.py`). Every piece is re-checked against
   the sources before the result is recorded. Evidence that does not resolve is
   dropped, and a result with none left is not recorded.

The checks are plain code. Claude (`reader.py`) is asked only for what code
cannot settle: service dates written as a sentence, and two vendor names with no
document to tie them together. An answer about a document must quote a line,
and is thrown away if the line is not on the page.

## Where things are

| Path | What |
|---|---|
| `src/auditorium_agent/checks/` | `receipt`, `approval`, `ledger_match`, `period` |
| `src/auditorium_agent/matching.py` | pairing transactions with ledger entries; accruals |
| `src/auditorium_agent/books.py` | everything a check may look at, with each look logged |
| `src/auditorium_agent/sample.py`, `resolve.py`, `drafts.py` | sample-request mode |
| `src/auditorium_agent/reader.py` | the Claude reader and its call limit |
| `src/auditorium_agent/service_period.py`, `receipt_reader.py` | reading documents with plain code |
| `examples/sample/` | a small hand-written dataset for tests; not the scored one |
| `runs/` | one pre-flight run and one sample run on the scored dataset |
| `decisions/` | why each rule is what it is; `scripts/decisions agent` lists them |

## Score

On the scored dataset (40 planted mistakes among 800 checks): 37 caught with the
Claude reader, 35 without, 1 missed, no false alarms, and every piece of
evidence verified. The scorecards are in `evals/scorecards/`; the walkthrough
and the caveats are in [docs/DEMO.md](../docs/DEMO.md).

The agent was built without seeing the answer key. See [docs/SEALED.md](../docs/SEALED.md).

## Tests

```bash
uv run pytest agent
```

They run offline: a fake stands in for Claude. Rebuilding the test sample needs
one extra package: `uv sync --all-packages --all-groups`, then
`uv run python agent/examples/make_sample.py`.
