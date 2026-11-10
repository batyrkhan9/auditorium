"""Contract 0.2.0: the changes that came out of mapping real Ramp and QuickBooks payloads."""

import threading
from datetime import UTC, datetime

import auditorium_contracts as c
import pytest
from pydantic import ValidationError


def test_two_writers_share_one_chain(tmp_path):
    # The agent and the screen each hold their own writer on the same log.
    agent, screen = (
        c.LogWriter(tmp_path / "log.jsonl", "r"),
        c.LogWriter(tmp_path / "log.jsonl", "r"),
    )
    agent.append("system", c.LogAction.RUN_STARTED)
    screen.sign_off_run("Dana Reyes", "accepted")
    agent.append("agent", c.LogAction.NOTE)
    screen.sign_off("Dana Reyes", "t#period", "disputed", "see invoice")
    assert c.verify_log(tmp_path / "log.jsonl") == []
    assert [event.seq for event in c.read_log(tmp_path / "log.jsonl")] == [1, 2, 3, 4]


def test_concurrent_appends_do_not_fork_the_chain(tmp_path):
    path = tmp_path / "log.jsonl"

    def write(name):
        writer = c.LogWriter(path, "r")
        for _ in range(25):
            writer.append("agent", c.LogAction.NOTE, actor_id=name, detail={"pad": "x" * 300})

    threads = [threading.Thread(target=write, args=(f"w{n}",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert c.verify_log(path) == []
    assert len(c.read_log(path)) == 200


def test_run_sign_off_is_marked_as_such(tmp_path):
    event = c.LogWriter(tmp_path / "log.jsonl", "run_9").sign_off_run("Dana Reyes", "accepted")
    assert event.subject_ids == ["run_9"] and event.detail["scope"] == "run"


def test_pending_approval_has_no_decision_time():
    assert c.Approval(approver_id="p_cfo", decision="pending").decided_at is None
    with pytest.raises(ValidationError):
        c.Approval(approver_id="p_cfo", decision="approved")
    decided = datetime(2026, 1, 9, tzinfo=UTC)
    assert c.Approval(approver_id="p_cfo", decision="rejected", decided_at=decided)


def test_default_origin():
    assert c.default_origin("invoice") == c.default_origin("bank_statement") == "external"
    assert c.default_origin(c.DocumentType.POLICY) == "internal"


def test_new_fields_are_optional():
    # Records written under 0.1.0 still load.
    line = c.LedgerLine(account="Accrued Liabilities", side="credit", amount_cents=100)
    assert line.account_type is None and line.vendor_name is None
    assert c.Person(id="p", name="Pat").platform_role is None


# --- Review round 2 ---


def started_log(tmp_path):
    path = tmp_path / "log.jsonl"
    writer = c.LogWriter(path, "r")
    writer.append("system", c.LogAction.RUN_STARTED)
    writer.append("agent", c.LogAction.NOTE)
    return path, writer


def test_a_cut_off_last_line_stops_appends_until_repaired(tmp_path):
    path, writer = started_log(tmp_path)
    with path.open("ab") as handle:  # a crash halfway through writing an event
        handle.write(b'{"seq": 3, "ts": "2026-01-01T00:00:00Z", "run_id": "r", "act')
    with pytest.raises(c.LogDamaged, match="line 3.*repair-log"):
        writer.append("agent", c.LogAction.NOTE)
    assert len(path.read_bytes().splitlines()) == 3  # nothing was appended to the damage

    assert "Removed 1 damaged line" in c.repair_log(path)
    assert (tmp_path / "log.jsonl.damaged").read_bytes().startswith(b'{"seq": 3')
    assert writer.append("agent", c.LogAction.NOTE).seq == 3
    assert c.verify_log(path) == []


def test_a_last_event_without_its_line_ending_is_not_merged_with_the_next(tmp_path):
    path, writer = started_log(tmp_path)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(c.LogDamaged):
        writer.append("agent", c.LogAction.NOTE)
    assert "Added it" in c.repair_log(path)
    writer.append("agent", c.LogAction.NOTE)
    assert c.verify_log(path) == [] and len(c.read_log(path)) == 3
    assert c.repair_log(path) == "The log ends cleanly. Nothing to repair."


def a_result(**extra):
    return dict(
        id="t#period",
        run_id="r",
        transaction_id="t",
        check="period",
        status="pass",
        summary="s",
        rationale="r",
        evidence=[c.SearchEvidence(system="qbo", query="q")],
        **extra,
    )


def test_result_hash_survives_a_round_trip_and_a_new_optional_field(tmp_path):
    built = c.CheckResult(**a_result(recorded_period=None))
    c.write_jsonl(tmp_path / "results.jsonl", [built])
    reloaded = c.read_jsonl(tmp_path / "results.jsonl", c.CheckResult)[0]
    assert c.result_hash(built) == c.result_hash(reloaded)

    class NextContractResult(c.CheckResult):  # the contract gains an optional field later
        reviewer_note: str | None = None
        tags: list[str] = []

    assert c.result_hash(NextContractResult(**a_result())) == c.result_hash(built)
    changed = c.CheckResult(**(a_result() | {"summary": "different"}))
    assert c.result_hash(changed) != c.result_hash(built)


def test_unknown_attachments_are_internal_evidence():
    assert c.default_origin("other") == "internal"
    assert c.default_origin("contract") == "external"


def test_pending_approval_rules():
    assert c.Approval(decision="pending").approver_id is None  # assigned to a group
    with pytest.raises(ValidationError):
        c.Approval(approver_id="p", decision="pending", decided_at=datetime(2026, 1, 9, tzinfo=UTC))
    with pytest.raises(ValidationError):
        c.Approval(decision="approved", decided_at=datetime(2026, 1, 9, tzinfo=UTC))


def test_synthetic_is_set_per_source_and_read_from_the_run():
    live = c.SourceUse(system="ramp", mode="live", detail="demo account", synthetic=True)
    real = c.SourceUse(system="qbo", mode="live", detail="production")
    manifest = dict(
        run_id="r",
        mode="preflight",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        period_start="2025-01-01",
        period_end="2025-12-31",
        contract_version=c.CONTRACT_VERSION,
    )
    assert c.RunManifest(**manifest, sources=[live, real]).synthetic_data is True
    assert c.RunManifest(**manifest, sources=[real]).synthetic_data is False
