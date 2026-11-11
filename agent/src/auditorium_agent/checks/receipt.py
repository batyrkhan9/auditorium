"""The receipt check: was a receipt or invoice required, is one attached, and is it the right one?

"Attached" is not enough. A file clipped to the wrong transaction passes a
checkbox and fails an audit, so the attached document is read and compared with
the transaction. Reasons for where the lines are drawn are in agent/decisions/.
"""

from __future__ import annotations

import json

from auditorium_contracts import (
    CheckName,
    CheckResult,
    CheckStatus,
    DocumentEvidence,
    IssueCode,
    RecordEvidence,
    SpendTransaction,
    TransactionKind,
    result_id,
)

from ..books import Books
from ..receipt_reader import Line, find_amount, find_name, lines_of, stated_total

# Best outcome first: with several attachments, one good one is enough.
_RANK = {CheckStatus.PASS: 0, CheckStatus.NEEDS_REVIEW: 1, CheckStatus.FAIL: 2}


def _money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def _quote(document_id: str, line: Line, note: str) -> DocumentEvidence:
    return DocumentEvidence(document_id=document_id, page=line.page, quote=line.text, note=note)


def _assess(txn: SpendTransaction, document_id: str, books: Books, what: str):
    """Compare one attached document with the transaction: status, issue, summary, rationale, evidence."""
    article = "An" if what == "invoice" else "A"
    pages = books.read_document(document_id)
    lines = lines_of(pages)
    amount = _money(txn.amount_cents)
    if not lines:
        document = books.documents[document_id]
        unreadable = RecordEvidence(
            record_type="document",
            record_id=document_id,
            field="media_type",
            value=document.media_type,
            note="The attached file. It has no readable text.",
        )
        return (
            CheckStatus.NEEDS_REVIEW,
            None,
            f"{article} {what} is attached but it has no readable text.",
            "The file cannot be compared with the transaction automatically. A person should open it.",
            [unreadable],
        )

    name = find_name(lines, txn.vendor_name, txn.invoice_number)
    shown = find_amount(lines, txn.amount_cents)
    total = stated_total(lines)
    read_by = None
    by_books = False
    entry = books.matches[txn.id].entry
    if not name and entry is not None and entry.vendor_name:
        # The receipt does not use the card statement's name. If it names the business the
        # books record this very charge under, that ties it to the transaction just as well.
        name = find_name(lines, entry.vendor_name, None)
        by_books = name is not None
    if shown and not name:
        # The amount is right but the name is not the card statement's. Whether two names
        # mean one business is a judgement plain code cannot make, so ask the model reader.
        asked = books.ask_merchant(document_id, txn.vendor_name, pages)
        if asked:
            name, read_by = asked
    evidence = []
    if name and by_books:
        evidence.append(
            _quote(
                document_id,
                name,
                "Names the business. The books record this charge under the same name.",
            )
        )
        evidence.append(
            RecordEvidence(
                record_type="ledger_entry",
                record_id=entry.id,
                field="vendor_name",
                value=entry.vendor_name,
                note="The vendor the books record this charge under.",
            )
        )
    elif name and read_by:
        note = (
            f"Names the business. {read_by} read it as the same merchant as "
            f"'{txn.vendor_name}' on the card statement."
        )
        evidence.append(_quote(document_id, name, note))
    elif name:
        evidence.append(_quote(document_id, name, "Ties the document to this vendor."))
    if shown:
        evidence.append(_quote(document_id, shown, "Shows the amount of this transaction."))

    if name and shown and by_books:
        return (
            CheckStatus.PASS,
            None,
            f"A {what} from {entry.vendor_name} for {amount} is attached. The card statement "
            f"shows the same business as '{txn.vendor_name}'.",
            f"The attached {what} shows the amount and names the business the books record "
            "this charge under. The card statement's name is a coded form of it.",
            evidence,
        )
    if name and shown and read_by:
        return (
            CheckStatus.PASS,
            None,
            f"{article} {what} for {amount} is attached. It names the merchant differently from "
            f"the card statement's '{txn.vendor_name}'.",
            f"The attached {what} shows the amount. Its merchant name differs from the card "
            f"statement's, and {read_by} read the two as the same business. The quoted line "
            "lets a reviewer confirm that.",
            evidence,
        )
    if name and shown:
        return (
            CheckStatus.PASS,
            None,
            f"{article} {what} from {txn.vendor_name} for {amount} is attached.",
            f"The attached {what} names the vendor and shows the amount of the transaction.",
            evidence,
        )
    if shown:
        return (
            CheckStatus.NEEDS_REVIEW,
            None,
            f"The attached {what} shows {amount} but does not name {txn.vendor_name}.",
            "Card statements often name a merchant differently from its own receipts, so this "
            "may be the right document. The amount alone does not prove it.",
            evidence,
        )

    if total:
        evidence.append(_quote(document_id, total[0], "What the document says its total is."))
    states = f"shows {_money(total[1])}" if total else "shows no amount"
    if name and txn.kind is TransactionKind.CARD_EXPENSE:
        return (
            CheckStatus.NEEDS_REVIEW,
            None,
            f"The attached receipt is from {entry.vendor_name if by_books else txn.vendor_name} "
            f"but {states}, not the {amount} charged.",
            "A tip, tax or currency conversion can make a card charge differ from the receipt. "
            "A person should confirm the difference is one of those.",
            evidence,
        )
    if name:
        return (
            CheckStatus.FAIL,
            IssueCode.RECEIPT_MISMATCH,
            f"The attached invoice is from {txn.vendor_name} but {states}, not {amount}.",
            "A bill is paid for the amount on its invoice. This invoice is for a different "
            "amount, so it does not support this bill.",
            evidence,
        )
    if not evidence:
        evidence.append(_quote(document_id, lines[0], "The first line of the attached document."))
    else:
        evidence.insert(
            0, _quote(document_id, lines[0], "The first line of the attached document.")
        )
    return (
        CheckStatus.FAIL,
        IssueCode.RECEIPT_MISMATCH,
        f"The attached {what} is for something else: it does not name {txn.vendor_name} "
        f"and {states}, not {amount}.",
        f"Neither the vendor nor the amount on the attached {what} matches this transaction.",
        evidence,
    )


