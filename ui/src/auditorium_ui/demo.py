"""A small synthetic dataset and two runs, for looking at the screen before the agent exists.

Everything here is made up and written through the contract models, then checked
with `validate_dataset` and `validate_run`. The screen labels it as demo data.
It includes the pitch example: a $12,000 consulting bill for December work,
recorded in January, flagged as the wrong period.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import auditorium_contracts as c

DATASET_ID = "demo-larkspur"
PREFLIGHT_RUN_ID = "demo-run-preflight"
SAMPLE_RUN_ID = "demo-run-sample"
START = datetime(2026, 2, 2, 9, 0, tzinfo=UTC)

DOCUMENTS = {
    "doc_inv_2041": (
        "invoice",
        "external",
        "inv-2041.txt",
        "Northwind Advisory LLC\n"
        "Invoice INV-2041\n"
        "Invoice date: January 8, 2026\n"
        "Bill to: Larkspur Labs, Inc.\n"
        "Services rendered: December 1 - December 31, 2025\n"
        "Finance systems advisory, 80 hours\n"
        "Total due: $12,000.00\n",
    ),
    "doc_rcpt_figma": (
        "receipt",
        "external",
        "figma-2025-12.txt",
        "Figma, Inc.\nReceipt #FG-88213\nDate paid: December 3, 2025\n"
        "Professional plan, 3 editors\nAmount paid: $45.00\n",
    ),
    "doc_inv_aws": (
        "invoice",
        "external",
        "aws-2025-11.txt",
        "Amazon Web Services, Inc.\nInvoice 2025-11-0047\n"
        "Billing period: November 1 - November 30, 2025\nTotal: $3,180.22\n",
    ),
    "doc_inv_wework": (
        "invoice",
        "external",
        "wework-2025-12.txt",
        "WeWork\nInvoice WW-55120\nInvoice date: December 20, 2025\n"
        "Membership: December 15, 2025 - January 14, 2026\nTotal: $8,500.00\n",
    ),
    "doc_policy": (
        "policy",
        "internal",
        "spend-policy.txt",
        "Larkspur Labs spend policy (effective January 1, 2025)\n"
        "Card expenses of $75.00 or more need an itemized receipt.\n"
        "Vendor bills of $5,000.00 or more need CFO approval.\n",
    ),
}


def _ramp(record_type: str, record_id: str) -> c.SourceRef:
    return c.SourceRef(system="ramp", record_type=record_type, record_id=record_id)


def _qbo(record_type: str, record_id: str) -> c.SourceRef:
    return c.SourceRef(system="qbo", record_type=record_type, record_id=record_id)


def build_dataset(root: Path) -> Path:
    root = Path(root)
    documents = []
    for doc_id, (doc_type, origin, filename, text) in DOCUMENTS.items():
        file = root / "documents" / filename
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        documents.append(
            c.Document(
                id=doc_id,
                type=doc_type,
                source=_ramp("attachment", doc_id),
                origin=origin,
                path=f"documents/{filename}",
                media_type="text/plain",
                sha256=c.sha256_file(file),
                filename=filename,
            )
        )
    c.write_jsonl(root / "documents.jsonl", documents)
    c.write_json(
        root / "manifest.json",
        c.DatasetManifest(
            dataset_id=DATASET_ID,
            company_name="Larkspur Labs (demo)",
            contract_version=c.CONTRACT_VERSION,
            fiscal_year_start=date(2025, 1, 1),
            fiscal_year_end=date(2025, 12, 31),
            coverage_end=date(2026, 2, 28),
            description="Synthetic demo data for the UI. Not a real company.",
        ),
    )
    c.write_jsonl(
        root / "people.jsonl",
        [
            c.Person(id="p_cfo", name="Dana Reyes", email="dana@larkspur.example", role="cfo"),
            c.Person(
                id="p_eng",
                name="Sam Okafor",
                email="sam@larkspur.example",
                role="engineer",
                manager_id="p_cfo",
            ),
        ],
    )
    approved = c.Approval(
        approver_id="p_cfo", decision="approved", decided_at=datetime(2026, 1, 9, tzinfo=UTC)
    )
    c.write_jsonl(
        root / "transactions.jsonl",
        [
            c.SpendTransaction(
                id="ramp:bill:b_1001",
                kind="vendor_bill",
                source=_ramp("bill", "b_1001"),
                vendor_name="Northwind Advisory LLC",
                amount_cents=1_200_000,
                transaction_date=date(2026, 1, 8),
                invoice_number="INV-2041",
                spender_id="p_cfo",
                document_ids=["doc_inv_2041"],
                approvals=[approved],
            ),
            c.SpendTransaction(
                id="ramp:txn:t_2001",
                kind="card_expense",
                source=_ramp("transaction", "t_2001"),
                vendor_name="Figma",
                amount_cents=4_500,
                transaction_date=date(2025, 12, 3),
                spender_id="p_eng",
                document_ids=["doc_rcpt_figma"],
            ),
            c.SpendTransaction(
                id="ramp:txn:t_2002",
                kind="card_expense",
                source=_ramp("transaction", "t_2002"),
                vendor_name="Delta Air Lines",
                amount_cents=61_240,
                transaction_date=date(2025, 12, 11),
                spender_id="p_eng",
            ),
            c.SpendTransaction(
                id="ramp:bill:b_1002",
                kind="vendor_bill",
                source=_ramp("bill", "b_1002"),
                vendor_name="Amazon Web Services",
                amount_cents=318_022,
                transaction_date=date(2025, 11, 30),
                invoice_number="2025-11-0047",
                spender_id="p_eng",
                document_ids=["doc_inv_aws"],
            ),
            c.SpendTransaction(
                id="ramp:bill:b_1003",
                kind="vendor_bill",
                source=_ramp("bill", "b_1003"),
                vendor_name="WeWork",
                amount_cents=850_000,
                transaction_date=date(2025, 12, 20),
                invoice_number="WW-55120",
                spender_id="p_cfo",
                document_ids=["doc_inv_wework"],
                approvals=[
                    c.Approval(
                        approver_id="p_cfo",
                        decision="approved",
                        decided_at=datetime(2025, 12, 21, tzinfo=UTC),
                    )
                ],
            ),
        ],
    )
    c.write_jsonl(
        root / "ledger.jsonl",
        [
            c.LedgerEntry(
                id="qbo:Bill:147",
                source=_qbo("Bill", "147"),
                entry_type="bill",
                txn_date=date(2026, 1, 8),
                vendor_name="Northwind Advisory LLC",
                doc_number="INV-2041",
                total_cents=1_200_000,
            ),
            c.LedgerEntry(
                id="qbo:Purchase:88",
                source=_qbo("Purchase", "88"),
                entry_type="expense",
                txn_date=date(2025, 12, 3),
                vendor_name="Figma",
                total_cents=4_500,
            ),
            c.LedgerEntry(
                id="qbo:Purchase:91",
                source=_qbo("Purchase", "91"),
                entry_type="expense",
                txn_date=date(2025, 12, 11),
                vendor_name="Delta Air Lines",
                total_cents=61_240,
            ),
            c.LedgerEntry(
                id="qbo:Bill:139",
                source=_qbo("Bill", "139"),
                entry_type="bill",
                txn_date=date(2025, 11, 30),
                vendor_name="Amazon Web Services",
                doc_number="2025-11-0047",
                total_cents=310_822,
            ),
            c.LedgerEntry(
                id="qbo:Bill:152",
                source=_qbo("Bill", "152"),
                entry_type="bill",
                txn_date=date(2025, 12, 20),
                vendor_name="WeWork",
                doc_number="WW-55120",
                total_cents=850_000,
            ),
        ],
    )
    c.write_json(
        root / "policy.json",
        c.SpendPolicy(
            id="policy_2025",
            effective_from=date(2025, 1, 1),
            receipt_required_from_cents=7_500,
            approval_rules=[
                c.ApprovalRule(
                    id="rule_bills_5k",
                    description="Vendor bills of $5,000 or more need CFO approval.",
                    kinds=["vendor_bill"],
                    min_amount_cents=500_000,
                    approver_role="cfo",
                )
            ],
            document_id="doc_policy",
        ),
    )
    (root / "samples").mkdir(exist_ok=True)
    c.write_json(
        root / "samples" / "s_2026_01.json",
        c.SampleRequest(
            id="s_2026_01",
            auditor="Demo Audit LLP",
            received_on=date(2026, 2, 1),
            items=[
                c.SampleItem(
                    line=1,
                    description="Northwind Advisory consulting",
                    vendor_name="Northwind Advisory",
                    amount_cents=1_200_000,
                    reference="INV-2041",
                ),
                c.SampleItem(
                    line=2,
                    description="Air travel, Delta",
                    transaction_date=date(2025, 12, 11),
                    amount_cents=61_240,
                ),
                c.SampleItem(
                    line=3,
                    description="Catering, Bayside Events",
                    transaction_date=date(2025, 12, 18),
                    amount_cents=240_000,
                ),
            ],
        ),
    )
    return root


def _doc(doc_id: str, quote: str, note: str | None = None) -> c.DocumentEvidence:
    return c.DocumentEvidence(document_id=doc_id, page=1, quote=quote, note=note)


def _rec(record_type: str, record_id: str, field: str, value: str, note: str | None = None):
    return c.RecordEvidence(
        record_type=record_type, record_id=record_id, field=field, value=value, note=note
    )


def _result(
    run_id: str, txn: str, check: str, status: str, summary: str, rationale: str, evidence, **kw
):
    return c.CheckResult(
        id=c.result_id(txn, check),
        run_id=run_id,
        transaction_id=txn,
        check=check,
        status=status,
        summary=summary,
        rationale=rationale,
        evidence=evidence,
        **kw,
    )


def _results(run_id: str) -> list[c.CheckResult]:
    northwind, figma, delta, aws, wework = (
        "ramp:bill:b_1001",
        "ramp:txn:t_2001",
        "ramp:txn:t_2002",
        "ramp:bill:b_1002",
        "ramp:bill:b_1003",
    )
    r = _result
    return [
        r(run_id, northwind, "receipt", "pass", "The Northwind invoice INV-2041 is attached.",
          "A vendor bill needs its invoice; the attached document is that invoice.",
          [_doc("doc_inv_2041", "Invoice INV-2041"), _rec("transaction", northwind, "document_ids.0", "doc_inv_2041")]),
        r(run_id, northwind, "approval", "pass", "The CFO approved the $12,000 bill.",
          "Bills of $5,000 or more need CFO approval; Dana Reyes (CFO) approved it.",
          [_rec("transaction", northwind, "approvals.0.approver_id", "p_cfo"), _rec("person", "p_cfo", "role", "cfo")]),
        r(run_id, northwind, "ledger_match", "pass", "QuickBooks bill 147 matches vendor, number and amount.",
          "Same vendor, invoice number and total.",
          [_rec("ledger_entry", "qbo:Bill:147", "total_cents", "1200000"), _rec("ledger_entry", "qbo:Bill:147", "doc_number", "INV-2041")]),
        r(run_id, northwind, "period", "fail",
          "December 2025 consulting was recorded in January 2026, with no December accrual.",
          "The invoice covers December 2025. The only ledger entry is dated 2026-01-08, and no December journal entry books it another way.",
          [
              _doc("doc_inv_2041", "Services rendered: December 1 - December 31, 2025", "Service period on the invoice"),
              _rec("ledger_entry", "qbo:Bill:147", "txn_date", "2026-01-08", "Date the books use"),
              c.SearchEvidence(system="qbo", query="Journal entries dated December 2025 mentioning Northwind or INV-2041", result_ids=[], note="No accrual found"),
          ],
          issue_codes=["period_wrong"], recorded_period="2026-01", expected_period="2025-12"),
        r(run_id, figma, "receipt", "not_applicable", "Card expense of $45.00 is under the $75.00 receipt threshold.",
          "The policy needs a receipt at $75.00 or more.",
          [_rec("policy", "policy_2025", "receipt_required_from_cents", "7500"), _rec("transaction", figma, "amount_cents", "4500")]),
        r(run_id, figma, "approval", "not_applicable", "No approval rule covers card expenses.",
          "The only rule applies to vendor bills.",
          [_rec("policy", "policy_2025", "approval_rules.0.kinds.0", "vendor_bill")]),
        r(run_id, figma, "ledger_match", "pass", "QuickBooks purchase 88 matches.",
          "Same vendor, date and amount.",
          [_rec("ledger_entry", "qbo:Purchase:88", "total_cents", "4500")]),
        r(run_id, figma, "period", "pass", "Paid and recorded in December 2025.",
          "Receipt date and ledger date are both December 2025.",
          [_doc("doc_rcpt_figma", "Date paid: December 3, 2025"), _rec("ledger_entry", "qbo:Purchase:88", "txn_date", "2025-12-03")]),
        r(run_id, delta, "receipt", "fail", "No receipt is attached to the $612.40 Delta charge.",
          "Card expenses of $75.00 or more need a receipt. Ramp holds no attachment for this charge.",
          [
              _rec("transaction", delta, "amount_cents", "61240"),
              c.SearchEvidence(system="ramp", query="Attachments on transaction t_2002", result_ids=[], note="Nothing attached"),
          ],
          issue_codes=["receipt_missing"]),
        r(run_id, delta, "approval", "not_applicable", "No approval rule covers card expenses.",
          "The only rule applies to vendor bills.",
          [_rec("policy", "policy_2025", "approval_rules.0.kinds.0", "vendor_bill")]),
        r(run_id, delta, "ledger_match", "pass", "QuickBooks purchase 91 matches.",
          "Same vendor, date and amount.",
          [_rec("ledger_entry", "qbo:Purchase:91", "total_cents", "61240")]),
        r(run_id, delta, "period", "pass", "Charged and recorded in December 2025.",
          "Travel on the charge date; recorded the same day.",
          [_rec("ledger_entry", "qbo:Purchase:91", "txn_date", "2025-12-11")]),
        r(run_id, aws, "receipt", "pass", "The AWS invoice is attached.", "The attached invoice matches the bill.",
          [_doc("doc_inv_aws", "Invoice 2025-11-0047")]),
        r(run_id, aws, "approval", "not_applicable", "The bill is under the $5,000 approval threshold.",
          "Rule rule_bills_5k starts at $5,000.",
          [_rec("policy", "policy_2025", "approval_rules.0.min_amount_cents", "500000")]),
        r(run_id, aws, "ledger_match", "fail", "QuickBooks bill 139 is $72.00 less than the invoice.",
          "The invoice and Ramp say $3,180.22; QuickBooks says $3,108.22.",
          [_doc("doc_inv_aws", "Total: $3,180.22"), _rec("ledger_entry", "qbo:Bill:139", "total_cents", "310822")],
          issue_codes=["ledger_match_amount"]),
        r(run_id, aws, "period", "pass", "November usage recorded in November 2025.",
          "Billing period and ledger date are both November 2025.",
          [_doc("doc_inv_aws", "Billing period: November 1 - November 30, 2025"), _rec("ledger_entry", "qbo:Bill:139", "txn_date", "2025-11-30")]),
        r(run_id, wework, "receipt", "pass", "The WeWork invoice is attached.", "The attached invoice matches the bill.",
          [_doc("doc_inv_wework", "Invoice WW-55120")]),
        r(run_id, wework, "approval", "pass", "The CFO approved the $8,500 bill.",
          "Bills of $5,000 or more need CFO approval; the CFO approved it.",
          [_rec("transaction", wework, "approvals.0.approver_id", "p_cfo")]),
        r(run_id, wework, "ledger_match", "pass", "QuickBooks bill 152 matches.", "Same vendor, number and amount.",
          [_rec("ledger_entry", "qbo:Bill:152", "total_cents", "850000")]),
        r(run_id, wework, "period", "needs_review",
          "The membership spans December and January; the whole bill is in December.",
          "Half the service period falls in January 2026. Whether that needs a prepaid adjustment is a judgement for a person.",
          [_doc("doc_inv_wework", "Membership: December 15, 2025 - January 14, 2026"), _rec("ledger_entry", "qbo:Bill:152", "txn_date", "2025-12-20")]),
    ]  # fmt: skip


def _manifest(run_id: str, mode: str, sources: c.LocalSources, sample_id: str | None = None):
    described = sources.describe()
    # Show both badges in the demo: pretend the Ramp demo MCP server was reached.
    described[0] = c.SourceUse(system="ramp", mode="live", detail="Ramp demo MCP server (demo)")
    return c.RunManifest(
        run_id=run_id,
        mode=mode,
        started_at=START,
        finished_at=START + timedelta(minutes=4),
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        dataset_id=DATASET_ID,
        sample_id=sample_id,
        sources=described,
        models=["claude-sonnet-5-5"],
        contract_version=c.CONTRACT_VERSION,
    )


def _write_log(run_dir: Path, run_id: str, results, drafts, packets) -> None:
    log = c.LogWriter(run_dir / c.LOG, run_id)
    ts = START
    step = timedelta(seconds=7)
    log.append("system", c.LogAction.RUN_STARTED, actor_id="auditorium-demo", ts=ts)
    log.append(
        "agent",
        c.LogAction.SOURCE_CALL,
        actor_id="ramp",
        detail={"call": "list_transactions", "returned": 5},
        ts=(ts := ts + step),
    )
    for result in results:
        for evidence in result.evidence:
            if isinstance(evidence, c.SearchEvidence):
                log.append(
                    "agent",
                    c.LogAction.SOURCE_CALL,
                    actor_id=evidence.system.value,
                    subject_ids=[result.transaction_id],
                    detail={"query": evidence.query, "returned": evidence.result_ids},
                    ts=(ts := ts + step),
                )
        log.append(
            "agent",
            c.LogAction.CHECK_RECORDED,
            actor_id="claude-sonnet-5-5",
            subject_ids=[result.id, result.transaction_id],
            detail={"status": result.status.value},
            ts=(ts := ts + step),
        )
    for draft in drafts:
        log.append(
            "agent",
            c.LogAction.DRAFT_CREATED,
            subject_ids=[draft.id, *draft.result_ids],
            ts=(ts := ts + step),
        )
    for packet in packets:
        log.append(
            "agent",
            c.LogAction.PACKET_ASSEMBLED,
            subject_ids=[packet.id] + ([packet.transaction_id] if packet.transaction_id else []),
            ts=(ts := ts + step),
        )
    log.append("system", c.LogAction.RUN_FINISHED, ts=ts + step)


def _delta_draft(run_id: str) -> c.FollowUpDraft:
    return c.FollowUpDraft(
        id=f"{run_id}:draft:1",
        run_id=run_id,
        channel="slack",
        to="Sam Okafor (@sam)",
        body="Hi Sam, the auditors need the receipt for the Delta Air Lines charge of "
        "$612.40 on December 11, 2025. Could you upload it to Ramp? Thanks.",
        asks_for="Receipt for Delta Air Lines, $612.40, 2025-12-11",
        result_ids=["ramp:txn:t_2002#receipt"],
    )


def build_preflight_run(run_dir: Path, sources: c.LocalSources) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    results = _results(PREFLIGHT_RUN_ID)
    drafts = [_delta_draft(PREFLIGHT_RUN_ID)]
    c.write_json(run_dir / c.RUN_MANIFEST, _manifest(PREFLIGHT_RUN_ID, "preflight", sources))
    c.write_jsonl(run_dir / c.RESULTS, results)
    c.write_jsonl(run_dir / c.DRAFTS, drafts)
    _write_log(run_dir, PREFLIGHT_RUN_ID, results, drafts, [])
    return run_dir


def build_sample_run(run_dir: Path, sources: c.LocalSources) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = SAMPLE_RUN_ID
    sampled = {"ramp:bill:b_1001", "ramp:txn:t_2002"}
    results = [r for r in _results(run_id) if r.transaction_id in sampled]
    draft = _delta_draft(run_id)
    catering = c.FollowUpDraft(
        id=f"{run_id}:draft:2",
        run_id=run_id,
        channel="email",
        to="dana@larkspur.example",
        subject="Audit sample line 3: Bayside Events catering",
        body="Hi Dana, the auditors picked a $2,400.00 catering charge from Bayside Events "
        "on December 18, 2025. We cannot find it in Ramp or QuickBooks. Do you know "
        "how it was paid?",
        asks_for="Where the Bayside Events catering charge was recorded",
    )
    northwind = "ramp:bill:b_1001"
    packets = [
        c.EvidencePacket(
            id=f"{run_id}:packet:1",
            run_id=run_id,
            sample_id="s_2026_01",
            sample_line=1,
            transaction_id=northwind,
            items=[
                c.PacketItem(role="receipt_or_invoice", evidence=_doc("doc_inv_2041", "Total due: $12,000.00")),
                c.PacketItem(role="approval", evidence=_rec("transaction", northwind, "approvals.0.approver_id", "p_cfo")),
                c.PacketItem(role="ledger_entry", evidence=_rec("ledger_entry", "qbo:Bill:147", "txn_date", "2026-01-08")),
                c.PacketItem(role="policy", evidence=_doc("doc_policy", "Vendor bills of $5,000.00 or more need CFO approval.")),
            ],
            missing=["Bank statement line for the payment (no bank statement in the dataset)"],
            result_ids=[r.id for r in results if r.transaction_id == northwind],
        ),
        c.EvidencePacket(
            id=f"{run_id}:packet:2",
            run_id=run_id,
            sample_id="s_2026_01",
            sample_line=2,
            transaction_id="ramp:txn:t_2002",
            items=[
                c.PacketItem(role="ledger_entry", evidence=_rec("ledger_entry", "qbo:Purchase:91", "total_cents", "61240")),
                c.PacketItem(role="receipt_or_invoice", evidence=c.SearchEvidence(system="ramp", query="Attachments on transaction t_2002", result_ids=[])),
            ],
            missing=["Receipt"],
            result_ids=[r.id for r in results if r.transaction_id == "ramp:txn:t_2002"],
            draft_ids=[draft.id],
        ),
        c.EvidencePacket(
            id=f"{run_id}:packet:3",
            run_id=run_id,
            sample_id="s_2026_01",
            sample_line=3,
            transaction_id=None,
            items=[
                c.PacketItem(role="other", evidence=c.SearchEvidence(system="ramp", query="Transactions from Bayside Events, $2,400.00, December 2025", result_ids=[])),
                c.PacketItem(role="other", evidence=c.SearchEvidence(system="qbo", query="Entries for Bayside Events or $2,400.00 in December 2025", result_ids=[])),
            ],
            missing=["The transaction itself: no match in Ramp or QuickBooks"],
            draft_ids=[catering.id],
        ),
    ]  # fmt: skip
    c.write_json(
        run_dir / c.RUN_MANIFEST, _manifest(run_id, "sample", sources, sample_id="s_2026_01")
    )
    c.write_jsonl(run_dir / c.RESULTS, results)
    c.write_jsonl(run_dir / c.DRAFTS, [draft, catering])
    c.write_jsonl(run_dir / c.PACKETS, packets)
    _write_log(run_dir, run_id, results, [draft, catering], packets)
    return run_dir


def build_demo(root: Path) -> tuple[Path, Path, Path]:
    """Write <root>/dataset, <root>/runs/preflight and <root>/runs/sample, and validate them."""
    root = Path(root)
    dataset = build_dataset(root / "dataset")
    problems = c.validate_dataset(dataset)
    if problems:
        raise ValueError(f"demo dataset is invalid: {problems}")
    sources = c.LocalSources(dataset)
    runs = (
        build_preflight_run(root / "runs" / "preflight", sources),
        build_sample_run(root / "runs" / "sample", sources),
    )
    for run in runs:
        problems = c.validate_run(run, sources)
        if problems:
            raise ValueError(f"demo run {run.name} is invalid: {problems}")
    return dataset, runs[0], runs[1]
