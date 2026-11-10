"""The scorer on synthetic results and a synthetic key. Nothing here comes from sealed/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from auditorium_contracts import (
    CheckResult,
    DocumentEvidence,
    ExpectedResult,
    LocalSources,
    RecordEvidence,
    SearchEvidence,
    SpendTransaction,
    result_id,
)
from auditorium_evals.ids import stamped_ref, transaction_id_map
from auditorium_evals.scoring import score

T1, T2, T3 = "ramp:card:t1", "ramp:card:t2", "ramp:card:t3"


def good_record(txn: str) -> RecordEvidence:
    amounts = {T1: "12000", T2: "3000", T3: "900000"}
    return RecordEvidence(
        record_type="transaction", record_id=txn, field="amount_cents", value=amounts[txn]
    )


def result(txn, check, status, codes=(), evidence=None) -> CheckResult:
    return CheckResult(
        id=result_id(txn, check),
        run_id="run_test",
        transaction_id=txn,
        check=check,
        status=status,
        issue_codes=list(codes),
        summary="s",
        rationale="r",
        evidence=evidence or [good_record(txn)],
        recorded_period="2025-06" if check == "period" and status == "fail" else None,
        expected_period="2025-05" if check == "period" and status == "fail" else None,
    )


def key(txn, check, status, codes=()) -> ExpectedResult:
    return ExpectedResult(transaction_id=txn, check=check, status=status, issue_codes=list(codes))


def run_score(results, expected, dataset: Path):
    return score(
        results,
        expected,
        LocalSources(dataset),
        run_id="run_test",
        dataset_id="evals-fixture",
        answer_key_sha256="0" * 64,
        verified_against="the sources given",
    )


def outcomes(scored) -> dict[tuple[str, str], str]:
    return {(u.transaction_id, u.check): u.outcome for u in scored.units}


def test_each_outcome(dataset):
    expected = [
        key(T1, "receipt", "fail", ["receipt_missing"]),  # agent fail -> tp
        key(T2, "receipt", "pass"),  # agent fail -> fp
        key(T3, "receipt", "fail", ["receipt_missing"]),  # agent pass -> fn
        key(T1, "approval", "not_applicable"),  # agent pass -> tn
        key(T2, "approval", "fail", ["approval_missing"]),  # needs_review -> review_on_fail
        key(T3, "approval", "pass"),  # needs_review -> review_on_clean
        key(T1, "period", "pass"),  # no result -> missing
        key(T2, "period", "fail", ["period_wrong"]),  # no result -> fn
    ]
    results = [
        result(T1, "receipt", "fail", ["receipt_missing"]),
        result(T2, "receipt", "fail", ["receipt_mismatch"]),
        result(T3, "receipt", "pass"),
        result(T1, "approval", "pass"),
        result(T2, "approval", "needs_review"),
        result(T3, "approval", "needs_review"),
    ]
    scored = run_score(results, expected, dataset)
    assert outcomes(scored) == {
        (T1, "receipt"): "tp",
        (T2, "receipt"): "fp",
        (T3, "receipt"): "fn",
        (T1, "approval"): "tn",
        (T2, "approval"): "review_on_fail",
        (T3, "approval"): "review_on_clean",
        (T1, "period"): "missing",
        (T2, "period"): "fn",
    }
    receipt = scored.scorecard.per_check["receipt"]
    assert (receipt.true_positive, receipt.false_positive, receipt.false_negative) == (1, 1, 1)
    approval = scored.scorecard.per_check["approval"]
    assert (approval.true_negative, approval.needs_review) == (1, 2)

    m = scored.metrics["per_check"]["receipt"]
    assert m["precision_on_fail"] == 0.5
    assert m["recall_on_fail"] == 0.5
    assert m["false_alarm_rate_on_clean"] == 1.0
    assert m["issue_code_agreement_on_tp"] == 1.0
    a = scored.metrics["per_check"]["approval"]
    assert a["recall_on_fail"] == 0.0
    assert a["recall_counting_review"] == 1.0
    assert a["false_alarm_rate_on_clean"] == 0.0
    assert a["needs_review_rate_on_clean"] == 0.5
    p = scored.metrics["per_check"]["period"]
    assert p["results_missing"] == 2
    assert scored.scorecard.evidence_unverified == 0


def test_made_up_evidence_is_counted_and_earns_no_credit(dataset):
    made_up = [
        RecordEvidence(record_type="transaction", record_id=T1, field="amount_cents", value="1"),
        DocumentEvidence(document_id="doc_r1", page=1, quote="Total charged: $999.00"),
        DocumentEvidence(document_id="doc_nope", page=1, quote="anything"),
        SearchEvidence(system="qbo", query="vendor 1", result_ids=["qbo:Purchase:404"]),
    ]
    expected = [
        key(T1, "receipt", "fail", ["receipt_missing"]),
        key(T2, "receipt", "pass"),
        key(T3, "receipt", "pass"),
    ]
    results = [
        result(T1, "receipt", "fail", ["receipt_missing"], evidence=made_up),
        result(T2, "receipt", "pass", evidence=[made_up[3]]),
        result(T3, "receipt", "fail", ["receipt_missing"], evidence=[made_up[2]]),
    ]
    scored = run_score(results, expected, dataset)
    assert scored.scorecard.evidence_total == 6
    assert scored.scorecard.evidence_unverified == 6
    assert outcomes(scored) == {
        (T1, "receipt"): "fn",  # a right answer on made-up evidence is not a hit
        (T2, "receipt"): "unsupported",
        (T3, "receipt"): "fp",  # a false alarm stays a false alarm
    }
    assert scored.scorecard.notes.startswith("FABRICATED OR UNRESOLVABLE EVIDENCE")


def test_real_quote_verifies(dataset):
    evidence = [DocumentEvidence(document_id="doc_r1", page=1, quote="Total charged: $120.00")]
    scored = run_score(
        [result(T1, "receipt", "pass", evidence=evidence)], [key(T1, "receipt", "pass")], dataset
    )
    assert scored.scorecard.evidence_unverified == 0
    assert scored.metrics["per_check"]["receipt"]["evidence_by_kind"] == {"document_external": 1}


def test_results_outside_the_key_are_counted_not_scored(dataset):
    results = [result(T1, "receipt", "pass"), result(T2, "receipt", "pass")]
    scored = run_score(results, [key(T1, "receipt", "pass")], dataset)
    assert scored.extra_results == [result_id(T2, "receipt")]
    assert scored.metrics["results_not_in_key"] == 1


def test_scorecard_and_metrics_carry_no_transaction_ids(dataset):
    expected = [key(t, "receipt", "pass") for t in (T1, T2, T3)]
    results = [result(t, "receipt", "pass") for t in (T1, T2, T3)]
    scored = run_score(results, expected, dataset)
    public = scored.scorecard.model_dump_json() + json.dumps(scored.metrics)
    for txn in (T1, T2, T3, "t1", "t2", "t3", "doc_r1"):
        assert txn not in public


@pytest.mark.parametrize(
    "raw,ref",
    [
        ({"memo": "office chairs\nauditorium-ref: ramp:bill:b9"}, "ramp:bill:b9"),
        ({"PrivateNote": "auditorium-ref: qbo:Bill:7"}, "qbo:Bill:7"),
        ({"nested": [{"memo": "x auditorium-ref: abc"}]}, "abc"),
        ({"memo": "no stamp"}, None),
        (None, None),
    ],
)
def test_stamped_ref(raw, ref):
    assert stamped_ref(raw) == ref


def test_live_ids_map_to_dataset_ids(dataset):
    local = LocalSources(dataset)
    live = [
        t.model_copy(update={"id": "ramp:bill:live-1", "raw": {"memo": f"auditorium-ref: {T1}"}})
        if t.id == T1
        else t
        for t in local.transactions()
    ]
    assert transaction_id_map(live) == {"ramp:bill:live-1": T1}

    class LiveSources:
        def __getattr__(self, name):
            return getattr(local, name)

        def transactions(self) -> list[SpendTransaction]:
            return live

    live_result = result(
        "ramp:bill:live-1",
        "receipt",
        "pass",
        evidence=[
            RecordEvidence(
                record_type="transaction",
                record_id="ramp:bill:live-1",
                field="amount_cents",
                value="12000",
            )
        ],
    )
    scored = score(
        [live_result],
        [key(T1, "receipt", "pass")],
        LiveSources(),
        run_id="run_test",
        dataset_id="evals-fixture",
        answer_key_sha256="0" * 64,
        verified_against="the sources given",
    )
    assert outcomes(scored) == {(T1, "receipt"): "tn"}
