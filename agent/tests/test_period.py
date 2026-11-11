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
    statuses = {txn: r.status.value for txn, r in by_txn.items()}
    assert {txn: status for txn, status in statuses.items() if status != "pass"} == {
        "ramp:bill:b_1001": "fail",  # the pitch example
        "ramp:bill:b_1004": "needs_review",  # no service period, recorded just after year end
        "ramp:bill:b_1012": "needs_review",  # dates given in words; settled only with a reader
        "ramp:card:c_2002": "not_applicable",  # not in the books at all
        "ramp:card:c_2009": "not_applicable",  # the books' entry is not confirmed as its own
    }
    # December work accrued in December, and a wrong amount in the books, are not period errors.
    assert statuses["ramp:bill:b_1002"] == statuses["ramp:bill:b_1006"] == "pass"
    assert len(statuses) == len(sources.transactions())
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


def test_service_period_on_a_wrapped_line():
    # What a PDF gives when a long line wraps: the range is split across two lines.
    page = (
        "Legal services for the financing and related matters. Service period: December 1 -\n"
        "December 31, 2025\nAmount due: $4,500.00"
    )
    found = find_service_period((page,))
    assert (found.start, found.end) == (date(2025, 12, 1), date(2025, 12, 31))
    assert found.quote.endswith("December 1 - December 31, 2025")

    # The label is at the end of one line and the dates are all on the next.
    found = find_service_period(
        ("Consulting engagement. Services rendered:\n12/01/2025 - 12/31/2025",)
    )
    assert (found.start, found.end) == (date(2025, 12, 1), date(2025, 12, 31))

    # A complete range is not stretched by an unrelated date on the following line.
    found = find_service_period(("Service period: 12/01/2025 - 12/15/2025\nDue date: 01/30/2026",))
    assert found.end == date(2025, 12, 15)
