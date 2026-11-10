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
    ramp, coverage = _ramp_for_dataset(ramp, local)
    if coverage:
        note = f"{coverage}; {note}"
    return ConnectedSources(ramp, QboLedger(fallback=dataset_root), ramp_note=note)


def _ramp_for_dataset(ramp: Sources, local: LocalSources) -> tuple[Sources, str]:
    """Live Ramp only if it holds the dataset's Ramp transactions.

    Bills the loader stamped from this dataset there (ramp_loader.py): those bills are
    served live and the rest from the dataset; see
    connectors/decisions/2026-11-10-ramp-bill-loader.md. Otherwise matched by Ramp id.
    None of them there: Ramp holds another company, and checking the dataset's ledger
    against it would flag every transaction, so the dataset is used instead. Some of
    them there: Ramp stays live and the returned note says how many are missing.
    See connectors/decisions/2026-11-10-ramp-must-match-dataset.md.
    """
    if isinstance(ramp, LocalFallback):
        return ramp, ""
    # Imported here so `python -m auditorium_connectors.ramp_loader` runs it only once.
    from .ramp_loader import LoadedBillsSources, stamped_bills

    if isinstance(ramp, RampSources) and stamped_bills(ramp, local):
        return LoadedBillsSources(ramp, local), ""
    wanted = {
        (t.source.record_type, t.source.record_id)
        for t in local.transactions()
        if t.source.system is SourceSystem.RAMP
    }
    if not wanted:
        return ramp, ""
    found = {(t.source.record_type, t.source.record_id) for t in ramp.transactions()}
    dataset_id = local.dataset.manifest.dataset_id
    if not wanted & found:
        reason = (
            f"Ramp holds a different company (0 of {len(wanted)} Ramp transactions of "
            f"dataset {dataset_id} found among {len(found)} read from Ramp); using the dataset"
        )
        return LocalFallback(local, {SourceSystem.RAMP: reason}), ""
    return ramp, (
        f"dataset {dataset_id}: {len(wanted - found)} of {len(wanted)} Ramp transactions "
        f"missing, {len(found - wanted)} Ramp transactions not from the dataset"
    )


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
