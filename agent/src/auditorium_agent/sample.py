"""Sample mode: the auditor picked transactions; line up the evidence for each one.

For every line of the request this finds the transaction, runs the four checks
on it, gathers the documents and records an auditor asks to see into one
packet, says what is missing, and drafts a message for anything a coworker can
supply.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from auditorium_contracts import (
    DRAFTS,
    PACKETS,
    RESULTS,
    RUN_MANIFEST,
    CheckName,
    CheckResult,
    DatasetManifest,
    DocumentEvidence,
    DocumentType,
    EvidencePacket,
    FollowUpDraft,
    IssueCode,
    LogAction,
    PacketItem,
    RecordEvidence,
    SampleRequest,
    Sources,
    SpendTransaction,
    TransactionKind,
    verify_evidence,
    write_json,
    write_jsonl,
)

from .books import Books
from .drafts import drafts_for
from .matching import vendor_key
from .preflight import CHECKS, _verified
from .reader import Reader
from .receipt_reader import amounts_on, lines_of
from .resolve import resolve
from .runs import open_run, run_manifest

# What a failed check means is missing from the packet, in an auditor's words.
_MISSING = {
    IssueCode.RECEIPT_MISSING: "receipt or invoice",
    IssueCode.RECEIPT_MISMATCH: "the right receipt or invoice (the attached one is for something else)",
    IssueCode.APPROVAL_MISSING: "approval",
    IssueCode.LEDGER_MATCH_MISSING: "ledger entry",
}


def _payment(txn: SpendTransaction, books: Books) -> DocumentEvidence | None:
    """The bank statement line that shows this bill being paid: same vendor, same amount."""
    key = vendor_key(txn.vendor_name)
    for document in books.documents.values():
        if document.type is not DocumentType.BANK_STATEMENT:
            continue
        for line in lines_of(books.read_document(document.id)):
            if abs(txn.amount_cents) in amounts_on(line.text) and key in vendor_key(line.text):
                return DocumentEvidence(
                    document_id=document.id,
                    page=line.page,
                    quote=line.text,
                    note="The payment leaving the bank.",
                )
    return None


def _packet_items(results: list[CheckResult]) -> list[PacketItem]:
    """Pick out of the checks' evidence the things an auditor asks to see, each under its role."""
    items = []
    for result in results:
        for evidence in result.evidence:
            role = None
            if result.check is CheckName.RECEIPT and isinstance(evidence, DocumentEvidence):
                role = "receipt_or_invoice"
            elif isinstance(evidence, RecordEvidence):
                if result.check is CheckName.APPROVAL and evidence.field.startswith("approvals."):
                    role = "approval"
                elif result.check is CheckName.APPROVAL and evidence.record_type == "policy":
                    role = "policy"
                elif (
                    result.check is CheckName.LEDGER_MATCH
                    and evidence.record_type == "ledger_entry"
                ):
                    role = "ledger_entry"
            if role:
                items.append(PacketItem(role=role, evidence=evidence))
    return items


def run_sample(
    sources: Sources,
    request: SampleRequest,
    out_dir: Path | str,
    *,
    fiscal_year_start: date,
    fiscal_year_end: date,
    run_id: str | None = None,
    dataset_id: str | None = None,
    dataset_path: Path | str | None = None,
    dataset_manifest: DatasetManifest | None = None,
    reader: Reader | None = None,
) -> tuple[list[EvidencePacket], list[FollowUpDraft]]:
    out_dir = Path(out_dir)
    log, books, started, run_id = open_run(
        sources,
        out_dir,
        detail={"mode": "sample", "sample_id": request.id},
        fiscal_year_start=fiscal_year_start,
        fiscal_year_end=fiscal_year_end,
        run_id=run_id,
        dataset_id=dataset_id,
        reader=reader,
        dataset_manifest=dataset_manifest,
    )

    results: dict[str, CheckResult] = {}
    drafts: dict[str, FollowUpDraft] = {}
    packets = []
    for item in request.items:
        packet_id = f"{request.id}:{item.line}"
        txn, reason = resolve(item, books)
        if txn is None:
            packet = EvidencePacket(
                id=packet_id,
                run_id=run_id,
                sample_id=request.id,
                sample_line=item.line,
                missing=[reason],
            )
        else:
            checked = []
            for check in CHECKS:
                result_id = f"{txn.id}#{check.__name__.removeprefix('check_')}"
                if result_id not in results:  # two lines can mean the same transaction
                    result = _verified(check(txn, books, run_id), books)
                    if result is None:
                        continue
                    results[result.id] = result
                    log.append(
                        "agent",
                        LogAction.CHECK_RECORDED,
                        subject_ids=[result.id, txn.id],
                        detail={"check": result.check.value, "status": result.status.value},
                    )
                checked.append(results[result_id])

            items = _packet_items(checked)
            missing = [
                _MISSING[code]
                for result in checked
                for code in result.issue_codes
                if code in _MISSING
            ]
            if txn.kind is TransactionKind.VENDOR_BILL and txn.paid_date:
                payment = _payment(txn, books)
                if payment and verify_evidence(payment, books.index) is None:
                    items.append(PacketItem(role="payment", evidence=payment))
                else:
                    missing.append("payment on a bank statement")

            written = drafts_for(txn, checked, books, run_id)
            for draft in written:
                if draft.id not in drafts:
                    drafts[draft.id] = draft
                    log.append(
                        "agent",
                        LogAction.DRAFT_CREATED,
                        subject_ids=[draft.id, txn.id],
                        detail={"channel": draft.channel, "to": draft.to, "sent": False},
                    )
            packet = EvidencePacket(
                id=packet_id,
                run_id=run_id,
                sample_id=request.id,
                sample_line=item.line,
                transaction_id=txn.id,
                items=items,
                missing=missing,
                result_ids=[result.id for result in checked],
                draft_ids=[draft.id for draft in written],
            )
        packets.append(packet)
        log.append(
            "agent",
            LogAction.PACKET_ASSEMBLED,
            subject_ids=[packet.id, *([txn.id] if txn else [])],
            detail={"items": len(packet.items), "missing": packet.missing},
        )

    write_jsonl(out_dir / RESULTS, results.values())
    write_jsonl(out_dir / PACKETS, packets)
    write_jsonl(out_dir / DRAFTS, drafts.values())
    log.append(
        "system",
        LogAction.RUN_FINISHED,
        detail={"packets": len(packets), "drafts": len(drafts), "results": len(results)},
    )
    write_json(
        out_dir / RUN_MANIFEST,
        run_manifest(
            sources,
            books,
            run_id=run_id,
            mode="sample",
            started=started,
            dataset_id=dataset_id,
            dataset_path=dataset_path,
            sample_id=request.id,
        ),
    )
    return packets, list(drafts.values())
