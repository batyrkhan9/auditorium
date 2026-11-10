from datetime import date
from pathlib import Path

from auditorium_agent import matching
from auditorium_agent.preflight import run_preflight
from auditorium_contracts import LedgerEntry, LocalSources, SourceRef, SpendTransaction

SAMPLE = Path(__file__).parents[1] / "examples" / "sample"


def txn(key, vendor, cents, day, invoice=None):
    return SpendTransaction(
        id=key,
        kind="vendor_bill",
        source=SourceRef(system="ramp", record_type="bill", record_id=key),
        vendor_name=vendor,
        amount_cents=cents,
        transaction_date=day,
        invoice_number=invoice,
    )


def entry(key, vendor, cents, day, doc=None):
    return LedgerEntry(
        id=key,
        source=SourceRef(system="qbo", record_type="Bill", record_id=key),
        entry_type="bill",
        txn_date=day,
        vendor_name=vendor,
        doc_number=doc,
        total_cents=cents,
    )


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
        if r.check.value == "ledger_match"
    }
    assert {key: value for key, value in found.items() if value[0] != "pass"} == {
        "b_1006": ("fail", ["ledger_match_amount"]),
        "b_1007": ("fail", ["ledger_match_duplicate"]),
        "c_2002": ("fail", ["ledger_match_missing"]),
    }
    assert len(found) == len(sources.transactions())


def test_recurring_bills_each_keep_their_own_entry():
    # Same vendor, same amount, no invoice numbers: only the dates tell them apart.
    txns = [
        txn(f"t{m}", "Cloudline Hosting, Inc.", 230_000, date(2025, m, 28)) for m in (9, 10, 11)
    ]
    ledger = [entry(f"e{m}", "Cloudline Hosting", 230_000, date(2025, m, 30)) for m in (11, 9, 10)]
    matches = matching.match_ledger(txns, ledger)
    assert {t: m.entry.id for t, m in matches.items()} == {"t9": "e9", "t10": "e10", "t11": "e11"}
    assert not any(m.duplicates for m in matches.values())


def test_same_amount_under_another_vendor_is_only_a_candidate():
    matches = matching.match_ledger(
        [txn("t1", "AMZN Mktp US", 4_999, date(2025, 6, 2))],
        [entry("e1", "Amazon", 4_999, date(2025, 6, 3))],
    )
    assert matches["t1"].how == matching.VENDOR_UNCLEAR


def test_same_invoice_and_amount_under_another_vendor_is_a_vendor_mismatch():
    matches = matching.match_ledger(
        [txn("t1", "Northwind Advisory LLC", 50_000, date(2025, 6, 2), "NW-9")],
        [entry("e1", "Southwind Partners", 50_000, date(2025, 6, 20), "NW-9")],
    )
    assert matches["t1"].how == matching.VENDOR_DIFFERS


def test_an_unrelated_entry_is_not_a_match():
    matches = matching.match_ledger(
        [txn("t1", "Figma", 54_000, date(2025, 9, 12))],
        [entry("e1", "Delta Air Lines", 128_400, date(2025, 9, 12))],
    )
    assert matches["t1"] == matching.Match(None, matching.NONE)
