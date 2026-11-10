"""Pre-flight: run the checks on every transaction and write a run folder."""

from __future__ import annotations

import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from auditorium_contracts import (
    CONTRACT_VERSION,
    LOG,
    RESULTS,
    RUN_MANIFEST,
    CheckResult,
    CheckStatus,
    LogAction,
    LogWriter,
    RunManifest,
    Sources,
    verify_evidence,
    write_json,
    write_jsonl,
)

from .books import Books
from .checks import check_ledger_match, check_period, check_receipt

CHECKS = (check_receipt, check_ledger_match, check_period)


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


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
) -> list[CheckResult]:
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty. Each run gets its own folder.")
    started = datetime.now(UTC)
    run_id = run_id or f"run_{started:%Y%m%d_%H%M%S}"

    log = LogWriter(out_dir / LOG, run_id)
    log.append("system", LogAction.RUN_STARTED, detail={"mode": "preflight"})
    books = Books.open(sources, log, fiscal_year_start, fiscal_year_end)

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
        RunManifest(
            run_id=run_id,
            mode="preflight",
            started_at=started,
            finished_at=datetime.now(UTC),
            period_start=fiscal_year_start,
            period_end=fiscal_year_end,
            dataset_id=dataset_id,
            sources=sources.describe(),
            git_commit=_git_commit(),
            contract_version=CONTRACT_VERSION,
        ),
    )
    return results
