"""What every run does first and last, whichever mode it is in.

First: start the log, fingerprint what the sources returned, and save a copy of it
into the run folder. That happens before any check, so the log can later prove the
snapshot is the data the checks actually saw. Last: write the run manifest.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from auditorium_contracts import (
    CONTRACT_VERSION,
    LOG,
    SOURCES_SNAPSHOT,
    DatasetManifest,
    LogAction,
    LogWriter,
    RunManifest,
    Sources,
    log_source_fingerprints,
    snapshot_sources,
)

from .books import Books
from .reader import Reader


def git_commit() -> str | None:
    """The commit of the agent's own code, wherever the run was started from."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


class FetchedOnce:
    """The caller's sources, asked once for each kind of record and remembered.

    The fingerprints, the checks and the snapshot must all be the same data. The
    contract asks every `Sources` to return the same records each time, and this
    makes sure of it from the agent's side: a live source is fetched once per run.
    """

    def __init__(self, inner: Sources):
        self.inner = inner
        self._kept: dict[str, object] = {}

    def _once(self, name: str):
        if name not in self._kept:
            self._kept[name] = getattr(self.inner, name)()
        return self._kept[name]

    def describe(self):
        return self._once("describe")

    def transactions(self):
        return self._once("transactions")

    def ledger_entries(self):
        return self._once("ledger_entries")

    def documents(self):
        return self._once("documents")

    def people(self):
        return self._once("people")

    def policy(self):
        return self._once("policy")

    def document_file(self, document_id: str) -> Path:
        return self.inner.document_file(document_id)


def _dataset_of(sources: Sources):
    """The dataset a `LocalSources` serves, if that is what these sources are."""
    return getattr(getattr(sources, "inner", sources), "dataset", None)


def _snapshot_manifest(
    sources: Sources,
    books: Books,
    run_id: str,
    dataset_id: str | None,
    dataset_manifest: DatasetManifest | None,
) -> DatasetManifest:
    if dataset_manifest is not None:
        return dataset_manifest
    dataset = _dataset_of(sources)
    if dataset is not None:
        return dataset.manifest
    dates = [t.transaction_date for t in books.transactions] + [e.txn_date for e in books.ledger]
    return DatasetManifest(
        dataset_id=dataset_id or f"snapshot-{run_id}",
        company_name="(as fetched)",
        contract_version=CONTRACT_VERSION,
        fiscal_year_start=books.fiscal_year_start,
        fiscal_year_end=books.fiscal_year_end,
        coverage_end=max([books.fiscal_year_end, *dates]),
        synthetic=any(source.synthetic for source in sources.describe()),
        description=f"What run {run_id} fetched from its sources.",
    )


def open_run(
    sources: Sources,
    out_dir: Path,
    *,
    detail: dict,
    fiscal_year_start: date,
    fiscal_year_end: date,
    run_id: str | None,
    dataset_id: str | None,
    reader: Reader | None,
    dataset_manifest: DatasetManifest | None = None,
) -> tuple[LogWriter, Books, datetime, str]:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty. Each run gets its own folder.")
    started = datetime.now(UTC)
    run_id = run_id or f"run_{started:%Y%m%d_%H%M%S}"

    log = LogWriter(out_dir / LOG, run_id)
    log.append("system", LogAction.RUN_STARTED, detail=detail)
    sources = FetchedOnce(sources)  # one fetch: what is fingerprinted, checked and saved
    log_source_fingerprints(log, sources)
    books = Books.open(sources, log, fiscal_year_start, fiscal_year_end, reader)
    manifest = _snapshot_manifest(sources, books, run_id, dataset_id, dataset_manifest)
    snapshot_sources(sources, out_dir / SOURCES_SNAPSHOT, manifest)
    return log, books, started, run_id


def run_manifest(
    sources: Sources,
    books: Books,
    *,
    run_id: str,
    mode: str,
    started: datetime,
    dataset_id: str | None,
    dataset_path: Path | str | None,
    sample_id: str | None = None,
) -> RunManifest:
    sources = books.sources  # what this run actually fetched
    dataset = _dataset_of(sources)
    if dataset_path is None and dataset is not None:
        dataset_path = dataset.root
    return RunManifest(
        run_id=run_id,
        mode=mode,
        started_at=started,
        finished_at=datetime.now(UTC),
        period_start=books.fiscal_year_start,
        period_end=books.fiscal_year_end,
        dataset_id=dataset_id or (dataset.manifest.dataset_id if dataset else None),
        dataset_path=str(Path(dataset_path).resolve()) if dataset_path else None,
        sample_id=sample_id,
        sources=sources.describe(),
        models=sorted(books.models_used),
        git_commit=git_commit(),
        contract_version=CONTRACT_VERSION,
    )
