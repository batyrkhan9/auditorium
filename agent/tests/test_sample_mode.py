from pathlib import Path

from auditorium_agent.sample import run_sample
from auditorium_contracts import LocalSources, load_run, validate_run

SAMPLE = Path(__file__).parents[1] / "examples" / "sample"


def run(tmp_path):
    sources = LocalSources(SAMPLE)
    manifest = sources.dataset.manifest
    packets, drafts = run_sample(
        sources,
        sources.dataset.samples[0],
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
    )
    return sources, {p.sample_line: p for p in packets}, {d.id: d for d in drafts}


def test_each_line_of_the_request(tmp_path):
    sources, packets, drafts = run(tmp_path)

    def found(line):
        packet = packets[line]
        return (packet.transaction_id or "").split(":")[-1], packet.missing

    def sent_to(line):
        return [(drafts[d].channel, drafts[d].to) for d in packets[line].draft_ids]

    # Complete: invoice, approval, ledger entry and the bank payment.
    assert found(1) == ("b_1001", [])
    assert {item.role for item in packets[1].items} == {
        "receipt_or_invoice",
        "approval",
        "policy",
        "ledger_entry",
        "payment",
    }
    assert "NORTHWIND ADVISORY" in packets[1].items[-1].evidence.quote

    assert found(2) == ("c_2003", ["receipt or invoice"])
    assert sent_to(2) == [("slack", "tom@larkspur.example")]

    assert found(3) == ("b_1009", ["approval"])
    assert sent_to(3) == [("slack", "dana@larkspur.example")]  # the CFO, whom the rule names

    assert found(4)[0] == "b_1008" and "for something else" in found(4)[1][0]
    assert sent_to(4) == [("email", "tom@larkspur.example")]

    # The auditor wrote the ledger's amount; it is still found, through its ledger entry.
    assert found(5) == ("b_1006", [])

    # Paid, but the payment is on no bank statement we hold. Nobody to ask, so no draft.
    assert found(6) == ("b_1002", ["payment on a bank statement"])
    assert sent_to(6) == []

    assert found(7)[0] == "" and "2 transactions match" in found(7)[1][0]
    assert found(8) == ("", ["No transaction matches the details in the request."])

    assert validate_run(tmp_path / "run", sources) == []


def test_nothing_is_sent_and_every_step_is_logged(tmp_path):
    _, packets, drafts = run(tmp_path)
    saved = load_run(tmp_path / "run")
    assert saved.manifest.mode == "sample" and saved.manifest.sample_id == "sample-1"
    assert all(draft.sent is False for draft in saved.drafts)
    actions = [event.action.value for event in saved.log]
    assert actions.count("packet_assembled") == len(packets) == 8
    assert actions.count("draft_created") == len(drafts) == 3
    # Only the transactions the auditor picked were checked.
    assert len(saved.results) == 6 * 4
