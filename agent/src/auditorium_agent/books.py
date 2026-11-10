"""Everything a check may look at, with every look written to the log."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from auditorium_contracts import (
    Document,
    LedgerEntry,
    LogAction,
    LogWriter,
    Person,
    SearchEvidence,
    SourceIndex,
    Sources,
    SourceSystem,
    SpendPolicy,
    SpendTransaction,
    normalize,
    page_texts,
)

from .matching import Match, match_ledger
from .receipt_reader import Line
from .service_period import ServicePeriod

if TYPE_CHECKING:  # reader.py imports the Anthropic SDK; only the type is needed here
    from .reader import Reader


@dataclass
class Books:
    sources: Sources
    log: LogWriter
    fiscal_year_start: date
    fiscal_year_end: date
    transactions: list[SpendTransaction]
    ledger: list[LedgerEntry]
    documents: dict[str, Document]
    policy: SpendPolicy
    people: dict[str, Person]
    index: SourceIndex
    matches: dict[str, Match]  # transaction id -> its ledger entry, paired one-to-one
    reader: Reader | None = None  # asked only where plain code cannot settle something
    models_used: set[str] = field(default_factory=set)

    @classmethod
    def open(
        cls,
        sources: Sources,
        log: LogWriter,
        fiscal_year_start: date,
        fiscal_year_end: date,
        reader: Reader | None = None,
    ):
        transactions = sources.transactions()
        ledger = sources.ledger_entries()
        documents = sources.documents()
        for call, records in (
            ("transactions", transactions),
            ("ledger_entries", ledger),
            ("documents", documents),
        ):
            log.append("agent", LogAction.SOURCE_CALL, detail={"call": call, "count": len(records)})
        return cls(
            sources=sources,
            log=log,
            fiscal_year_start=fiscal_year_start,
            fiscal_year_end=fiscal_year_end,
            transactions=transactions,
            ledger=ledger,
            documents={d.id: d for d in documents},
            policy=sources.policy(),
            people={p.id: p for p in sources.people()},
            index=SourceIndex(sources),
            matches=match_ledger(transactions, ledger),
            reader=reader,
        )

    def _model_answer(self, purpose: str, document_id: str, pages: tuple[str, ...], found):
        """Log a model call, and keep its answer only if the line it quotes is in the document."""
        page, quote = (found.page, getattr(found, "quote", None) or getattr(found, "text", ""))
        on_page = 1 <= page <= len(pages) and bool(quote)
        verified = on_page and normalize(quote) in normalize(pages[page - 1])
        call = dict(getattr(self.reader, "last_call", {}))
        model = call.get("served_by") or self.reader.model
        self.models_used.add(model)
        outcome = "used" if verified else "discarded: the quoted line is not in the document"
        self.log.append(
            "agent",
            LogAction.MODEL_CALL,
            actor_id=model,
            subject_ids=[document_id],
            detail={"purpose": purpose, "outcome": outcome, **call},
        )
        return found if verified else None

    def _no_answer(self, purpose: str, document_id: str):
        call = dict(getattr(self.reader, "last_call", {}))
        model = call.get("served_by") or self.reader.model
        if "error" not in call:
            self.models_used.add(model)
        outcome = "failed" if "error" in call else "nothing found"
        self.log.append(
            "agent",
            LogAction.ERROR if "error" in call else LogAction.MODEL_CALL,
            actor_id=model,
            subject_ids=[document_id],
            detail={"purpose": purpose, "outcome": outcome, **call},
        )

    def ask_service_period(self, document_id: str, pages: tuple[str, ...]) -> ServicePeriod | None:
        """When the work was done, read by the model. None without a reader or a verified quote."""
        if self.reader is None:
            return None
        found = self.reader.service_period(pages)
        if found is None:
            return self._no_answer("service_period", document_id)
        return self._model_answer("service_period", document_id, pages, found)

    def ask_merchant(
        self, document_id: str, vendor_name: str, pages: tuple[str, ...]
    ) -> tuple[Line, str] | None:
        """The receipt line naming the business, and the model that judged it the same merchant."""
        if self.reader is None:
            return None
        found = self.reader.merchant(vendor_name, pages)
        if found is None:
            return self._no_answer("merchant", document_id)
        line = self._model_answer("merchant", document_id, pages, found)
        if line is None:
            return None
        model = dict(getattr(self.reader, "last_call", {})).get("served_by") or self.reader.model
        return line, model

    def side(self, day: date) -> int:
        """-1 before the fiscal year under audit, 0 inside it, 1 after it."""
        if day < self.fiscal_year_start:
            return -1
        return 1 if day > self.fiscal_year_end else 0

    def read_document(self, document_id: str) -> tuple[str, ...]:
        pages = page_texts(self.sources.document_file(document_id))
        self.log.append(
            "agent",
            LogAction.DOCUMENT_READ,
            subject_ids=[document_id],
            detail={"pages": len(pages)},
        )
        return pages

    def search(self, system: SourceSystem, query: str, result_ids: list[str]) -> SearchEvidence:
        """Record a search in the log and return it as evidence."""
        self.log.append(
            "agent",
            LogAction.SOURCE_CALL,
            subject_ids=result_ids,
            detail={"call": "search", "system": system.value, "query": query},
        )
        return SearchEvidence(system=system, query=query, result_ids=result_ids)
