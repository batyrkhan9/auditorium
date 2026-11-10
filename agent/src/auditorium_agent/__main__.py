"""Run the agent.

python -m auditorium_agent preflight DATASET_DIR [--out RUN_DIR]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from auditorium_contracts import LocalSources, validate_run

from .preflight import run_preflight


def main() -> int:
    parser = argparse.ArgumentParser(prog="auditorium_agent", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="check every transaction in a dataset folder")
    preflight.add_argument("dataset")
    preflight.add_argument("--out", help="run folder to create (default: runs/<run id>)")
    args = parser.parse_args()

    sources = LocalSources(args.dataset)
    manifest = sources.dataset.manifest
    run_id = f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    out = Path(args.out) if args.out else Path("runs") / run_id
    results = run_preflight(
        sources,
        out,
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
        run_id=run_id,
        dataset_id=manifest.dataset_id,
    )

    vendors = {txn.id: txn.vendor_name for txn in sources.transactions()}
    for result in results:
        print(
            f"{result.status.value.upper():<15}{result.check.value:<14}{vendors[result.transaction_id]}"
        )
        print(f"{'':<15}{result.summary}")
    problems = validate_run(out, sources)
    for problem in problems:
        print(f"PROBLEM  {problem}", file=sys.stderr)
    print(f"\n{len(results)} results in {out}" + ("" if problems else " (run folder valid)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
