"""Everything a check may look at, with every look written to the log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from auditorium_contracts import (
    Document,
    LedgerEntry,
    LogAction,
    LogWriter,
    SearchEvidence,
    SourceIndex,
    Sources,
    SourceSystem,
    SpendTransaction,
    page_texts,
)

from .matching import Match, match_ledger


@dataclass
class Books:
    sources: Sources
    log: LogWriter
    fiscal_year_start: date
    fiscal_year_end: date
    transactions: list[SpendTransaction]
    ledger: list[LedgerEntry]
    documents: dict[str, Document]
    index: SourceIndex
    matches: dict[str, Match]  # transaction id -> its ledger entry, paired one-to-one

    @classmethod
    def open(cls, sources: Sources, log: LogWriter, fiscal_year_start: date, fiscal_year_end: date):
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
            index=SourceIndex(sources),
            matches=match_ledger(transactions, ledger),
        )

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
