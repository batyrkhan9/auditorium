"""Regression coverage for the results table's sign-off and evidence totals."""

import json
import re

import auditorium_contracts as c
import pytest
from auditorium_ui.app import Verification, Viewer
from auditorium_ui.demo import build_demo
from starlette.testclient import TestClient


@pytest.fixture
def viewer(tmp_path):
    dataset, preflight, _ = build_demo(tmp_path)
    return Viewer(preflight, dataset, demo=True)


def transaction_row(page, transaction_id):
    match = re.search(rf'<tr data-tx="{re.escape(transaction_id)}".*?</tr>', page, re.S)
    assert match is not None
    return match.group(0)


def test_latest_unpinned_signoff_replaces_previously_counted_signoff(viewer):
    run = c.load_run(viewer.run_dir)
    signed = next(
        subject
        for event in run.log
        if event.action is c.LogAction.SIGNOFF and event.detail.get("result_sha256")
        for subject in event.subject_ids
    )
    c.LogWriter(viewer.run_dir / c.LOG, run.manifest.run_id).sign_off(
        "Legacy reviewer", signed, "accepted"
    )
    page = TestClient(viewer.app()).get("/").text
    row = transaction_row(page, signed.split("#")[0])
    signed_cell = re.search(r'<td class="signed">(.*?)</td>', row, re.S).group(1)
    assert "0 / 4" in signed_cell
    assert "1 unpinned" in signed_cell
    assert "<b>0 / 20</b>" in page
    assert "1 unpinned: no version recorded, so not counted." in page


def test_mixed_verified_and_unchecked_counts_are_rendered_separately(viewer, monkeypatch):
    run = c.load_run(viewer.run_dir)
    transaction_id = run.results[0].transaction_id
    total = sum(len(r.evidence) for r in run.results if r.transaction_id == transaction_id)
    assert total > 1
    calls = 0

    def verify(evidence, basis):
        nonlocal calls
        calls += 1
        return (
            Verification("verified", basis=basis.kind) if calls == 1 else Verification("unchecked")
        )

    monkeypatch.setattr(viewer, "verify", verify)
    row = transaction_row(TestClient(viewer.app()).get("/").text, transaction_id)
    assert 'data-ev="verified" title="1 verified against snapshot"' in row
    assert f'data-ev="unchecked">{total - 1} not checked' in row
    assert f">{total} not checked" not in row


def test_transaction_with_no_evidence_never_shows_zero_not_checked(viewer):
    run = c.load_run(viewer.run_dir)
    transaction_id = run.results[0].transaction_id
    records = [r.model_dump(mode="json") for r in run.results]
    missing = 0
    for record in records:
        if record["transaction_id"] == transaction_id:
            record["evidence"] = []
            missing += 1
    (viewer.run_dir / c.RESULTS).write_text("".join(json.dumps(r) + "\n" for r in records))
    page = TestClient(viewer.app()).get("/").text
    row = transaction_row(page, transaction_id)
    assert f'data-no-evidence="{missing}"' in row
    assert f"{missing} with no evidence" in row
    assert "0 not checked" not in row
    assert "data-ev=" not in row
    assert f'data-no-evidence-total="{missing}"' in page
