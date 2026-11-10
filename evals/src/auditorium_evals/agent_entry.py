"""Runs the agent's pre-flight once. The harness starts this in a child process.

    python -m auditorium_evals.agent_entry DATASET RUN_DIR --ramp dataset|live --qbo live|dataset

The child runs with its working directory outside the sealed clone, on a copy of
the public dataset, so the agent can reach nothing but that copy and the live
systems. It prints one JSON line on stdout: the run id and what `validate_run`
said. Everything else it writes goes into RUN_DIR.

After the agent finishes, the same `Sources` instance is saved into RUN_DIR/sources
(`snapshot_sources`). One instance is one fetch (contract), so that snapshot is the
data the agent checked, and the eval verifies evidence against it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from auditorium_contracts import (
    SOURCES_SNAPSHOT,
    LocalSources,
    Sources,
    snapshot_sources,
    validate_run,
)

RAMP_ENV = ("RAMP_CLIENT_ID", "RAMP_CLIENT_SECRET", "RAMP_MCP_URL", "RAMP_MCP_TOKEN")
QBO_ENV = ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REFRESH_TOKEN", "QBO_REALM_ID")


def build_sources(dataset: Path, ramp: str, qbo: str) -> Sources:
    """The `Sources` for a scored run. Ramp's side from the dataset unless asked
    otherwise (data/decisions/2026-11-10-scored-runs-read-ramp-from-the-dataset.md),
    QuickBooks live unless asked otherwise. `describe()` says what really happened."""
    if ramp == "dataset" and qbo == "dataset":
        return LocalSources(dataset)
    if ramp == "dataset":
        for name in RAMP_ENV:
            os.environ.pop(name, None)
    if qbo == "dataset":
        for name in QBO_ENV:
            os.environ.pop(name, None)
    from auditorium_connectors import connect_sources

    return connect_sources(dataset, cache_dir=dataset.parent / "ramp-cache")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auditorium_evals.agent_entry")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--ramp", choices=("dataset", "live"), default="dataset")
    parser.add_argument("--qbo", choices=("live", "dataset"), default="live")
    args = parser.parse_args(argv)

    from auditorium_agent.preflight import run_preflight

    sources = build_sources(args.dataset, args.ramp, args.qbo)
    manifest = LocalSources(args.dataset).dataset.manifest
    run_id = f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    run_preflight(
        sources,
        args.run_dir,
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
        run_id=run_id,
        dataset_id=manifest.dataset_id,
    )
    snapshot_sources(sources, args.run_dir / SOURCES_SNAPSHOT, manifest)
    problems = validate_run(args.run_dir, sources)
    print(json.dumps({"run_id": run_id, "validate_run_against_sources": problems}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
