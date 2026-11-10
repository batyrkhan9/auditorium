"""The harness end to end on the synthetic fixture: the agent runs in a child process
outside the repo, the run is scored, and only aggregates land outside sealed/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from auditorium_contracts import CheckName, CheckStatus, ExpectedResult, load_run, write_jsonl
from auditorium_evals.__main__ import main
from auditorium_evals.harness import run_agent, score_run


def fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "sealed").mkdir(parents=True)
    (root / "ownership.json").write_text("{}", "utf-8")
    return root


def all_pass_key(run_dir: Path, path: Path) -> Path:
    run = load_run(run_dir)
    write_jsonl(
        path,
        [
            ExpectedResult(transaction_id=t, check=c, status=CheckStatus.PASS)
            for t in sorted({r.transaction_id for r in run.results})
            for c in CheckName
        ],
    )
    return path


def test_refuses_a_dataset_inside_sealed(tmp_path):
    root = fake_root(tmp_path)
    with pytest.raises(ValueError, match="public dataset only"):
        run_agent(root / "sealed" / "anything", root=root, qbo="dataset")


def test_run_and_score(tmp_path, dataset):
    root = fake_root(tmp_path)
    out = tmp_path / "kept-run"
    run_dir = run_agent(dataset, out, ramp="dataset", qbo="dataset", root=root)
    assert run_dir == out
    run = load_run(run_dir)
    assert run.results, "the agent produced no results"
    assert all(s.mode == "local" for s in run.manifest.sources)
    assert (run_dir / "sources" / "transactions.jsonl").is_file()

    keyfile = all_pass_key(run_dir, tmp_path / "expected.jsonl")
    scorecards = tmp_path / "scorecards"
    sealed = tmp_path / "sealed-scores"
    scored = score_run(
        run_dir, key=keyfile, root=root, scorecards_dir=scorecards, sealed_dir=sealed
    )
    assert scored.scorecard.evidence_unverified == 0
    assert "verified against snapshot" in scored.scorecard.notes
    assert "Braintrust" in scored.scorecard.notes
    assert "evidence problems against the run's own Sources: 0" in scored.scorecard.notes

    public = (scorecards / f"{scored.scorecard.run_id}.json").read_text() + (
        scorecards / f"{scored.scorecard.run_id}.metrics.json"
    ).read_text()
    for txn in {r.transaction_id for r in run.results}:
        assert txn not in public
    units = [json.loads(line) for line in (sealed / "units.jsonl").read_text().splitlines()]
    assert len(units) == len({r.transaction_id for r in run.results}) * len(CheckName)
    assert (sealed / "braintrust_records.jsonl").is_file()
    assert (run_dir / "scorecard.json").is_file()


def test_cli_score(tmp_path, dataset, capsys, monkeypatch):
    root = fake_root(tmp_path)
    run_dir = run_agent(dataset, tmp_path / "run", qbo="dataset", root=root)
    keyfile = all_pass_key(run_dir, tmp_path / "expected.jsonl")
    import auditorium_evals.harness as harness

    monkeypatch.setattr(harness, "repo_root", lambda: root)
    assert main(["score", str(run_dir), "--key", str(keyfile), "--sources", str(dataset)]) == 0
    printed = capsys.readouterr().out
    assert "evidence:" in printed and "receipt" in printed
    assert (root / "evals" / "scorecards").is_dir()
    assert (root / "sealed" / "eval" / "scores").is_dir()
