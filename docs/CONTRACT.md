# The shared data format (v0.2.0)

The models are code: `contracts/src/auditorium_contracts/`. This page is the
map. JSON Schemas for non-Python code are in `contracts/schema/`.

## The seams

| From → to | What crosses | Model / file |
|---|---|---|
| data → everyone | the public dataset folder | `load_dataset`, `validate_dataset` |
| connectors → agent | read access to source records | `Sources` (live) / `LocalSources` (folder) |
| agent → evals, ui | the run folder | `load_run`, `validate_run` |
| data → evals | the answer key (sealed) | `ExpectedResult` |
| evals → everyone | aggregate scores | `Scorecard` |

## Dataset folder

```
manifest.json        DatasetManifest (fiscal year, coverage_end, synthetic)
transactions.jsonl   SpendTransaction   card expenses and vendor bills, as Ramp holds them
ledger.jsonl         LedgerEntry        QuickBooks entries
documents.jsonl      Document           index of files, with sha256
people.jsonl         Person             job roles and managers
policy.json          SpendPolicy        receipt threshold and approval rules
documents/...        the files
samples/<id>.json    SampleRequest      what the auditor picked (optional)
```

Records carry only what the source systems really provide. There is no "service
period" field and no link from a ledger entry to its transaction. The agent
works those out from the documents; that is the product.

## Where each record comes from when the sources are live

Ramp and QuickBooks do not hold everything the checks need. A live `Sources`
combines them with a small folder of company records, and says so in
`describe()`.

| Record | Live source |
|---|---|
| `SpendTransaction`, its approvals, attached `Document`s | Ramp |
| `LedgerEntry` | QuickBooks |
| `Person.role`, `Person.manager_id` | company records. Ramp's own "role" is a permission level and goes in `Person.platform_role` |
| `SpendPolicy` | company records. Ramp has no policy in this shape |
| bank statements, the PBC list | company records (files) |

Other rules for live sources:

- `Document.sha256` is computed by the connector after it downloads the file.
  `Sources.document_file()` is the way to reach the file; `Document.path` is
  where the connector saved it.
- `Document.origin`: use `default_origin(type)` when the source does not say.
- Approvals are the steps of the approval chain. A step is `approved`,
  `rejected` or `pending`. No steps at all means it was never routed.
- `SpendTransaction.paid_date`: for a bill, the day payment left; for a card
  charge, the settlement date.
- **A run is verified against the `Sources` it ran on.** Ledger ids in a
  QuickBooks sandbox differ from the ids in the dataset folder that was loaded
  into it, so evidence from a live run only resolves against the live sources.

## Run folder

```
run.json        RunManifest    sources (live or local, per system), dataset_path, synthetic_data
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

## The log

- Written only through `LogWriter`. Any number of writers may share one log:
  each append takes an exclusive file lock and reads the last line under it.
- Sign-off on one result: `sign_off(reviewer, result_id, decision, comment, result=result)`.
  Passing the result stores its hash, and `validate_run` then reports any
  result changed after it was signed.
- Sign-off on the whole run: `sign_off_run(reviewer, decision, comment)`.

## Conventions

- Money is integer US cents. Periods are calendar months, `YYYY-MM`.
- Result id is `<transaction_id>#<check>`.
- Statuses: `pass`, `fail`, `needs_review`, `not_applicable`. A fail carries an
  `IssueCode`. The eval scores on transaction, check and status.
- The scored dataset's ledger entries carry their lines, debits equal to
  credits, with `account_type` set. The connectors' loader needs them.

## Changing it

`shared/` branch, a task labelled `contract-change`, both humans agree. Then
`uv run python -m auditorium_contracts export-schema` and `uv run pytest`.
What changed in 0.2.0 and why: `decisions/2026-11-09-contract-v0.2.md`.

## Not decided yet

- Nothing here has been checked against captured live Ramp or QuickBooks
  payloads. The 0.2.0 changes came from their documentation.
- Should a run save a copy of what its `Sources` returned, so a live run can be
  re-verified after the live data has moved on? Not built.
- Parsed PBC-list requests have no model yet.
- Receipts that are photos (no text layer) cannot be quoted.
- How `needs_review` is scored is the eval's call.
