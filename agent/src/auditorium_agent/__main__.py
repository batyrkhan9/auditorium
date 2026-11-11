"""Run the agent.

python -m auditorium_agent preflight DATASET_DIR [--out RUN_DIR]
python -m auditorium_agent sample DATASET_DIR --request SAMPLE.json [--out RUN_DIR]

Add --live to read Ramp and QuickBooks through the connectors where credentials are
set; the dataset folder then supplies whatever is not live. Add --claude to let Claude read the documents plain code cannot settle. It needs
credentials in the environment: uv run --env-file .env python -m auditorium_agent ...
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from auditorium_contracts import LocalSources, SampleRequest, load_run, validate_run

from .preflight import run_preflight
from .reader import DEFAULT_MAX_CALLS, ClaudeReader
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
        command.add_argument(
            "--claude", action="store_true", help="ask Claude where plain code cannot settle it"
        )
        command.add_argument(
            "--live",
            action="store_true",
            help="read Ramp and QuickBooks live where credentials are set, the dataset otherwise",
        )
        command.add_argument(
            "--max-claude-calls",
            type=int,
            default=DEFAULT_MAX_CALLS,
            help=f"stop asking Claude after this many requests (default {DEFAULT_MAX_CALLS})",
        )
    args = parser.parse_args()

    reader = ClaudeReader(max_calls=args.max_claude_calls) if args.claude else None
    if reader and (problem := reader.problem()):
        print(f"Cannot use Claude: {problem}.", file=sys.stderr)
        print("Run with: uv run --env-file .env python -m auditorium_agent ...", file=sys.stderr)
        return 2

    manifest = LocalSources(args.dataset).dataset.manifest
    if args.live:
        from auditorium_connectors import connect_sources

        sources = connect_sources(args.dataset)
    else:
        sources = LocalSources(args.dataset)
    for use in sources.describe():
        print(f"source {use.system.value}: {use.mode} ({use.detail})")
    run_id = f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    out = Path(args.out) if args.out else Path("runs") / run_id
    common = dict(
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
        run_id=run_id,
        dataset_id=manifest.dataset_id,
        dataset_path=args.dataset,
        dataset_manifest=manifest,
        reader=reader,
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

    used = load_run(out).manifest.models
    if args.claude:
        print(f"\nClaude requests made: {reader.calls} of at most {reader.max_calls}.", end=" ")
        print(f"Models that answered: {', '.join(used) or 'none'}")
    problems = validate_run(out, sources)
    for problem in problems:
        print(f"PROBLEM  {problem}", file=sys.stderr)
    print(f"\n{done} in {out}" + ("" if problems else " (run folder valid)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
