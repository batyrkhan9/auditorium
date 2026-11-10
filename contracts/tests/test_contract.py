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
                lines=[
                    c.LedgerLine(
                        account="Professional Fees",
                        account_type="Expense",
                        side="debit",
                        amount_cents=1_200_000,
                    ),
                    c.LedgerLine(
                        account="Accounts Payable",
                        account_type="Accounts Payable",
                        side="credit",
                        amount_cents=1_200_000,
                    ),
                ],
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


def test_result_edited_after_sign_off_is_caught(tmp_path):
    build_dataset(tmp_path / "data")
    sources = c.LocalSources(tmp_path / "data")
    result = period_result()
    build_run(tmp_path / "run", sources, result)
    c.LogWriter(tmp_path / "run" / c.LOG, "run_1").sign_off(
        "Dana Reyes", result.id, "accepted", ts=TS, result=result
    )
    assert c.validate_run(tmp_path / "run", sources) == []

    edited = result.model_copy(update={"summary": "Nothing to see here."})
    c.write_jsonl(tmp_path / "run" / c.RESULTS, [edited])
    problems = c.validate_run(tmp_path / "run", sources)
    assert problems == [f"result {result.id}: changed after it was signed off"]


# --- Review round 2: the cases Noriaki reproduced ---


def signed_run(tmp_path):
    build_dataset(tmp_path / "data")
    sources = c.LocalSources(tmp_path / "data")
    result = period_result()
    build_run(tmp_path / "run", sources, result)
    return sources, result, c.LogWriter(tmp_path / "run" / c.LOG, "run_1")


def test_a_result_signed_again_after_a_change_is_not_a_problem(tmp_path):
    sources, result, log = signed_run(tmp_path)
    log.sign_off("Dana Reyes", result.id, "accepted", ts=TS, result=result)
    edited = result.model_copy(update={"summary": "Corrected wording."})
    c.write_jsonl(tmp_path / "run" / c.RESULTS, [edited])
    assert len(c.validate_run(tmp_path / "run", sources)) == 1  # stale sign-off
    log.sign_off("Dana Reyes", result.id, "accepted", ts=TS, result=edited)
    assert c.validate_run(tmp_path / "run", sources) == []  # only the latest counts


def test_sign_off_pins_what_the_reviewer_was_shown(tmp_path):
    import pytest

    sources, result, log = signed_run(tmp_path)
    shown = c.result_hash(result)  # the screen takes this when it renders the page
    rewritten = result.model_copy(update={"summary": "The agent rewrote this meanwhile."})
    with pytest.raises(ValueError, match="changed after it was shown"):
        log.sign_off("Dana Reyes", result.id, "accepted", result=rewritten, result_sha256=shown)
    event = log.sign_off("Dana Reyes", result.id, "accepted", ts=TS, result_sha256=shown)
    assert event.detail["result_sha256"] == shown
    assert c.validate_run(tmp_path / "run", sources) == []


def test_run_sign_off_pins_the_results_and_must_follow_the_finish(tmp_path):
    sources, result, log = signed_run(tmp_path)
    log.sign_off_run("Dana Reyes", "accepted", ts=TS)
    assert any("before it finished" in p for p in c.validate_run(tmp_path / "run", sources))

    log.append("system", c.LogAction.RUN_FINISHED, ts=TS)
    event = log.sign_off_run("Dana Reyes", "accepted", ts=TS)
    assert {"results_sha256", "run_sha256"} <= set(event.detail)
    assert c.validate_run(tmp_path / "run", sources) == []

    c.write_jsonl(tmp_path / "run" / c.RESULTS, [result.model_copy(update={"rationale": "x"})])
    problems = c.validate_run(tmp_path / "run", sources)
    assert "results.jsonl: changed after the run was signed off" in problems


def test_a_swapped_document_file_is_caught(tmp_path):
    sources, _, _ = signed_run(tmp_path)
    # Same quote still present, but the file is no longer the one that was hashed.
    (tmp_path / "data" / "documents" / "inv-2041.txt").write_text(INVOICE + "Total due: $1.00\n")
    problems = c.validate_run(tmp_path / "run", c.LocalSources(tmp_path / "data"))
    assert any("not the one its sha256 records" in p for p in problems)


def test_run_verifies_from_its_own_snapshot(tmp_path):
    build_dataset(tmp_path / "data")
    sources = c.LocalSources(tmp_path / "data")
    run = tmp_path / "run"
    log = c.LogWriter(run / c.LOG, "run_1")
    log.append("system", c.LogAction.RUN_STARTED, ts=TS)
    logged = c.log_source_fingerprints(log, sources)
    result = period_result()
    c.write_jsonl(run / c.RESULTS, [result])
    log.append("agent", c.LogAction.CHECK_RECORDED, subject_ids=[result.id], ts=TS)
    c.write_json(
        run / c.RUN_MANIFEST,
        c.RunManifest(
            run_id="run_1",
            mode="preflight",
            started_at=TS,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            sources=sources.describe(),
            contract_version=c.CONTRACT_VERSION,
        ),
    )
    c.snapshot_sources(sources, run / c.SOURCES_SNAPSHOT, sources.dataset.manifest)

    # The snapshot has the same fingerprints as the fetch, and the run verifies from it alone.
    assert c.fingerprint_sources(c.LocalSources(run / c.SOURCES_SNAPSHOT)) == logged
    assert c.validate_run(run) == []
    assert c.verified_against(run, None) == "snapshot"

    # Change one amount in the snapshot: it no longer matches what the log says was fetched.
    ledger = run / c.SOURCES_SNAPSHOT / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace("1200000", "1200001"))
    assert "snapshot: ledger_entries is not what the run fetched" in c.validate_run(run)


def test_no_sources_and_no_snapshot_is_a_problem(tmp_path):
    signed_run(tmp_path)
    assert c.validate_run(tmp_path / "run") == [
        "no sources were given and the run folder has no snapshot"
    ]
