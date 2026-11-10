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
