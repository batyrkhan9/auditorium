"""Pair each transaction with the ledger entry that records it.

The sources give no link between a card-platform transaction and its accounting
entry, so the pairing is worked out here, for all transactions at once. Doing
it one-to-one matters: a monthly bill for the same amount must not claim last
month's entry, and an entry left over after everything is paired is how a
duplicate shows up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from auditorium_contracts import LedgerEntry, LedgerEntryType, SpendTransaction

_SUFFIXES = {"inc", "llc", "llp", "ltd", "corp", "co", "company", "incorporated", "the"}
_RECORDING_TYPES = (LedgerEntryType.BILL, LedgerEntryType.EXPENSE, LedgerEntryType.VENDOR_CREDIT)

# How a pair was made, strongest first.
INVOICE_NUMBER = "invoice_number"  # same vendor, same invoice number, same amount
VENDOR_AND_AMOUNT = "vendor_and_amount"  # same vendor, same amount
AMOUNT_DIFFERS = "amount_differs"  # same vendor, same invoice number or close date, other amount
VENDOR_DIFFERS = "vendor_differs"  # same invoice number and amount, other vendor
VENDOR_UNCLEAR = "vendor_unclear"  # same amount within days, other vendor: only a candidate
# An unclear vendor, settled afterwards (see Books): the attached receipt names the business
# the books use, or the model reader judged the two names to be one business.
VENDOR_BY_DOCUMENT = "vendor_by_document"
VENDOR_BY_MODEL = "vendor_by_model"
NONE = "none"

AMOUNT_DIFFERS_WITHIN_DAYS = 10
VENDOR_UNCLEAR_WITHIN_DAYS = 3


@dataclass(frozen=True)
class Match:
    entry: LedgerEntry | None
    how: str
    duplicates: tuple[LedgerEntry, ...] = ()
    support: tuple = ()  # evidence that settled an unclear vendor
    read_by: str | None = None  # the model that judged the names, if one did


def vendor_key(name: str | None) -> str:
    """A vendor name reduced to what identifies it: lower case, no punctuation, no 'LLC'."""
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and ")).split()
    return " ".join(word for word in words if word not in _SUFFIXES)


def same_vendor(a: str | None, b: str | None) -> bool:
    key_a, key_b = vendor_key(a), vendor_key(b)
    return bool(key_a) and bool(key_b) and (key_a in key_b or key_b in key_a)


def same_document(a: str | None, b: str | None) -> bool:
    """True when two invoice numbers are the same one, ignoring punctuation and case."""
    key_a, key_b = (re.sub(r"[^a-z0-9]", "", (n or "").lower()) for n in (a, b))
    return bool(key_a) and key_a == key_b


def anchor_date(txn: SpendTransaction) -> date:
    return txn.accounting_date or txn.transaction_date


def match_ledger(
    transactions: list[SpendTransaction], ledger: list[LedgerEntry]
) -> dict[str, Match]:
    """One Match per transaction id. Each ledger entry is paired with at most one transaction."""
    entries = [entry for entry in ledger if entry.entry_type in _RECORDING_TYPES]
    free_txns = {txn.id: txn for txn in transactions}
    free_entries = {entry.id: entry for entry in entries}
    matches: dict[str, Match] = {}

    def gap(txn, entry):
        return abs((entry.txn_date - anchor_date(txn)).days)

    def same_amount(txn, entry):
        return abs(entry.total_cents) == abs(txn.amount_cents)

    def pair_up(how, wanted, rank=gap):
        """Pair free transactions with free entries that satisfy `wanted`, best-ranked first."""
        pairs = [
            (rank(txn, entry), txn.id, entry.id)
            for txn in free_txns.values()
            for entry in free_entries.values()
            if wanted(txn, entry)
        ]
        for _, txn_id, entry_id in sorted(pairs):
            if txn_id in free_txns and entry_id in free_entries:
                txn, entry = free_txns.pop(txn_id), free_entries.pop(entry_id)
                matches[txn_id] = Match(entry, how(txn, entry) if callable(how) else how)

    def same_invoice(txn, entry):
        return same_vendor(txn.vendor_name, entry.vendor_name) and same_document(
            txn.invoice_number, entry.doc_number
        )

    pair_up(
        lambda txn, entry: INVOICE_NUMBER if same_amount(txn, entry) else AMOUNT_DIFFERS,
        same_invoice,
        rank=lambda txn, entry: (not same_amount(txn, entry), gap(txn, entry)),
    )
    pair_up(
        VENDOR_AND_AMOUNT,
        lambda txn, entry: (
            same_vendor(txn.vendor_name, entry.vendor_name) and same_amount(txn, entry)
        ),
    )
    pair_up(
        VENDOR_DIFFERS,
        lambda txn, entry: (
            same_document(txn.invoice_number, entry.doc_number) and same_amount(txn, entry)
        ),
    )
    pair_up(
        AMOUNT_DIFFERS,
        lambda txn, entry: (
            same_vendor(txn.vendor_name, entry.vendor_name)
            and gap(txn, entry) <= AMOUNT_DIFFERS_WITHIN_DAYS
        ),
        rank=lambda txn, entry: (gap(txn, entry), abs(entry.total_cents - txn.amount_cents)),
    )

    # Same amount, close date, different vendor name: offered only when there is no other
    # candidate on either side, and reported as needs_review, never as a failure.
    loose = [
        (txn, entry)
        for txn in free_txns.values()
        for entry in free_entries.values()
        if same_amount(txn, entry) and gap(txn, entry) <= VENDOR_UNCLEAR_WITHIN_DAYS
    ]
    for txn, entry in loose:
        alone = sum(t is txn for t, _ in loose) == 1 and sum(e is entry for _, e in loose) == 1
        if alone:
            matches[txn.id] = Match(entry, VENDOR_UNCLEAR)
            free_txns.pop(txn.id)
            free_entries.pop(entry.id)

    for txn_id in free_txns:
        matches[txn_id] = Match(None, NONE)

    # An entry nobody claimed that repeats a claimed one is a duplicate of it.
    by_id = {txn.id: txn for txn in transactions}
    for extra in free_entries.values():
        for txn_id, match in matches.items():
            txn, entry = by_id[txn_id], match.entry
            if entry is None or match.how == VENDOR_UNCLEAR:
                continue
            repeats = (
                same_vendor(extra.vendor_name, entry.vendor_name)
                and abs(extra.total_cents) == abs(entry.total_cents)
                and (
                    same_document(extra.doc_number, txn.invoice_number)
                    or extra.txn_date == entry.txn_date
                )
            )
            if repeats:
                matches[txn_id] = Match(entry, match.how, (*match.duplicates, extra))
                break
    return matches


def find_accruals(
    txn: SpendTransaction, ledger: list[LedgerEntry], start: date, end: date
) -> list[LedgerEntry]:
    """Journal entries dated from `start` to `end` that name this transaction's vendor."""
    key = vendor_key(txn.vendor_name)
    found = []
    for entry in ledger:
        if (
            entry.entry_type is not LedgerEntryType.JOURNAL_ENTRY
            or not start <= entry.txn_date <= end
        ):
            continue
        texts = [entry.vendor_name, entry.memo]
        for line in entry.lines:
            texts += [line.description, line.vendor_name]
        if key and any(key in vendor_key(text) for text in texts if text):
            found.append(entry)
    return found
