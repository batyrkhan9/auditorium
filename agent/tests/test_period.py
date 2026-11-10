from datetime import date
from pathlib import Path

from auditorium_agent.matching import same_vendor
from auditorium_agent.preflight import run_preflight
from auditorium_agent.service_period import find_service_period
from auditorium_contracts import LocalSources, validate_run

SAMPLE = Path(__file__).parents[1] / "examples" / "sample"


def run(tmp_path):
    sources = LocalSources(SAMPLE)
    manifest = sources.dataset.manifest
    results = run_preflight(
        sources,
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
    )
    return sources, {r.transaction_id: r for r in results if r.check.value == "period"}


def test_sample_statuses(tmp_path):
    sources, by_txn = run(tmp_path)
    assert {txn: r.status.value for txn, r in by_txn.items()} == {
        "ramp:bill:b_1001": "fail",  # the pitch example
        "ramp:bill:b_1002": "pass",  # December work, accrued in December
        "ramp:bill:b_1003": "pass",
        "ramp:bill:b_1004": "needs_review",  # no service period, recorded just after year end
        "ramp:bill:b_1005": "pass",
        "ramp:bill:b_1006": "pass",  # the amount is wrong in the books, the period is not
        "ramp:bill:b_1007": "pass",
        "ramp:card:c_2001": "pass",
        "ramp:card:c_2002": "not_applicable",  # not in the books at all
    }
    assert validate_run(tmp_path / "run", sources) == []


def test_pitch_example_is_flagged_with_evidence(tmp_path):
    _, by_txn = run(tmp_path)
    result = by_txn["ramp:bill:b_1001"]
    assert [code.value for code in result.issue_codes] == ["period_wrong"]
    assert (result.expected_period, result.recorded_period) == ("2025-12", "2026-01")
    kinds = [e.kind for e in result.evidence]
    assert kinds == ["document", "record", "search"]
    assert "December 31, 2025" in result.evidence[0].quote
    assert result.evidence[2].result_ids == []


def test_service_period_formats():
    def period(line):
        found = find_service_period((line,))
        return (found.start, found.end) if found else None

    december = (date(2025, 12, 1), date(2025, 12, 31))
    assert period("Services rendered: December 1 - December 31, 2025") == december
    assert period("Service period: 12/01/2025 - 12/31/2025") == december
    assert period("Hosting for the month of December 2025") == december
    assert period("Billing period: Dec 15 - Jan 14, 2026") == (
        date(2025, 12, 15),
        date(2026, 1, 14),
    )
    assert period("Invoice date: January 8, 2026") is None
    assert period("Brand design work") is None


def test_vendor_names_match_across_systems():
    assert same_vendor("Northwind Advisory LLC", "Northwind Advisory")
    assert same_vendor("Harlow & Finch LLP", "Harlow and Finch")
    assert not same_vendor("Cloudline Hosting", "Brightside Design Co.")
