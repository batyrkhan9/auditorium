# The shared data format (v0.1.0)

The models are code: `contracts/src/auditorium_contracts/`. This page is the
map. JSON Schemas for non-Python code are in `contracts/schema/`.

## The four seams

| From → to | What crosses | Model / file |
|---|---|---|
| data → everyone | the public dataset folder | `load_dataset`, `validate_dataset` |
| connectors → agent | read access to source records | `Sources` (live) / `LocalSources` (folder) |
| agent → evals, ui | the run folder | `load_run`, `validate_run` |
| data → evals | the answer key (sealed) | `ExpectedResult` |
| evals → everyone | aggregate scores | `Scorecard` |

## Dataset folder

```
manifest.json        DatasetManifest (fiscal year, coverage_end)
transactions.jsonl   SpendTransaction   card expenses and vendor bills, as Ramp holds them
ledger.jsonl         LedgerEntry        QuickBooks entries
documents.jsonl      Document           index of files, with sha256
people.jsonl         Person             for approver roles and managers
policy.json          SpendPolicy        receipt threshold and approval rules
documents/...        the files
samples/<id>.json    SampleRequest      what the auditor picked (optional)
```

Records carry only what the source systems really provide. There is no "service
period" field and no link from a ledger entry to its transaction. The agent
works those out from the documents; that is the product.

## Run folder

```
run.json        RunManifest    includes sources: live or local, per system
results.jsonl   CheckResult    one per transaction and check
packets.jsonl   EvidencePacket sample mode
drafts.jsonl    FollowUpDraft  never sent
log.jsonl       LogEvent       hash-chained; written only through LogWriter
scorecard.json  Scorecard      added by the eval
```

## Evidence, the rule everything hangs on

Every `CheckResult` carries at least one piece of evidence, of three kinds:

- `document`: document id, page, and a quote copied from that page
- `record`: a field of a source record and the value read
- `search`: a query and the ids it returned (how absence is shown)

`verify_evidence` checks each one against the sources. The agent calls it before
recording a result and the eval calls it on every result. Quotes are compared
through `text.py` (same extraction, same normalization) so both sides read the
same words. Documents need a text layer.

## Conventions

- Money is integer US cents. Periods are calendar months, `YYYY-MM`.
- Result id is `<transaction_id>#<check>`.
- Statuses: `pass`, `fail`, `needs_review`, `not_applicable`. A fail carries an
  `IssueCode`. The eval scores on transaction, check and status.

## Changing it

`shared/` branch, a task labelled `contract-change`, both humans agree. Then
`uv run python -m auditorium_contracts export-schema` and `uv run pytest`.

## Not decided yet

- Field names have not been checked against real Ramp or QuickBooks payloads.
  Connectors will find mismatches; raise them as contract changes.
- Parsed PBC-list requests have no model yet.
- Receipts that are photos (no text layer) cannot be quoted. Out of v0.
- How `needs_review` is scored is the eval's call.
