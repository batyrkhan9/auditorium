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
from .ramp_api import API_URL, ApiRampSources, RampApiClient, connect_ramp_api


class ConnectedSources:
    """Everything from `base` (Ramp or local), except the ledger, which QuickBooks answers."""

    def __init__(self, base: Sources, ledger: QboLedger, ramp_note: str = ""):
        self.base = base
        self.ledger = ledger
        # Which Ramp path was taken and why the others were not.
        self.ramp_note = ramp_note

    def describe(self) -> list[SourceUse]:
        uses = [
            u.model_copy(update={"detail": f"{u.detail}; {self.ramp_note}"})
            if u.system is SourceSystem.RAMP and self.ramp_note
            else u
            for u in self.base.describe()
            if u.system is not SourceSystem.QBO
        ]
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

    Ramp: the Developer API if RAMP_CLIENT_ID and RAMP_CLIENT_SECRET are set
    (RAMP_API_URL, default the sandbox); else the MCP server (RAMP_MCP_URL,
    RAMP_MCP_TOKEN); else local. A failed path falls back to local, not to the next
    path. QuickBooks reads QBO_*. Bank and policy come from the dataset at
    `dataset_root`, as does any system that is not live.
    """
    env = os.environ
    local = LocalSources(dataset_root)
    cache = cache_dir or Path(tempfile.gettempdir()) / "auditorium-ramp"
    client_id = env.get("RAMP_CLIENT_ID", "").strip()
    client_secret = env.get("RAMP_CLIENT_SECRET", "").strip()
    mcp_url = env.get("RAMP_MCP_URL", "").strip()
    if client_id and client_secret:
        ramp = connect_ramp_api(
            local,
            client_id=client_id,
            client_secret=client_secret,
            base_url=env.get("RAMP_API_URL", "").strip() or API_URL,
            cache_dir=cache,
        )
        note = "Ramp path: Developer API (RAMP_CLIENT_ID and RAMP_CLIENT_SECRET are set)"
        if mcp_url:
            note += "; MCP not tried: the Developer API comes first"
    else:
        ramp = connect_ramp(
            local, url=mcp_url or None, token=env.get("RAMP_MCP_TOKEN"), cache_dir=cache
        )
        unset = [
            n
            for n, v in (("RAMP_CLIENT_ID", client_id), ("RAMP_CLIENT_SECRET", client_secret))
            if not v
        ]
        note = f"Developer API not used: {' and '.join(unset)} not set"
        if mcp_url:
            note = f"Ramp path: MCP (RAMP_MCP_URL is set); {note}"
    return ConnectedSources(ramp, QboLedger(fallback=dataset_root), ramp_note=note)


__all__ = [
    "ApiRampSources",
    "ConnectedSources",
    "LocalFallback",
    "QboLedger",
    "RampApiClient",
    "RampSources",
    "connect_ramp",
    "connect_ramp_api",
    "connect_sources",
]
