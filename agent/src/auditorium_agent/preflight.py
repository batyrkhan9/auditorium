"""Pre-flight: run the checks on every transaction and write a run folder."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from auditorium_contracts import (
    RESULTS,
    RUN_MANIFEST,
    CheckResult,
    CheckStatus,
    DatasetManifest,
    LogAction,
    Sources,
    verify_evidence,
    write_json,
    write_jsonl,
)

from .books import Books
from .checks import check_approval, check_ledger_match, check_period, check_receipt
from .reader import Reader
from .runs import open_run, run_manifest

CHECKS = (check_receipt, check_approval, check_ledger_match, check_period)


def _verified(result: CheckResult, books: Books) -> CheckResult | None:
    """Drop any evidence that does not resolve to something real.

    A result that loses evidence is downgraded to needs_review. A result with
    none left is not recorded at all: no evidence, no result.
    """
    good = []
    for evidence in result.evidence:
        problem = verify_evidence(evidence, books.index)
        if problem:
            books.log.append(
                "agent",
                LogAction.ERROR,
                subject_ids=[result.id],
                detail={"unverified_evidence": problem},
            )
        else:
            good.append(evidence)
    if len(good) == len(result.evidence):
        return result
    if not good:
        return None
    downgraded = result.model_dump() | {
        "status": CheckStatus.NEEDS_REVIEW,
        "issue_codes": [],
        "evidence": good,
        "summary": "Some of the evidence for this check could not be verified. " + result.summary,
    }
    return CheckResult.model_validate(downgraded)


def run_preflight(
    sources: Sources,
    out_dir: Path | str,
    *,
    fiscal_year_start: date,
    fiscal_year_end: date,
    run_id: str | None = None,
    dataset_id: str | None = None,
    dataset_path: Path | str | None = None,
    dataset_manifest: DatasetManifest | None = None,
    reader: Reader | None = None,
) -> list[CheckResult]:
    out_dir = Path(out_dir)
    log, books, started, run_id = open_run(
        sources,
        out_dir,
        detail={"mode": "preflight"},
        fiscal_year_start=fiscal_year_start,
        fiscal_year_end=fiscal_year_end,
        run_id=run_id,
        dataset_id=dataset_id,
        reader=reader,
        dataset_manifest=dataset_manifest,
    )

    results = []
    for txn in books.transactions:
        for check in CHECKS:
            result = _verified(check(txn, books, run_id), books)
            if result is None:
                continue
            results.append(result)
            log.append(
                "agent",
                LogAction.CHECK_RECORDED,
                subject_ids=[result.id, txn.id],
                detail={"check": result.check.value, "status": result.status.value},
            )

    write_jsonl(out_dir / RESULTS, results)
    log.append("system", LogAction.RUN_FINISHED, detail={"results": len(results)})
    write_json(
        out_dir / RUN_MANIFEST,
        run_manifest(
            sources,
            books,
            run_id=run_id,
            mode="preflight",
            started=started,
            dataset_id=dataset_id,
            dataset_path=dataset_path,
        ),
    )
    return results
