"""Ramp and QuickBooks connections behind the contract's Sources interface."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from auditorium_contracts import LocalSources, Sources

from .fallback import LocalFallback
from .ramp import RampSources, connect_ramp


def connect_sources(dataset_root: Path | str, cache_dir: Path | str | None = None) -> Sources:
    """The best `Sources` available now. `describe()` says which systems are live.

    Reads RAMP_MCP_URL and RAMP_MCP_TOKEN from the environment. Ledger, bank and
    policy come from the dataset at `dataset_root` until their connectors land.
    """
    local = LocalSources(dataset_root)
    return connect_ramp(
        local,
        url=os.environ.get("RAMP_MCP_URL"),
        token=os.environ.get("RAMP_MCP_TOKEN"),
        cache_dir=cache_dir or Path(tempfile.gettempdir()) / "auditorium-ramp",
    )


__all__ = ["LocalFallback", "RampSources", "connect_ramp", "connect_sources"]
