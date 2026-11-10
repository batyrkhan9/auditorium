"""Ramp and QuickBooks connections behind the contract's Sources interface."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from auditorium_contracts import (
    Document,
    LedgerEntry,
    LocalSources,
    Person,
    Sources,
    SourceSystem,
    SourceUse,
    SpendPolicy,
    SpendTransaction,
)

from .fallback import LocalFallback
from .qbo import QboLedger
from .ramp import RampSources, connect_ramp


class ConnectedSources:
    """Everything from `base` (Ramp or local), except the ledger, which QuickBooks answers."""

    def __init__(self, base: Sources, ledger: QboLedger):
        self.base = base
        self.ledger = ledger

    def describe(self) -> list[SourceUse]:
        uses = [u for u in self.base.describe() if u.system is not SourceSystem.QBO]
        return [*uses, self.ledger.describe()]

    def ledger_entries(self) -> list[LedgerEntry]:
        return self.ledger.ledger_entries()

    def transactions(self) -> list[SpendTransaction]:
        return self.base.transactions()

    def documents(self) -> list[Document]:
        return self.base.documents()

    def document_file(self, document_id: str) -> Path:
        return self.base.document_file(document_id)

    def people(self) -> list[Person]:
        return self.base.people()

    def policy(self) -> SpendPolicy:
        return self.base.policy()


def connect_sources(dataset_root: Path | str, cache_dir: Path | str | None = None) -> Sources:
    """The best `Sources` available now. `describe()` says which systems are live.

    Reads RAMP_MCP_URL and RAMP_MCP_TOKEN (Ramp) and QBO_* (QuickBooks) from the
    environment. Bank and policy come from the dataset at `dataset_root`, as does
    any system that is not live.
    """
    ramp = connect_ramp(
        LocalSources(dataset_root),
        url=os.environ.get("RAMP_MCP_URL"),
        token=os.environ.get("RAMP_MCP_TOKEN"),
        cache_dir=cache_dir or Path(tempfile.gettempdir()) / "auditorium-ramp",
    )
    return ConnectedSources(ramp, QboLedger(fallback=dataset_root))


__all__ = [
    "ConnectedSources",
    "LocalFallback",
    "QboLedger",
    "RampSources",
    "connect_ramp",
    "connect_sources",
]
