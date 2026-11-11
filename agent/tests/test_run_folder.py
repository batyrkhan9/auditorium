"""What the eval and the screen rely on in a run folder, beyond the results themselves."""

from datetime import date
from pathlib import Path

from auditorium_agent import matching
from auditorium_agent.preflight import run_preflight
from auditorium_agent.sample import run_sample
from auditorium_contracts import (
    SOURCES_SNAPSHOT,
    Approval,
    LedgerEntry,
    LedgerLine,
    LocalSources,
    SourceRef,
    SpendTransaction,
    load_run,
    validate_run,
)

SAMPLE = Path(__file__).parents[1] / "examples" / "sample"


class Changed(LocalSources):
    """The sample, with some transactions' approvals replaced, as a live source might return them."""

    def __init__(self, approvals_by_key):
        super().__init__(SAMPLE)
        self.approvals_by_key = approvals_by_key

    def transactions(self):
        changed = []
        for txn in super().transactions():
            key = txn.id.split(":")[-1]
            if key in self.approvals_by_key:
                txn = txn.model_copy(update={"approvals": self.approvals_by_key[key]})
            changed.append(txn)
        return changed


def preflight(tmp_path, sources):
    manifest = LocalSources(SAMPLE).dataset.manifest
    results = run_preflight(
        sources,
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
    )
    return {r.id.split(":")[-1]: r for r in results}


def test_a_run_can_be_verified_from_its_own_folder(tmp_path):
    preflight(tmp_path, LocalSources(SAMPLE))
    run = tmp_path / "run"
    assert (run / SOURCES_SNAPSHOT / "transactions.jsonl").is_file()
    # No sources passed: the snapshot is tied to the logged fingerprints and used.
    assert validate_run(run) == []

    saved = load_run(run)
    fingerprinted = [e.detail["call"] for e in saved.log if "sha256" in e.detail]
    assert fingerprinted == ["transactions", "ledger_entries", "documents", "people", "policy"]
    assert saved.log[1].detail["call"] == "transactions"  # straight after run_started
    assert saved.manifest.dataset_id == "agent-sample"
    assert saved.manifest.dataset_path == str(SAMPLE.resolve())
    assert saved.manifest.git_commit  # taken from where the agent's code lives


def test_sample_mode_writes_the_same_proof(tmp_path):
    sources = LocalSources(SAMPLE)
    manifest = sources.dataset.manifest
    run_sample(
        sources,
        sources.dataset.samples[0],
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
    )
    assert validate_run(tmp_path / "run") == []


def test_approvals_the_source_could_not_report_are_not_called_missing(tmp_path):
    results = preflight(tmp_path, Changed({"b_1001": None}))
    approval = results["b_1001#approval"]
    assert approval.status.value == "needs_review" and approval.issue_codes == []
    assert "could not say" in approval.summary
    assert validate_run(tmp_path / "run") == []


def test_a_pending_approval_is_still_unapproved_and_says_so(tmp_path):
    pending = [Approval(approver_id="p_cfo", decision="pending")]
    results = preflight(tmp_path, Changed({"b_1001": pending}))
    approval = results["b_1001#approval"]
    assert approval.status.value == "fail"
    assert [c.value for c in approval.issue_codes] == ["approval_missing"]
    assert "still pending" in approval.summary


def test_accrual_search_reads_the_vendor_named_on_a_journal_line():
    bill = SpendTransaction(
        id="t",
        kind="vendor_bill",
        source=SourceRef(system="ramp", record_type="bill", record_id="t"),
        vendor_name="Northwind Advisory LLC",
        amount_cents=1_200_000,
        transaction_date=date(2026, 1, 8),
    )
    accrual = LedgerEntry(
        id="je",
        source=SourceRef(system="qbo", record_type="JournalEntry", record_id="1"),
        entry_type="journal_entry",
        txn_date=date(2025, 12, 31),
        memo="Year-end accruals",
        total_cents=1_200_000,
        lines=[
            LedgerLine(
                account="Professional Fees",
                account_type="Expense",
                side="debit",
                amount_cents=1_200_000,
                vendor_name="Northwind Advisory",
            ),
            LedgerLine(
                account="Accrued Liabilities",
                account_type="Other Current Liability",
                side="credit",
                amount_cents=1_200_000,
            ),
        ],
    )
    found = matching.find_accruals(bill, [accrual], date(2025, 12, 1), date(2025, 12, 31))
    assert [entry.id for entry in found] == ["je"]


def test_each_kind_of_record_is_fetched_once_per_run(tmp_path):
    class Counting(LocalSources):
        """Counts fetches, as a live source that costs a network call each time would."""

        def __init__(self):
            super().__init__(SAMPLE)
            self.fetches = {}

        def _count(self, name):
            self.fetches[name] = self.fetches.get(name, 0) + 1

        def transactions(self):
            self._count("transactions")
            return super().transactions()

        def ledger_entries(self):
            self._count("ledger_entries")
            return super().ledger_entries()

        def documents(self):
            self._count("documents")
            return super().documents()

    sources = Counting()
    preflight(tmp_path, sources)
    # Fingerprints, checks and snapshot all saw the one fetch.
    assert sources.fetches == {"transactions": 1, "ledger_entries": 1, "documents": 1}
    assert validate_run(tmp_path / "run") == []
