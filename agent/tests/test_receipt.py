from pathlib import Path

from auditorium_agent.preflight import run_preflight
from auditorium_agent.receipt_reader import amounts_on, find_amount, lines_of, stated_total
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
        if r.check.value == "receipt"
    }
    assert {key: value for key, value in found.items() if value[0] != "pass"} == {
        "c_2003": ("fail", ["receipt_missing"]),  # over the threshold, nothing attached
        "c_2004": ("not_applicable", []),  # under the threshold
        "c_2005": ("needs_review", []),  # right restaurant, charge includes a tip
        "b_1008": ("fail", ["receipt_mismatch"]),  # another vendor's invoice attached
    }
    assert len(found) == len(sources.transactions())
    assert validate_run(tmp_path / "run", sources) == []


def test_amounts_on_a_line():
    assert amounts_on("Total due: $12,000.00") == [1_200_000]
    assert amounts_on("Subtotal 183.50  Tax 16.50") == [18_350, 1_650]
    assert amounts_on("Flat fee $540") == [54_000]
    assert amounts_on("Invoice 5512, dated 03/30/2025") == []
    assert amounts_on("Table 12, 4 guests") == []


def test_total_line_is_preferred():
    lines = lines_of(("Deposit: $200.00\nItem: $200.00\nTotal: $200.00\nTip line: ____",))
    assert find_amount(lines, 20_000).text == "Total: $200.00"
    assert stated_total(lines)[1] == 20_000
