from pathlib import Path

from auditorium_agent.evidence import record_evidence
from auditorium_agent.preflight import run_preflight
from auditorium_contracts import LocalSources, validate_run

SAMPLE = Path(__file__).parents[1] / "examples" / "sample"


def test_sample_results(tmp_path):
    sources = LocalSources(SAMPLE)
    manifest = sources.dataset.manifest
    results = run_preflight(
        sources,
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
    )
    found = {
        r.transaction_id.split(":")[-1]: (r.status.value, [c.value for c in r.issue_codes])
        for r in results
        if r.check.value == "approval"
    }
    assert {key: value[1] for key, value in found.items() if value[0] == "fail"} == {
        "b_1009": ["approval_missing"],  # over the threshold, nobody approved
        "b_1010": ["approval_wrong_approver"],  # a manager where the CFO is required
        "b_1011": ["approval_after_the_fact"],  # approved four days after payment
        "c_2006": ["approval_wrong_approver"],  # approved their own charge
    }
    assert {key for key, value in found.items() if value[0] == "pass"} == {
        "b_1001",
        "b_1008",
        "c_2001",
        "c_2002",
    }
    # Everything else is under the thresholds.
    assert {value[0] for value in found.values()} == {"fail", "pass", "not_applicable"}
    assert len(found) == len(sources.transactions())
    assert validate_run(tmp_path / "run", sources) == []


def test_record_evidence_reads_the_value_from_the_record():
    txn = next(t for t in LocalSources(SAMPLE).transactions() if t.id.endswith("b_1011"))
    assert record_evidence("transaction", txn, "approvals.0.approver_id", "").value == "p_cfo"
    assert record_evidence("transaction", txn, "paid_date", "").value == "2025-03-10"
    assert record_evidence("transaction", txn, "amount_cents", "").value == "900000"


def test_a_refund_is_not_held_to_the_approval_or_receipt_rules(tmp_path):
    sources = LocalSources(SAMPLE)
    manifest = sources.dataset.manifest
    results = run_preflight(
        sources,
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
    )
    by_check = {r.check.value: r for r in results if r.transaction_id.endswith("c_2010")}
    # $650 back from an airline: over both thresholds as a purchase, but it is not one.
    assert by_check["approval"].status.value == "not_applicable"
    assert by_check["receipt"].status.value == "not_applicable"
    assert "refund" in by_check["approval"].summary
    # The books still have to record it.
    assert by_check["ledger_match"].status.value == "pass"
