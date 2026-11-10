"""Run the agent blind on the public dataset, then score the run.

Where things go:

    evals/scorecards/<run_id>.json           Scorecard (contract), aggregate only, committed
    evals/scorecards/<run_id>.metrics.json   derived aggregate metrics, committed
    sealed/eval/runs/<run_id>/               the run folder (sources/ snapshot not in git)
    sealed/eval/scores/<run_id>/             per-transaction scoring, encrypted

Braintrust: used only when BRAINTRUST_API_KEY is set and the `braintrust` package is
installed. Otherwise the records it would get are written to
sealed/eval/scores/<run_id>/braintrust_records.jsonl and the scorecard says so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from auditorium_contracts import (
    SCORECARD,
    SOURCES_SNAPSHOT,
    LocalSources,
    Sources,
    load_run,
    validate_run,
    write_json,
)

from .scoring import Scored, load_key, score, unit_rows


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "ownership.json").is_file():
            return parent
    raise RuntimeError("cannot find the repo root (no ownership.json above this file)")


def key_path(root: Path, dataset_id: str) -> Path:
    return root / "sealed" / "answer_key" / dataset_id / "expected.jsonl"


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def run_agent(
    dataset: Path,
    out: Path | None = None,
    *,
    ramp: str = "dataset",
    qbo: str = "live",
    root: Path | None = None,
) -> Path:
    """Copy the public dataset outside the repo, run the agent there, keep the run folder."""
    root = root or repo_root()
    dataset = dataset.resolve()
    if _inside(dataset, root / "sealed"):
        raise ValueError("the agent runs on the public dataset only, never on anything in sealed/")
    work = Path(tempfile.mkdtemp(prefix="auditorium-eval-"))
    if _inside(work, root):
        raise RuntimeError(f"temp folder {work} is inside the repo; set TMPDIR elsewhere")
    copy = work / "dataset"
    shutil.copytree(dataset, copy)
    run_dir = work / "run"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "auditorium_evals.agent_entry",
            str(copy),
            str(run_dir),
            "--ramp",
            ramp,
            "--qbo",
            qbo,
        ],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"the agent run failed:\n{proc.stderr[-4000:]}")
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    run_id = report["run_id"]
    final = out or root / "sealed" / "eval" / "runs" / run_id
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(run_dir, final)
    (final / "validate_run_against_sources.json").write_text(
        json.dumps(report["validate_run_against_sources"], indent=2) + "\n", "utf-8"
    )
    shutil.rmtree(work, ignore_errors=True)
    return final


def _sources_used(run_dir: Path) -> list[dict]:
    manifest = load_run(run_dir).manifest
    return [
        {"system": s.system.value, "mode": s.mode, "synthetic": s.synthetic}
        for s in manifest.sources
    ]


def score_run(
    run_dir: Path,
    *,
    key: Path | None = None,
    sources: Sources | None = None,
    root: Path | None = None,
    scorecards_dir: Path | None = None,
    sealed_dir: Path | None = None,
) -> Scored:
    root = root or repo_root()
    run = load_run(run_dir)
    dataset_id = run.manifest.dataset_id
    if not dataset_id:
        raise ValueError("the run does not name its dataset, so no answer key applies")
    expected, key_sha = load_key(key or key_path(root, dataset_id))

    if sources is not None:
        against = "the sources given"
    elif (run_dir / SOURCES_SNAPSHOT).is_dir():
        sources = LocalSources(run_dir / SOURCES_SNAPSHOT)
        against = "snapshot"
    else:
        raise ValueError("the run folder has no sources/ snapshot; pass the sources it ran on")

    structural = validate_run(run_dir, sources)
    used = _sources_used(run_dir)
    live = sorted(u["system"] for u in used if u["mode"] == "live")
    local = sorted(u["system"] for u in used if u["mode"] == "local")
    braintrust = braintrust_available()
    at_run = run_dir / "validate_run_against_sources.json"
    run_time = ""
    if at_run.is_file():
        # Written by `run`: validate_run against the very Sources the agent used (live
        # systems included), before anything moved on.
        problems = json.loads(at_run.read_text("utf-8"))
        evidence_problems = sum(1 for p in problems if p.startswith(("result ", "packet ")))
        run_time = (
            f"At run time, evidence problems against the run's own Sources: {evidence_problems}. "
        )
    notes = (
        f"Sources: live {', '.join(live) or 'none'}; local {', '.join(local) or 'none'}. "
        + run_time
        + f"validate_run problems at scoring: {len(structural)}. "
        + ("Logged to Braintrust." if braintrust else "Braintrust not used (scored locally).")
    )
    scored = score(
        list(run.results),
        expected,
        sources,
        run_id=run.manifest.run_id,
        dataset_id=dataset_id,
        answer_key_sha256=key_sha,
        verified_against=against,
        notes=notes,
    )
    scored.metrics["sources"] = used
    scored.metrics["validate_run_problems"] = len(structural)
    scored.metrics["braintrust"] = "used" if braintrust else "not used"

    run_id = run.manifest.run_id
    scorecards_dir = scorecards_dir or root / "evals" / "scorecards"
    sealed_dir = sealed_dir or root / "sealed" / "eval" / "scores" / run_id
    scorecards_dir.mkdir(parents=True, exist_ok=True)
    sealed_dir.mkdir(parents=True, exist_ok=True)
    write_json(scorecards_dir / f"{run_id}.json", scored.scorecard)
    write_json(run_dir / SCORECARD, scored.scorecard)
    (scorecards_dir / f"{run_id}.metrics.json").write_text(
        json.dumps(scored.metrics, indent=2, sort_keys=True) + "\n", "utf-8"
    )
    _write_sealed(sealed_dir, scored, structural)
    if braintrust:
        _log_braintrust(scored)
    return scored


def _write_sealed(folder: Path, scored: Scored, structural: list[str]) -> None:
    def jsonl(name: str, rows: list[dict]) -> None:
        with (folder / name).open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True) + "\n")

    jsonl("units.jsonl", unit_rows(scored.units))
    jsonl("unverified_evidence.jsonl", scored.unverified_details)
    jsonl("braintrust_records.jsonl", braintrust_records(scored))
    (folder / "validate_run.json").write_text(json.dumps(structural, indent=2) + "\n", "utf-8")
    (folder / "results_not_in_key.json").write_text(
        json.dumps(scored.extra_results, indent=2) + "\n", "utf-8"
    )


def braintrust_records(scored: Scored) -> list[dict]:
    """One Braintrust eval record per (transaction, check): input, output, expected, scores."""
    rows = []
    for u in scored.units:
        rows.append(
            {
                "input": {"transaction_id": u.transaction_id, "check": u.check},
                "output": {"status": u.actual_status, "issue_codes": u.actual_issue_codes},
                "expected": {"status": u.expected_status, "issue_codes": u.expected_issue_codes},
                "scores": {
                    "status_correct": float(
                        u.actual_status == u.expected_status and not u.unverified
                    ),
                    "no_false_alarm": float(u.outcome != "fp"),
                    "evidence_verified": float(not u.unverified),
                },
                "metadata": {"outcome": u.outcome, "run_id": scored.scorecard.run_id},
            }
        )
    return rows


def braintrust_available() -> bool:
    if not os.environ.get("BRAINTRUST_API_KEY", "").strip():
        return False
    try:
        import braintrust  # noqa: F401
    except ImportError:
        return False
    return True


def _log_braintrust(scored: Scored) -> None:  # pragma: no cover - needs a Braintrust account
    import braintrust

    experiment = braintrust.init(project="auditorium", experiment=scored.scorecard.run_id)
    for row in braintrust_records(scored):
        experiment.log(**row)
    experiment.flush()
