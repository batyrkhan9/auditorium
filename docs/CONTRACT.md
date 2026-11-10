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
| `Person.role` | company records. Ramp's own "role" is a permission level and goes in `Person.platform_role` |
| `Person.id`, `Person.manager_id` | Ramp. Approvals point at Ramp's user ids, so those ids win |
| `SpendPolicy` | company records. Ramp has no policy in this shape |
| bank statements, the PBC list | company records (files) |

Other rules for live sources:

- `Document.sha256` is computed by the connector after it downloads the file.
  `Sources.document_file()` is the way to reach the file; `Document.path` is
  where the connector saved it.
- **People are joined on email.** A live `Sources` takes each Ramp user as a
  `Person` with Ramp's id and manager, then fills in `role` from the company
  record with the same email. No match means no job role, and a rule that needs
  one comes out as `needs_review`.
- `Document.origin`: use `default_origin(type)` only when the source gives no
  signal. It treats an unknown type as internal, the weaker kind of evidence.
- Approvals are the steps of the approval chain. A step is `approved`,
  `rejected` or `pending`. A pending step may have no single approver yet.
  An empty list means it was never sent for approval. `None` means the source
  could not say. Those are different, and the agent must not treat the second
  as the first. `None` is written out as `"approvals": null` in files and in
  fingerprints, so it is still `None` when read back. A record with no
  `approvals` key at all reads as an empty list, as it always has.
- `raw` holds the record as the source returned it, minus anything short-lived
  or secret such as presigned download links. It is saved in snapshots.
- `SpendTransaction.paid_date`: for a bill, the day payment left; for a card
  charge, the settlement date.
- **One `Sources` instance is one fetch.** Its methods return the same records
  every time, so what was checked, fingerprinted and snapshotted is one set of
  data.

## Verifying a run later: fingerprints and the snapshot

Live data moves on, and ids in a QuickBooks sandbox differ from the ids in the
dataset folder that was loaded into it. So a run carries its own proof.

1. Right after `run_started`, before any check, the agent calls
   `log_source_fingerprints(log, sources)`. That writes a sha256 of each kind of
   record into the hash-chained log.
2. The agent calls `snapshot_sources(sources, run/sources, manifest)`: a copy of
   what was fetched, as a dataset folder. It stays out of git.
3. `validate_run(run)` with no sources recomputes the snapshot's fingerprints,
   compares them with the logged ones, checks each document file against its
   `sha256`, and verifies every piece of evidence against the snapshot.

What that proves, and what it does not: the snapshot is what the run fetched,
unchanged since. It does not prove the fetch was honest; only checking against
the live sources does. So say **"verified against snapshot"** or **"verified
against live"**, never just "verified". `verified_against()` gives the word.

## Run folder

```
run.json        RunManifest    sources (live or local, synthetic or not, per system), dataset_path
results.jsonl   CheckResult    one per transaction and check
packets.jsonl   EvidencePacket sample mode
drafts.jsonl    FollowUpDraft  never sent
log.jsonl       LogEvent       hash-chained; written only through LogWriter
scorecard.json  Scorecard      added by the eval
sources/        a dataset folder: what the run fetched (not in git)
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
- **After a crash.** If the last line is cut off, `LogWriter` refuses to append
  and names the fix: `python -m auditorium_contracts repair-log <log>`. That
  removes the damaged end, keeps it in `<log>.damaged`, and the agent redoes
  the step that was being written. Damage anywhere but the end is not repaired;
  treat that log as tampered.
- **Repair only removes what a crash can leave**: a last line that is cut off
  or does not read as an event. A last line that reads as a complete event but
  does not match its own hash was edited. `repair-log` refuses it, changes
  nothing, and `verify-log` keeps reporting it.
- A `source_call` event is a fingerprint only when its detail has both `call`
  and `sha256`. The agent may log other source calls, such as a document
  download, with whatever detail it likes.
- **Sign-off on one result:** `sign_off(reviewer, result_id, decision, comment,
  result_sha256=shown)`, where `shown` is `result_hash()` of the result as the
  page displayed it. Pass `result=` too (the result as it is now) and a sign-off
  of something that changed meanwhile is refused.
- **Sign-off on the whole run:** `sign_off_run(reviewer, decision, comment)`. It
  pins `results.jsonl` and `run.json` by hash and must come after `run_finished`.
- Only the latest sign-off of a result, or of the run, is checked. Signing
  again after a correction clears the earlier one.
- `result_hash()` leaves out fields at their defaults, so adding an optional
  field to the contract later does not disturb existing sign-offs.

## Conventions

- Money is integer US cents. Periods are calendar months, `YYYY-MM`.
- Result id is `<transaction_id>#<check>`.
- Statuses: `pass`, `fail`, `needs_review`, `not_applicable`. A fail carries an
  `IssueCode`. The eval scores on transaction, check and status.
- The scored dataset's ledger entries carry their lines with `account_type`
  set, using QuickBooks' AccountType names. A bill or an expense has exactly
  one balancing line equal to `total_cents`. An expense has `payment_type`.
  The connectors' loader needs all three.
- A run's data is synthetic if any of its sources says so (`SourceUse.synthetic`).
  `RunManifest.synthetic_data` reads that; nobody sets it by hand.

## Changing it

`shared/` branch, a task labelled `contract-change`, both humans agree. Then
`uv run python -m auditorium_contracts export-schema` and `uv run pytest`.
What changed in 0.2.0 and why: `decisions/2026-11-09-contract-v0.2.md`.

## Not decided yet

- Nothing here has been checked against captured live Ramp or QuickBooks
  payloads. The 0.2.0 changes came from their documentation and from code review.
- On Ramp, card approvals may sit on the spend request, not on the transaction.
  Unknown until a real payload is captured.
- Parsed PBC-list requests have no model yet.
- Receipts that are photos (no text layer) cannot be quoted.
- How `needs_review` is scored is the eval's call.
