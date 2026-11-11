"""Work out which transaction an auditor's sample line means.

Auditors pick from the ledger, not from the card platform, and write what they
see there: a vendor, an amount, a date, sometimes a reference. So a line is
compared with both sides of each pairing, the Ramp transaction and the ledger
entry that records it. Every detail the auditor gave must agree with one side
or the other. If more than one transaction fits, none is chosen.
"""

from __future__ import annotations

from auditorium_contracts import SampleItem, SpendTransaction

from .books import Books
from .matching import same_document, same_vendor


def _fits(item: SampleItem, txn: SpendTransaction, books: Books) -> bool:
    entry = books.matches[txn.id].entry
    if item.reference:
        references = [txn.invoice_number, txn.source.record_id, entry.doc_number if entry else None]
        if not any(same_document(item.reference, known) for known in references):
            return False
    if item.vendor_name:
        vendors = [txn.vendor_name, entry.vendor_name if entry else None]
        if not any(same_vendor(item.vendor_name, known) for known in vendors):
            return False
    if item.amount_cents is not None:
        amounts = [txn.amount_cents, entry.total_cents if entry else None]
        if abs(item.amount_cents) not in [abs(known) for known in amounts if known is not None]:
            return False
    if item.transaction_date:
        dates = [txn.transaction_date, txn.accounting_date, entry.txn_date if entry else None]
        if item.transaction_date not in dates:
            return False
    return True


def resolve(item: SampleItem, books: Books) -> tuple[SpendTransaction | None, str | None]:
    """The transaction this line means, or None with the reason it could not be pinned down."""
    if not (item.reference or item.vendor_name or item.amount_cents is not None):
        return None, "The request gives no vendor, amount or reference to look for."
    fitting = [txn for txn in books.transactions if _fits(item, txn, books)]
    if len(fitting) == 1:
        return fitting[0], None
    if not fitting:
        return None, "No transaction matches the details in the request."
    return None, (
        f"{len(fitting)} transactions match the details in the request. A date or a reference "
        "would tell them apart."
    )
