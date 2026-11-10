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

from .dataset import Sources, read_jsonl
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

RUN_MANIFEST = "run.json"
RESULTS = "results.jsonl"
PACKETS = "packets.jsonl"
DRAFTS = "drafts.jsonl"
LOG = "log.jsonl"
SCORECARD = "scorecard.json"


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


def validate_run(root: Path | str, sources: Sources) -> list[str]:
    """Problems with a run folder. An empty list means the eval and the screen can rely on it."""
    root = Path(root)
    try:
        run = load_run(root)
    except (OSError, ValidationError, ValueError) as error:
        return [f"cannot load run: {error}"]

    problems = verify_log(root / LOG)
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

    by_id = {result.id: result for result in run.results}
    for event in run.log:
        signed = event.detail.get("result_sha256")
        if event.action is not LogAction.SIGNOFF or not signed:
            continue
        for subject in event.subject_ids:
            if subject not in by_id:
                problems.append(f"log line {event.seq}: signs off unknown result {subject}")
            elif result_hash(by_id[subject]) != signed:
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
