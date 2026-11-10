"""End-to-end check of the contract on the pitch example: a $12,000 consulting bill,
work done in December, recorded in January. Also the shortest worked example of
how a dataset and a run are written."""

from datetime import UTC, date, datetime

import auditorium_contracts as c
from auditorium_contracts.schema import export_schemas

INVOICE = "Northwind Advisory LLC\nInvoice INV-2041\nServices rendered: December 1 - December 31, 2025\nTotal due: $12,000.00\n"
TS = datetime(2026, 2, 1, tzinfo=UTC)


def build_dataset(root):
    (root / "documents").mkdir(parents=True)
    (root / "documents" / "inv-2041.txt").write_text(INVOICE)
    ramp = c.SourceRef(system="ramp", record_type="bill", record_id="b_1001")
    c.write_json(
        root / "manifest.json",
        c.DatasetManifest(
            dataset_id="sample",
            company_name="Larkspur Labs",
            contract_version=c.CONTRACT_VERSION,
            fiscal_year_start=date(2025, 1, 1),
            fiscal_year_end=date(2025, 12, 31),
            coverage_end=date(2026, 2, 28),
        ),
    )
    c.write_jsonl(
        root / "transactions.jsonl",
        [
            c.SpendTransaction(
                id="ramp:bill:b_1001",
                kind="vendor_bill",
                source=ramp,
                vendor_name="Northwind Advisory LLC",
                amount_cents=1_200_000,
                transaction_date=date(2026, 1, 8),
                invoice_number="INV-2041",
                document_ids=["doc_inv_2041"],
            )
        ],
    )
    c.write_jsonl(
        root / "ledger.jsonl",
        [
            c.LedgerEntry(
                id="qbo:Bill:147",
                source=c.SourceRef(system="qbo", record_type="Bill", record_id="147"),
                entry_type="bill",
                txn_date=date(2026, 1, 8),
                vendor_name="Northwind Advisory LLC",
                total_cents=1_200_000,
            )
        ],
    )
    c.write_jsonl(
        root / "documents.jsonl",
        [
            c.Document(
                id="doc_inv_2041",
                type="invoice",
                source=ramp,
                origin="external",
                path="documents/inv-2041.txt",
                media_type="text/plain",
                sha256=c.sha256_file(root / "documents" / "inv-2041.txt"),
            )
        ],
    )
    c.write_jsonl(root / "people.jsonl", [c.Person(id="p_cfo", name="Dana Reyes", role="cfo")])
    c.write_json(
        root / "policy.json",
        c.SpendPolicy(
            id="policy_2025",
            effective_from=date(2025, 1, 1),
            receipt_required_from_cents=7500,
            approval_rules=[],
        ),
    )


def period_result(quote="Services rendered: December 1 - December 31, 2025"):
    return c.CheckResult(
        id=c.result_id("ramp:bill:b_1001", "period"),
        run_id="run_1",
        transaction_id="ramp:bill:b_1001",
        check="period",
        status="fail",
        issue_codes=["period_wrong"],
        summary="December work was recorded in January with no December accrual.",
        rationale="The invoice covers December 2025; the only ledger entry is dated 2026-01-08.",
        evidence=[
            c.DocumentEvidence(document_id="doc_inv_2041", page=1, quote=quote),
            c.RecordEvidence(
                record_type="ledger_entry",
                record_id="qbo:Bill:147",
                field="txn_date",
                value="2026-01-08",
            ),
            c.SearchEvidence(system="qbo", query="December 2025 journal entries for Northwind"),
        ],
        recorded_period="2026-01",
        expected_period="2025-12",
    )


def build_run(root, sources, result):
    c.write_json(
        root / c.RUN_MANIFEST,
        c.RunManifest(
            run_id="run_1",
            mode="preflight",
            started_at=TS,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            dataset_id="sample",
            sources=sources.describe(),
            contract_version=c.CONTRACT_VERSION,
        ),
    )
    c.write_jsonl(root / c.RESULTS, [result])
    log = c.LogWriter(root / c.LOG, "run_1")
    log.append("system", c.LogAction.RUN_STARTED, ts=TS)
    log.append("agent", c.LogAction.CHECK_RECORDED, subject_ids=[result.id], ts=TS)
    log.sign_off("Dana Reyes", result.id, "accepted", ts=TS)


def test_pitch_example_round_trips(tmp_path):
    build_dataset(tmp_path / "data")
    assert c.validate_dataset(tmp_path / "data") == []
    sources = c.LocalSources(tmp_path / "data")
    assert isinstance(sources, c.Sources)
    build_run(tmp_path / "run", sources, period_result())
    assert c.validate_run(tmp_path / "run", sources) == []
    assert c.load_run(tmp_path / "run").log[-1].action is c.LogAction.SIGNOFF


def test_made_up_quote_is_caught(tmp_path):
    build_dataset(tmp_path / "data")
    sources = c.LocalSources(tmp_path / "data")
    build_run(tmp_path / "run", sources, period_result(quote="Services rendered: January 2026"))
    problems = c.validate_run(tmp_path / "run", sources)
    assert len(problems) == 1 and "quote not found" in problems[0]


def test_edited_log_is_caught(tmp_path):
    build_dataset(tmp_path / "data")
    sources = c.LocalSources(tmp_path / "data")
    build_run(tmp_path / "run", sources, period_result())
    log = tmp_path / "run" / c.LOG
    log.write_text(log.read_text().replace("accepted", "disputed"))
    assert any("does not match its hash" in p for p in c.verify_log(log))


def test_answer_key_line_parses():
    line = '{"transaction_id": "t", "check": "period", "status": "fail", "issue_codes": ["period_wrong"]}'
    assert c.ExpectedResult.model_validate_json(line).status is c.CheckStatus.FAIL


def test_schema_files_are_current():
    assert export_schemas(check=True) == []
