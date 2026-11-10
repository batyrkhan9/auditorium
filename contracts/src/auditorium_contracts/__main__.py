"""Command line for the contract.

python -m auditorium_contracts validate-dataset DATASET_DIR
python -m auditorium_contracts validate-run RUN_DIR --dataset DATASET_DIR
python -m auditorium_contracts verify-log LOG_FILE
python -m auditorium_contracts export-schema [--check]
"""

from __future__ import annotations

import argparse
import sys

from . import LocalSources, validate_dataset, validate_run, verify_log
from .schema import export_schemas


def main() -> int:
    parser = argparse.ArgumentParser(prog="auditorium_contracts", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dataset = commands.add_parser("validate-dataset", help="check a dataset folder")
    dataset.add_argument("dataset")
    run = commands.add_parser("validate-run", help="check a run folder and all of its evidence")
    run.add_argument("run")
    run.add_argument("--dataset", required=True)
    log = commands.add_parser("verify-log", help="check a log's hash chain")
    log.add_argument("log")
    schema = commands.add_parser("export-schema", help="write contracts/schema/*.json")
    schema.add_argument("--check", action="store_true", help="fail if the files are out of date")
    args = parser.parse_args()

    if args.command == "validate-dataset":
        problems = validate_dataset(args.dataset)
    elif args.command == "validate-run":
        problems = validate_run(args.run, LocalSources(args.dataset))
    elif args.command == "verify-log":
        problems = verify_log(args.log)
    else:
        problems = export_schemas(check=args.check)

    for problem in problems:
        print(f"PROBLEM  {problem}", file=sys.stderr)
    if not problems:
        print("OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
