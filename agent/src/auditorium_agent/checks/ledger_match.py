"""The ledger match check: do the books record this transaction, once, as it happened?

The pairing itself is done for all transactions at once in matching.py. This
file turns each transaction's pairing into a result with evidence from both
systems, so a reviewer can put the two records side by side.
"""

from __future__ import annotations

from auditorium_contracts import (
    CheckName,
    CheckResult,
    CheckStatus,
    IssueCode,
    RecordEvidence,
    SourceSystem,
    SpendTransaction,
    TransactionKind,
    result_id,
)

from .. import matching
from ..books import Books


def _money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def check_ledger_match(txn: SpendTransaction, books: Books, run_id: str) -> CheckResult:
    def result(status, summary, rationale, evidence, issue=None):
        return CheckResult(
            id=result_id(txn.id, CheckName.LEDGER_MATCH),
            run_id=run_id,
            transaction_id=txn.id,
            check=CheckName.LEDGER_MATCH,
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

    def from_books(entry, field, value, note):
        return RecordEvidence(
            record_type="ledger_entry", record_id=entry.id, field=field, value=value, note=note
        )

    match = books.matches[txn.id]
    entry = match.entry
    what = "bill" if txn.kind is TransactionKind.VENDOR_BILL else "card charge"
    amount = _money(txn.amount_cents)
    evidence = [
        from_ramp("amount_cents", str(txn.amount_cents), "The amount in Ramp."),
        from_ramp("vendor_name", txn.vendor_name, "The vendor in Ramp."),
    ]

    if entry is None:
        day = matching.anchor_date(txn)
        evidence.append(
            books.search(
                SourceSystem.QBO,
                f"bill, expense or credit entries for vendor '{txn.vendor_name}', or for "
                f"{amount} within {matching.VENDOR_UNCLEAR_WITHIN_DAYS} days of {day}",
                [],
            )
        )
        return result(
            CheckStatus.FAIL,
            f"The books have no entry for this {amount} {what} from {txn.vendor_name}.",
            "No bill, expense or credit entry has this vendor, and none has this amount near "
            "this date under another name.",
            evidence,
            IssueCode.LEDGER_MATCH_MISSING,
        )

    evidence.append(
        from_books(entry, "total_cents", str(entry.total_cents), "The amount in the books.")
    )
    if entry.vendor_name:
        evidence.append(
            from_books(entry, "vendor_name", entry.vendor_name, "The vendor in the books.")
        )
    if entry.doc_number and matching.same_document(txn.invoice_number, entry.doc_number):
        evidence.append(
            from_books(entry, "doc_number", entry.doc_number, "The same invoice number.")
        )

    if match.how == matching.VENDOR_BY_DOCUMENT:
        return result(
            CheckStatus.PASS,
            f"The books record this {amount} {what} once, under '{entry.vendor_name}', the "
            f"name on its attached receipt. The card statement shows '{txn.vendor_name}'.",
            "Amount and date agree between Ramp and the books. The vendor names differ, but "
            "the receipt attached to the charge names the business the books use, so the "
            "statement's name is a coded form of it.",
            [*evidence, *match.support],
        )
    if match.how == matching.VENDOR_BY_MODEL:
        return result(
            CheckStatus.PASS,
            f"The books record this {amount} {what} once, under '{entry.vendor_name}'. The "
            f"card statement shows '{txn.vendor_name}'.",
            f"Amount and date agree between Ramp and the books. No document was available to "
            f"tie the two names together, so {match.read_by} was asked, and read them as the "
            "same business. Both names are quoted above for a reviewer to judge.",
            evidence,
        )
    if match.how == matching.VENDOR_UNCLEAR:
        return result(
            CheckStatus.NEEDS_REVIEW,
            f"No entry under {txn.vendor_name}. An entry for the same {amount} on "
            f"{entry.txn_date} is recorded under '{entry.vendor_name}'.",
            "The amount and date line up but the vendor names do not, and nothing else ties "
            "the two together. A person should confirm whether this entry is the same purchase.",
            evidence,
            IssueCode.LEDGER_MATCH_VENDOR,
        )
    if match.how == matching.VENDOR_DIFFERS:
        return result(
            CheckStatus.FAIL,
            f"The books record this {what} under '{entry.vendor_name}', not {txn.vendor_name}.",
            "The invoice number and the amount are the same in both systems, so this is the "
            "same bill. The vendor it is booked to is different.",
            evidence,
            IssueCode.LEDGER_MATCH_VENDOR,
        )
    if match.how == matching.AMOUNT_DIFFERS:
        difference = _money(entry.total_cents - txn.amount_cents)
        return result(
            CheckStatus.FAIL,
            f"Ramp has {amount} and the books have {_money(entry.total_cents)}, a difference "
            f"of {difference}.",
            "The vendor matches and the entry is this transaction's, but the amount recorded "
            "is not the amount spent.",
            evidence,
            IssueCode.LEDGER_MATCH_AMOUNT,
        )
    if match.duplicates:
        for extra in match.duplicates:
            evidence.append(
                from_books(
                    extra,
                    "total_cents",
                    str(extra.total_cents),
                    f"A second entry for the same amount, dated {extra.txn_date}.",
                )
            )
        ids = [entry.id, *(extra.id for extra in match.duplicates)]
        evidence.append(
            books.search(
                SourceSystem.QBO,
                f"bill and expense entries for vendor '{txn.vendor_name}' totalling {amount}, "
                "compared with the transactions for that vendor and amount",
                ids,
            )
        )
        return result(
            CheckStatus.FAIL,
            f"The books record this {amount} {what} from {txn.vendor_name} {len(ids)} times.",
            "One transaction happened. Every other transaction for this vendor and amount "
            "already has its own entry, so the extra entry repeats this one.",
            evidence,
            IssueCode.LEDGER_MATCH_DUPLICATE,
        )
    return result(
        CheckStatus.PASS,
        f"The books record this {amount} {what} once, under the same vendor, dated "
        f"{entry.txn_date}.",
        "Vendor and amount agree between Ramp and the books, and no second entry repeats it.",
        evidence,
    )
