"""`LocalSources` that says which live systems it stands in for, and why."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from auditorium_contracts import (
    Document,
    LedgerEntry,
    LocalSources,
    Person,
    SourceSystem,
    SourceUse,
    SpendPolicy,
    SpendTransaction,
)


class LocalFallback:
    """Serves the local dataset. `describe()` carries the reason each live system was not used."""

    def __init__(self, local: LocalSources, reasons: Mapping[SourceSystem, str]):
        self.local = local
        self.reasons = dict(reasons)

    def describe(self) -> list[SourceUse]:
        uses = []
        for use in self.local.describe():
            reason = self.reasons.get(use.system)
            if reason:
                use = SourceUse(
                    system=use.system, mode="local", detail=f"{use.detail}; not live: {reason}"
                )
            uses.append(use)
        return uses

    def transactions(self) -> list[SpendTransaction]:
        return self.local.transactions()

    def ledger_entries(self) -> list[LedgerEntry]:
        return self.local.ledger_entries()

    def documents(self) -> list[Document]:
        return self.local.documents()

    def document_file(self, document_id: str) -> Path:
        return self.local.document_file(document_id)

    def people(self) -> list[Person]:
        return self.local.people()

    def policy(self) -> SpendPolicy:
        return self.local.policy()
