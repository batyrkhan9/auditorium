"""Run the agent.

python -m auditorium_agent preflight DATASET_DIR [--out RUN_DIR]
python -m auditorium_agent sample DATASET_DIR --request SAMPLE.json [--out RUN_DIR]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from auditorium_contracts import LocalSources, SampleRequest, validate_run

from .preflight import run_preflight
from .sample import run_sample


def main() -> int:
    parser = argparse.ArgumentParser(prog="auditorium_agent", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="check every transaction in a dataset folder")
    sample = commands.add_parser("sample", help="assemble evidence for the auditor's picks")
    sample.add_argument("--request", required=True, help="the auditor's SampleRequest, as JSON")
    for command in (preflight, sample):
        command.add_argument("dataset")
        command.add_argument("--out", help="run folder to create (default: runs/<run id>)")
    args = parser.parse_args()

    sources = LocalSources(args.dataset)
    manifest = sources.dataset.manifest
    run_id = f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    out = Path(args.out) if args.out else Path("runs") / run_id
    common = dict(
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
        run_id=run_id,
        dataset_id=manifest.dataset_id,
    )
    vendors = {txn.id: txn.vendor_name for txn in sources.transactions()}

    if args.command == "preflight":
        results = run_preflight(sources, out, **common)
        for result in results:
            print(
                f"{result.status.value.upper():<15}{result.check.value:<14}{vendors[result.transaction_id]}"
            )
            print(f"{'':<15}{result.summary}")
        done = f"{len(results)} results"
    else:
        request = SampleRequest.model_validate_json(Path(args.request).read_text(encoding="utf-8"))
        packets, drafts = run_sample(sources, request, out, **common)
        by_id = {draft.id: draft for draft in drafts}
        for packet in packets:
            name = vendors.get(packet.transaction_id, "not found")
            print(f"Line {packet.sample_line}: {name}, {len(packet.items)} items lined up")
            for missing in packet.missing:
                print(f"    missing: {missing}")
            for draft_id in packet.draft_ids:
                print(f"    {by_id[draft_id].channel} draft to {by_id[draft_id].to}")
        done = f"{len(packets)} packets and {len(drafts)} drafts"

    problems = validate_run(out, sources)
    for problem in problems:
        print(f"PROBLEM  {problem}", file=sys.stderr)
    print(f"\n{done} in {out}" + ("" if problems else " (run folder valid)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
