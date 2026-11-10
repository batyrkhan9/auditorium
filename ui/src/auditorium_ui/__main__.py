"""uv run python -m auditorium_ui <run_dir> [--dataset <dir>] [--port 8000]

Or, before a real run exists: uv run python -m auditorium_ui --demo [preflight|sample]
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn

from .app import create_app
from .demo import build_demo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m auditorium_ui", description="Look at one Auditorium run folder."
    )
    parser.add_argument("run_dir", nargs="?", type=Path, help="The run folder (has run.json).")
    parser.add_argument(
        "--dataset", type=Path, help="The dataset folder, to check evidence and open documents."
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--demo",
        nargs="?",
        const="preflight",
        choices=["preflight", "sample"],
        help="Build synthetic demo data in a temporary folder and show that run instead.",
    )
    args = parser.parse_args(argv)

    if args.demo:
        if args.run_dir or args.dataset:
            parser.error("--demo builds its own run and dataset; leave out run_dir and --dataset")
        root = Path(tempfile.mkdtemp(prefix="auditorium-demo-"))
        dataset, preflight, sample = build_demo(root)
        run_dir = preflight if args.demo == "preflight" else sample
        print(f"Demo data written to {root}")
    elif args.run_dir:
        run_dir, dataset = args.run_dir, args.dataset
        if not (run_dir / "run.json").is_file():
            parser.error(f"{run_dir} has no run.json")
    else:
        parser.error("give a run folder, or --demo")

    app = create_app(run_dir, dataset, demo=bool(args.demo))
    print(f"Run viewer for {run_dir} on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