def check_receipt(txn: SpendTransaction, books: Books, run_id: str) -> CheckResult:
    def result(status, summary, rationale, evidence, issue=None):
        return CheckResult(
            id=result_id(txn.id, CheckName.RECEIPT),
            run_id=run_id,
            transaction_id=txn.id,
            check=CheckName.RECEIPT,
            status=status,
            issue_codes=[issue] if issue else [],
            summary=summary,
            rationale=rationale,
            evidence=evidence,
        )

    def from_ramp(field, value, note):
        return RecordEvidence(
            record_type="transaction", record_id=txn.id, field=field, value=value, note=note
        )

    is_bill = txn.kind is TransactionKind.VENDOR_BILL
    what = "invoice" if is_bill else "receipt"
    article = "An" if is_bill else "A"
    amount = _money(txn.amount_cents)
    policy = books.policy
    if txn.amount_cents < 0:
        # Money coming back. The receipt rule is about what the company spent.
        return result(
            CheckStatus.NOT_APPLICABLE,
            f"This is a refund of {amount} from {txn.vendor_name}, not a purchase, so no "
            f"{what} is required.",
            "The receipt requirement applies to money spent. A negative amount is money "
            "returned. Whether the books record the refund is the ledger_match check's job.",
            [from_ramp("amount_cents", str(txn.amount_cents), "Negative: a refund or credit.")],
        )
    threshold = policy.receipt_required_from_cents
    if is_bill:
        required = [from_ramp("kind", txn.kind.value, "A vendor bill always needs its invoice.")]
    else:
        required = [
            from_ramp("amount_cents", str(txn.amount_cents), "The amount charged."),
            RecordEvidence(
                record_type="policy",
                record_id=policy.id,
                field="receipt_required_from_cents",
                value=str(threshold),
                note="Company policy: a receipt is required at or above this amount.",
            ),
        ]

    if not is_bill and abs(txn.amount_cents) < threshold:
        return result(
            CheckStatus.NOT_APPLICABLE,
            f"No receipt is required: {amount} is under the {_money(threshold)} threshold.",
            "Company policy requires a receipt only at or above the threshold.",
            required,
        )

    attached = [document_id for document_id in txn.document_ids if document_id in books.documents]
    if not attached:
        listed = from_ramp(
            "document_ids", json.dumps(txn.document_ids), "The documents attached in Ramp."
        )
        return result(
            CheckStatus.FAIL,
            f"No {what} is attached to this {amount} {'bill' if is_bill else 'card charge'} "
            f"from {txn.vendor_name}.",
            f"{article} {what} is required and Ramp holds no document for this transaction.",
            [*required, listed],
            IssueCode.RECEIPT_MISSING,
        )

    status, issue, summary, rationale, evidence = min(
        (_assess(txn, document_id, books, what) for document_id in attached),
        key=lambda assessed: _RANK[assessed[0]],
    )
    return result(status, summary, rationale, [*evidence, *required], issue)
