"""Score the agent against the sealed answer key.

    python -m auditorium_evals run --dataset data/company [--out RUN_DIR]
                                   [--ramp dataset|live] [--qbo live|dataset]
    python -m auditorium_evals score RUN_DIR [--key expected.jsonl] [--sources DATASET]

`run` copies the public dataset outside the repo, runs the agent there, keeps the
run folder (default sealed/eval/runs/<run_id>) and scores it. `score` scores an
existing run folder. Aggregate scores go to evals/scorecards/, everything per
transaction to sealed/eval/scores/<run_id>/. Only aggregates are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from auditorium_contracts import LocalSources

from .harness import run_agent, score_run


def _print(scored) -> None:
    card = scored.scorecard
    print(f"run {card.run_id}  dataset {card.dataset_id}")
    print(f"evidence: {card.evidence_total} pieces, {card.evidence_unverified} unverified")
    if card.evidence_unverified:
        print("!! FABRICATED OR UNRESOLVABLE EVIDENCE: see sealed/eval/scores/<run_id>/")
    print(f"{'check':<14}{'tp':>5}{'fp':>5}{'fn':>5}{'tn':>5}{'review':>8}")
    for check, s in card.per_check.items():
        print(
            f"{check.value:<14}{s.true_positive:>5}{s.false_positive:>5}"
            f"{s.false_negative:>5}{s.true_negative:>5}{s.needs_review:>8}"
        )
    print(json.dumps(scored.metrics["all_checks"], indent=2))
    print(card.notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auditorium_evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the agent blind on a dataset, then score it")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--out", type=Path, help="where to keep the run folder")
    run.add_argument("--ramp", choices=("dataset", "live"), default="dataset")
    run.add_argument("--qbo", choices=("live", "dataset"), default="live")
    score = commands.add_parser("score", help="score an existing run folder")
    score.add_argument("run_dir", type=Path)
    score.add_argument("--key", type=Path, help="answer key (default: sealed/answer_key/...)")
    score.add_argument(
        "--sources", type=Path, help="dataset folder to verify against instead of the snapshot"
    )
    args = parser.parse_args(argv)

    if args.command == "run":
        run_dir = run_agent(args.dataset, args.out, ramp=args.ramp, qbo=args.qbo)
        scored = score_run(run_dir)
    else:
        sources = LocalSources(args.sources) if args.sources else None
        scored = score_run(args.run_dir, key=args.key, sources=sources)
    _print(scored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
