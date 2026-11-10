"""A run on disk, and the checks that make its evidence trustworthy.

A run folder looks like this:

    run.json         RunManifest
    results.jsonl    CheckResult, one per transaction and check
    packets.jsonl    EvidencePacket (sample mode)
    drafts.jsonl     FollowUpDraft
    log.jsonl        LogEvent, hash-chained (see log.py)
    scorecard.json   Scorecard, written by the eval (optional)

`verify_evidence` is the shared meaning of "the agent did not make this up".
The agent can call it before writing a result, and the eval calls it on every
result. One definition, so they cannot disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .dataset import LocalSources, Sources, fingerprint_sources, read_jsonl, sha256_file
from .layout import (
    DRAFTS,
    LOG,
    PACKETS,
    RESULTS,
    RUN_MANIFEST,
    SCORECARD,
    SOURCES_SNAPSHOT,
)
from .log import LogAction, LogEvent, read_log, result_hash, verify_log
from .results import (
    CheckResult,
    DocumentEvidence,
    EvidencePacket,
    FollowUpDraft,
    RecordEvidence,
    RunManifest,
    Scorecard,
    SearchEvidence,
)
from .text import quote_on_page


@dataclass(frozen=True)
class Run:
    root: Path
    manifest: RunManifest
    results: tuple[CheckResult, ...]
    packets: tuple[EvidencePacket, ...]
    drafts: tuple[FollowUpDraft, ...]
    log: tuple[LogEvent, ...]
    scorecard: Scorecard | None


def load_run(root: Path | str) -> Run:
    root = Path(root)

    def optional(name: str, model: type[BaseModel]) -> tuple:
        return tuple(read_jsonl(root / name, model)) if (root / name).is_file() else ()

    scorecard = root / SCORECARD
    return Run(
        root=root,
        manifest=RunManifest.model_validate_json((root / RUN_MANIFEST).read_text("utf-8")),
        results=tuple(read_jsonl(root / RESULTS, CheckResult)),
        packets=optional(PACKETS, EvidencePacket),
        drafts=optional(DRAFTS, FollowUpDraft),
        log=tuple(read_log(root / LOG)),
        scorecard=Scorecard.model_validate_json(scorecard.read_text("utf-8"))
        if scorecard.is_file()
        else None,
    )


class SourceIndex:
    """Source records by kind and id, for resolving evidence."""

    def __init__(self, sources: Sources):
        self.sources = sources
        policy = sources.policy()
        self.records: dict[str, dict[str, BaseModel]] = {
            "transaction": {r.id: r for r in sources.transactions()},
            "ledger_entry": {r.id: r for r in sources.ledger_entries()},
            "document": {r.id: r for r in sources.documents()},
            "person": {r.id: r for r in sources.people()},
            "policy": {policy.id: policy},
        }
        self.all_ids = {record_id for kind in self.records.values() for record_id in kind}
        self._file_checked: dict[str, bool] = {}

    def file_is_the_recorded_one(self, document_id: str, file: Path) -> bool:
        """True if the file's bytes match the document's recorded sha256. Hashed once per run."""
        if document_id not in self._file_checked:
            recorded = self.records["document"][document_id].sha256
            self._file_checked[document_id] = sha256_file(file) == recorded
        return self._file_checked[document_id]


def _field(record: BaseModel, path: str) -> Any:
    value: Any = record.model_dump(mode="json")
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        else:
            value = value[part]
    return value


def verify_evidence(
    evidence: DocumentEvidence | RecordEvidence | SearchEvidence, index: SourceIndex
) -> str | None:
    """Why this evidence does not resolve to something real, or None if it does."""
    if isinstance(evidence, DocumentEvidence):
        if evidence.document_id not in index.records["document"]:
            return f"document {evidence.document_id} does not exist"
        file = index.sources.document_file(evidence.document_id)
        if not file.is_file():
            return f"document {evidence.document_id} has no file"
        if not index.file_is_the_recorded_one(evidence.document_id, file):
            return f"document {evidence.document_id}: the file served is not the one its sha256 records"
        if not quote_on_page(file, evidence.page, evidence.quote):
            return f"quote not found on page {evidence.page} of {evidence.document_id}: {evidence.quote!r}"
        return None
    if isinstance(evidence, RecordEvidence):
        record = index.records[evidence.record_type].get(evidence.record_id)
        if record is None:
            return f"{evidence.record_type} {evidence.record_id} does not exist"
        try:
            actual = _field(record, evidence.field)
        except (KeyError, IndexError, ValueError, TypeError):
            return f"{evidence.record_type} {evidence.record_id} has no field {evidence.field}"
        shown = actual if isinstance(actual, str) else json.dumps(actual)
        if shown != evidence.value:
            return (
                f"{evidence.record_type} {evidence.record_id}.{evidence.field} is {shown!r}, "
                f"not {evidence.value!r}"
            )
        return None
    unknown = [i for i in evidence.result_ids if i not in index.all_ids]
    if unknown:
        return f"search returned ids that do not exist: {', '.join(unknown)}"
    return None


def verified_against(root: Path | str, sources: Sources | None) -> str:
    """What a clean `validate_run` proves: 'snapshot' or 'the sources given'.

    Say which, never just 'verified'. A snapshot shows the evidence matches what the
    run fetched. It does not show the fetch was honest; only the live sources do that.
    """
    return "the sources given" if sources is not None else "snapshot"


def _snapshot_problems(root: Path, log: tuple[LogEvent, ...]) -> list[str]:
    """Compare the run's snapshot with the fingerprints logged when the data was fetched."""
    logged = {
        event.detail["call"]: event.detail["sha256"]
        for event in log
        # Only fingerprint events carry both. The agent also logs other source calls,
        # a document download for one, and those are not fingerprints.
        if event.action is LogAction.SOURCE_CALL
        and "call" in event.detail
        and "sha256" in event.detail
    }
    if not logged:
        return ["snapshot: the log holds no source fingerprints, so it cannot be tied to the fetch"]
    try:
        current = fingerprint_sources(LocalSources(root / SOURCES_SNAPSHOT))
    except (OSError, ValidationError, ValueError) as error:
        return [f"snapshot: cannot be loaded ({error})"]
    return [
        f"snapshot: {call} is not what the run fetched"
        for call, digest in sorted(current.items())
        if logged.get(call) != digest
    ]


