"""A dataset on disk, and the `Sources` interface the agent reads through.

A dataset folder looks like this:

    manifest.json        DatasetManifest
    transactions.jsonl   SpendTransaction, one per line
    ledger.jsonl         LedgerEntry
    documents.jsonl      Document
    people.jsonl         Person
    policy.json          SpendPolicy
    documents/...        the files Document.path points at
    samples/<id>.json    SampleRequest (optional)

`Sources` is the seam between the connectors and the agent. The agent only ever
talks to a `Sources`. `LocalSources` serves a dataset folder. The connectors
area provides the live one, backed by Ramp and QuickBooks.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from .records import (
    DatasetManifest,
    Document,
    LedgerEntry,
    Person,
    SampleRequest,
    SourceSystem,
    SpendPolicy,
    SpendTransaction,
)
from .results import SourceUse
from .text import page_texts

Model = TypeVar("Model", bound=BaseModel)


def read_jsonl(path: Path | str, model: type[Model]) -> list[Model]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [model.model_validate_json(line) for line in lines if line.strip()]


def write_jsonl(path: Path | str, records: Iterable[BaseModel]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(record.model_dump_json(exclude_none=True) + "\n" for record in records)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path | str, record: BaseModel) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Dataset:
    root: Path
    manifest: DatasetManifest
    transactions: tuple[SpendTransaction, ...]
    ledger: tuple[LedgerEntry, ...]
    documents: tuple[Document, ...]
    people: tuple[Person, ...]
    policy: SpendPolicy
    samples: tuple[SampleRequest, ...]


def load_dataset(root: Path | str) -> Dataset:
    root = Path(root)
    samples = sorted((root / "samples").glob("*.json")) if (root / "samples").is_dir() else []
    return Dataset(
        root=root,
        manifest=DatasetManifest.model_validate_json((root / "manifest.json").read_text("utf-8")),
        transactions=tuple(read_jsonl(root / "transactions.jsonl", SpendTransaction)),
        ledger=tuple(read_jsonl(root / "ledger.jsonl", LedgerEntry)),
        documents=tuple(read_jsonl(root / "documents.jsonl", Document)),
        people=tuple(read_jsonl(root / "people.jsonl", Person)),
        policy=SpendPolicy.model_validate_json((root / "policy.json").read_text("utf-8")),
        samples=tuple(SampleRequest.model_validate_json(p.read_text("utf-8")) for p in samples),
    )


def _duplicates(ids: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    return {i for i in ids if i in seen or seen.add(i)}


def validate_dataset(root: Path | str) -> list[str]:
    """Problems with a dataset folder. An empty list means every area can rely on it."""
    root = Path(root)
    try:
        data = load_dataset(root)
    except (OSError, ValidationError, ValueError) as error:
        return [f"cannot load dataset: {error}"]

    problems: list[str] = []
    for kind, records in (
        ("transaction", data.transactions),
        ("ledger entry", data.ledger),
        ("document", data.documents),
        ("person", data.people),
    ):
        for duplicate in sorted(_duplicates(r.id for r in records)):
            problems.append(f"duplicate {kind} id: {duplicate}")

    documents = {d.id for d in data.documents}
    people = {p.id for p in data.people}

    for document in data.documents:
        file = root / document.path
        if not file.is_file():
            problems.append(f"document {document.id}: file missing at {document.path}")
        elif sha256_file(file) != document.sha256:
            problems.append(f"document {document.id}: sha256 does not match the file")
        elif not any(text.strip() for text in page_texts(file)):
            problems.append(
                f"document {document.id}: no readable text, so quotes cannot be verified"
            )

    for txn in data.transactions:
        for document_id in txn.document_ids:
            if document_id not in documents:
                problems.append(f"transaction {txn.id}: unknown document {document_id}")
        if txn.spender_id and txn.spender_id not in people:
            problems.append(f"transaction {txn.id}: unknown spender {txn.spender_id}")
        for approval in txn.approvals:
            if approval.approver_id not in people:
                problems.append(f"transaction {txn.id}: unknown approver {approval.approver_id}")

    for person in data.people:
        if person.manager_id and person.manager_id not in people:
            problems.append(f"person {person.id}: unknown manager {person.manager_id}")
    if data.policy.document_id and data.policy.document_id not in documents:
        problems.append(f"policy: unknown document {data.policy.document_id}")
    for sample in data.samples:
        if sample.document_id and sample.document_id not in documents:
            problems.append(f"sample {sample.id}: unknown document {sample.document_id}")

    manifest = data.manifest
    if not manifest.fiscal_year_start <= manifest.fiscal_year_end <= manifest.coverage_end:
        problems.append(
            "manifest: fiscal_year_start, fiscal_year_end, coverage_end are out of order"
        )
    return problems


@runtime_checkable
class Sources(Protocol):
    """Read access to the company's records, wherever they really live.

    Every method returns contract records. Implementations fetch everything in
    scope; the data is hundreds of records, not millions.
    """

    def describe(self) -> list[SourceUse]:
        """Which systems this reads, and whether each is live or local."""
        ...

    def transactions(self) -> list[SpendTransaction]: ...

    def ledger_entries(self) -> list[LedgerEntry]: ...

    def documents(self) -> list[Document]: ...

    def document_file(self, document_id: str) -> Path:
        """A local path to the document's bytes, downloading it first if needed."""
        ...

    def people(self) -> list[Person]: ...

    def policy(self) -> SpendPolicy: ...


class LocalSources:
    """`Sources` over a dataset folder. Also the fallback when a live system is unreachable."""

    def __init__(self, root: Path | str):
        self.dataset = load_dataset(root)
        self._documents = {d.id: d for d in self.dataset.documents}

    def describe(self) -> list[SourceUse]:
        systems = (SourceSystem.RAMP, SourceSystem.QBO, SourceSystem.BANK, SourceSystem.COMPANY)
        detail = f"dataset {self.dataset.manifest.dataset_id}"
        return [SourceUse(system=system, mode="local", detail=detail) for system in systems]

    def transactions(self) -> list[SpendTransaction]:
        return list(self.dataset.transactions)

    def ledger_entries(self) -> list[LedgerEntry]:
        return list(self.dataset.ledger)

    def documents(self) -> list[Document]:
        return list(self.dataset.documents)

    def document_file(self, document_id: str) -> Path:
        return self.dataset.root / self._documents[document_id].path

    def people(self) -> list[Person]:
        return list(self.dataset.people)

    def policy(self) -> SpendPolicy:
        return self.dataset.policy
