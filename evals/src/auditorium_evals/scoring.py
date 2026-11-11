"""Score a run's results against the answer key.

How each number maps to AU-C 500 and how `needs_review` is counted:
evals/decisions/2026-11-10-metrics-follow-au-c-500.md and
evals/decisions/2026-11-10-how-needs-review-is-scored.md.

The unit is one (transaction, check). The key's status is the truth; "clean" means
the key says pass or not_applicable, "fail" means it says fail. Each unit lands in
exactly one outcome:

    tp               agent fail,          key fail, evidence verified
    fp               agent fail,          key clean (a false alarm, verified or not)
    fn               anything else that is not a verified fail or needs_review, key fail
    tn               agent pass / n.a.,   key clean, evidence verified
    review_on_fail   agent needs_review,  key fail, evidence verified
    review_on_clean  agent needs_review,  key clean, evidence verified
    unsupported      key clean, a non-fail result whose evidence did not verify
    missing          key clean, no result at all (a missing result on a key fail is fn)

Evidence that does not verify earns no credit: such a result can still be a false
alarm or a miss, never a tp, tn or review.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from auditorium_contracts import (
    CheckName,
    CheckResult,
    CheckScore,
    CheckStatus,
    DocumentEvidence,
    ExpectedResult,
    RecordEvidence,
    Scorecard,
    SourceIndex,
    Sources,
    read_jsonl,
    sha256_file,
    verify_evidence,
)

from .ids import transaction_id_map

CLEAN = (CheckStatus.PASS, CheckStatus.NOT_APPLICABLE)
OUTCOMES = (
    "tp",
    "fp",
    "fn",
    "tn",
    "review_on_fail",
    "review_on_clean",
    "unsupported",
    "missing",
)


@dataclass
class Unit:
    """One (transaction, check). Per-transaction: goes only to sealed/eval/."""

    transaction_id: str
    check: str
    expected_status: str
    expected_issue_codes: list[str]
    outcome: str
    actual_status: str | None = None
    actual_issue_codes: list[str] = field(default_factory=list)
    run_transaction_id: str | None = None
    issue_codes_agree: bool | None = None
    unverified: list[str] = field(default_factory=list)
    evidence_kinds: list[str] = field(default_factory=list)
    summary: str | None = None


@dataclass
class Scored:
    units: list[Unit]
    scorecard: Scorecard
    metrics: dict
    unverified_details: list[dict]
    extra_results: list[str]


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _evidence_kind(evidence, index: SourceIndex) -> str:
    if isinstance(evidence, DocumentEvidence):
        doc = index.records["document"].get(evidence.document_id)
        origin = getattr(doc, "origin", None) or "unknown"
        return f"document_{origin}"
    if isinstance(evidence, RecordEvidence):
        return "record"
    return "search"


def _outcome(expected: ExpectedResult, result: CheckResult | None, verified: bool) -> str:
    key_fail = expected.status is CheckStatus.FAIL
    if result is None:
        return "fn" if key_fail else "missing"
    status = result.status
    if status is CheckStatus.FAIL:
        if not key_fail:
            return "fp"
        return "tp" if verified else "fn"
    if not verified:
        return "fn" if key_fail else "unsupported"
    if status is CheckStatus.NEEDS_REVIEW:
        return "review_on_fail" if key_fail else "review_on_clean"
    return "fn" if key_fail else "tn"


def check_metrics(counts: Counter, codes_agree: int, status_agree: int, evidence: Counter) -> dict:
    tp, fp, fn, tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
    r_fail, r_clean = counts["review_on_fail"], counts["review_on_clean"]
    key_fail = tp + fn + r_fail
    key_clean = fp + tn + r_clean + counts["unsupported"] + counts["missing"]
    total = key_fail + key_clean
    return {
        "units": total,
        "key_fail": key_fail,
        "key_clean": key_clean,
        "counts": {name: counts[name] for name in OUTCOMES},
        "results_missing": counts["no_result"],
        "precision_on_fail": _ratio(tp, tp + fp),
        "recall_on_fail": _ratio(tp, key_fail),
        "recall_counting_review": _ratio(tp + r_fail, key_fail),
        "false_alarm_rate_on_clean": _ratio(fp, key_clean),
        "needs_review_rate": _ratio(r_fail + r_clean, total),
        "needs_review_rate_on_clean": _ratio(r_clean, key_clean),
        "status_agreement": _ratio(status_agree, total),
        "issue_code_agreement_on_tp": _ratio(codes_agree, tp),
        "evidence_by_kind": dict(sorted(evidence.items())),
    }


def score(
    results: list[CheckResult],
    expected: list[ExpectedResult],
    sources: Sources,
    *,
    run_id: str,
    dataset_id: str,
    answer_key_sha256: str,
    verified_against: str,
    notes: str = "",
) -> Scored:
    index = SourceIndex(sources)
    id_map = transaction_id_map(sources.transactions())

    by_unit: dict[tuple[str, str], CheckResult] = {}
    extra: list[str] = []
    keyed = {(e.transaction_id, e.check.value) for e in expected}
    evidence_total = 0
    evidence_unverified = 0
    unverified_details: list[dict] = []
    problems_by_result: dict[str, list[str]] = {}
    kinds_by_result: dict[str, list[str]] = {}

    # Every result's evidence is verified, including results the key does not know.
    for result in results:
        problems = []
        kinds = []
        for evidence in result.evidence:
            evidence_total += 1
            kinds.append(_evidence_kind(evidence, index))
            problem = verify_evidence(evidence, index)
            if problem:
                evidence_unverified += 1
                problems.append(problem)
                unverified_details.append(
                    {"result_id": result.id, "kind": evidence.kind, "problem": problem}
                )
        problems_by_result[result.id] = problems
        kinds_by_result[result.id] = kinds
        unit = (id_map.get(result.transaction_id, result.transaction_id), result.check.value)
        if unit not in keyed or unit in by_unit:
            extra.append(result.id)
            continue
        by_unit[unit] = result

    units: list[Unit] = []
    counts: dict[str, Counter] = defaultdict(Counter)
    codes_agree: Counter = Counter()
    status_agree: Counter = Counter()
    evidence_mix: dict[str, Counter] = defaultdict(Counter)
    for exp in sorted(expected, key=lambda e: (e.transaction_id, e.check.value)):
        check = exp.check.value
        result = by_unit.get((exp.transaction_id, check))
        problems = problems_by_result.get(result.id, []) if result else []
        outcome = _outcome(exp, result, verified=not problems)
        unit = Unit(
            transaction_id=exp.transaction_id,
            check=check,
            expected_status=exp.status.value,
            expected_issue_codes=[c.value for c in exp.issue_codes],
            outcome=outcome,
        )
        counts[check][outcome] += 1
        if result is None:
            counts[check]["no_result"] += 1
        else:
            unit.actual_status = result.status.value
            unit.actual_issue_codes = [c.value for c in result.issue_codes]
            unit.run_transaction_id = result.transaction_id
            unit.unverified = problems
            unit.evidence_kinds = kinds_by_result[result.id]
            unit.summary = result.summary
            evidence_mix[check].update(unit.evidence_kinds)
            if not problems and result.status is exp.status:
                status_agree[check] += 1
            if outcome == "tp":
                unit.issue_codes_agree = bool(set(result.issue_codes) & set(exp.issue_codes))
                codes_agree[check] += unit.issue_codes_agree
        units.append(unit)

    per_check: dict[CheckName, CheckScore] = {}
    metrics_per_check = {}
    total = Counter()
    for check in CheckName:
        c = counts.get(check.value)
        if not c:
            continue
        total.update(c)
        per_check[check] = CheckScore(
            true_positive=c["tp"],
            false_positive=c["fp"],
            false_negative=c["fn"],
            true_negative=c["tn"],
            needs_review=c["review_on_fail"] + c["review_on_clean"],
        )
        metrics_per_check[check.value] = check_metrics(
            c, codes_agree[check.value], status_agree[check.value], evidence_mix[check.value]
        )
    all_evidence: Counter = Counter()
    for mix in evidence_mix.values():
        all_evidence.update(mix)
    metrics = {
        "run_id": run_id,
        "dataset_id": dataset_id,
        "verified_against": verified_against,
        "results_in_run": len(results),
        "results_not_in_key": len(extra),
        "evidence_total": evidence_total,
        "evidence_unverified": evidence_unverified,
        "results_with_unverified_evidence": sum(1 for p in problems_by_result.values() if p),
        "per_check": metrics_per_check,
        "all_checks": check_metrics(
            total, sum(codes_agree.values()), sum(status_agree.values()), all_evidence
        ),
    }
    scorecard = Scorecard(
        run_id=run_id,
        dataset_id=dataset_id,
        answer_key_sha256=answer_key_sha256,
        scored_at=datetime.now(UTC),
        per_check=per_check,
        evidence_total=evidence_total,
        evidence_unverified=evidence_unverified,
        notes=(summary_note(metrics) + (" " + notes if notes else "")).strip(),
    )
    return Scored(units, scorecard, metrics, unverified_details, extra)


def summary_note(metrics: dict) -> str:
    """A few aggregate sentences for `Scorecard.notes`. No ids, nothing per transaction."""

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    parts = []
    if metrics["evidence_unverified"]:
        parts.append(
            f"FABRICATED OR UNRESOLVABLE EVIDENCE: {metrics['evidence_unverified']} of "
            f"{metrics['evidence_total']} pieces did not verify (against "
            f"{metrics['verified_against']})."
        )
    else:
        parts.append(
            f"All {metrics['evidence_total']} pieces of evidence verified against "
            f"{metrics['verified_against']}."
        )
    for check, m in metrics["per_check"].items():
        parts.append(
            f"{check}: precision {pct(m['precision_on_fail'])}, recall {pct(m['recall_on_fail'])}"
            f" ({pct(m['recall_counting_review'])} counting needs_review), false alarms "
            f"{pct(m['false_alarm_rate_on_clean'])} of clean, needs_review "
            f"{pct(m['needs_review_rate'])}, results missing {m['results_missing']}."
        )
    parts.append("needs_review is neither a hit nor a false alarm; see evals/decisions.")
    return " ".join(parts)


def load_key(path: Path) -> tuple[list[ExpectedResult], str]:
    return read_jsonl(path, ExpectedResult), sha256_file(path)


def unit_rows(units: list[Unit]) -> list[dict]:
    return [asdict(u) for u in units]