def validate_run(root: Path | str, sources: Sources | None = None) -> list[str]:
    """Problems with a run folder. An empty list means the eval and the screen can rely on it.

    Evidence is verified against `sources`: the ones the run really used. Without
    them it is verified against the run's own snapshot (`sources/`), which is first
    checked against the fingerprints the run logged when it fetched the data.
    See `verified_against` for what each of the two proves.
    """
    root = Path(root)
    try:
        run = load_run(root)
    except (OSError, ValidationError, ValueError) as error:
        return [f"cannot load run: {error}"]

    problems = verify_log(root / LOG)
    has_snapshot = (root / SOURCES_SNAPSHOT).is_dir()
    if has_snapshot:
        problems += _snapshot_problems(root, run.log)
    if sources is None:
        if not has_snapshot:
            return [*problems, "no sources were given and the run folder has no snapshot"]
        try:
            sources = LocalSources(root / SOURCES_SNAPSHOT)
        except (OSError, ValidationError, ValueError) as error:
            return [*problems, f"snapshot: cannot be loaded ({error})"]
    index = SourceIndex(sources)
    run_id = run.manifest.run_id

    seen: set[str] = set()
    for result in run.results:
        if result.id in seen:
            problems.append(f"result {result.id}: appears more than once")
        seen.add(result.id)
        if result.run_id != run_id:
            problems.append(f"result {result.id}: run_id is {result.run_id}")
        if result.transaction_id not in index.records["transaction"]:
            problems.append(f"result {result.id}: unknown transaction")
        for evidence in result.evidence:
            problem = verify_evidence(evidence, index)
            if problem:
                problems.append(f"result {result.id}: {problem}")

    drafts = {draft.id for draft in run.drafts}
    for draft in run.drafts:
        for linked in draft.result_ids:
            if linked not in seen:
                problems.append(f"draft {draft.id}: unknown result {linked}")
    for packet in run.packets:
        if packet.transaction_id and packet.transaction_id not in index.records["transaction"]:
            problems.append(f"packet {packet.id}: unknown transaction {packet.transaction_id}")
        for item in packet.items:
            problem = verify_evidence(item.evidence, index)
            if problem:
                problems.append(f"packet {packet.id}: {problem}")
        for linked in packet.result_ids:
            if linked not in seen:
                problems.append(f"packet {packet.id}: unknown result {linked}")
        for linked in packet.draft_ids:
            if linked not in drafts:
                problems.append(f"packet {packet.id}: unknown draft {linked}")

    # Only the latest sign-off of each thing counts: a result signed again after a change is fine.
    by_id = {result.id: result for result in run.results}
    latest: dict[str, LogEvent] = {}
    finished_at_seq = None
    for event in run.log:
        if event.action is LogAction.RUN_FINISHED:
            finished_at_seq = event.seq
        if event.action is LogAction.SIGNOFF:
            for subject in event.subject_ids:
                latest[subject] = event
    for subject, event in latest.items():
        if event.detail.get("scope") == "run":
            if finished_at_seq is None or event.seq < finished_at_seq:
                problems.append(f"log line {event.seq}: the run was signed off before it finished")
            for key, name in (("results_sha256", RESULTS), ("run_sha256", RUN_MANIFEST)):
                pinned = event.detail.get(key)
                if pinned and pinned != sha256_file(root / name):
                    problems.append(f"{name}: changed after the run was signed off")
            continue
        signed = event.detail.get("result_sha256")
        if subject not in by_id:
            problems.append(f"log line {event.seq}: signs off unknown result {subject}")
        elif signed and result_hash(by_id[subject]) != signed:
            problems.append(f"result {subject}: changed after it was signed off")

    logged = {
        subject
        for event in run.log
        if event.action is LogAction.CHECK_RECORDED
        for subject in event.subject_ids
    }
    for missing in sorted(seen - logged):
        problems.append(f"result {missing}: no check_recorded event in the log")
    if not run.log or run.log[0].action is not LogAction.RUN_STARTED:
        problems.append("log: the first event must be run_started")
    for event in run.log:
        if event.run_id != run_id:
            problems.append(f"log line {event.seq}: run_id is {event.run_id}")
    return problems
