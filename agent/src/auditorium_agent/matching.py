"""Find the ledger entries that belong to a transaction.

The sources give no link between a card-platform transaction and its accounting
entry, so it is matched on vendor and amount. This is the plain-code version;
the full `ledger_match` check will build on it.
"""

from __future__ import annotations

import re
from datetime import date

from auditorium_contracts import LedgerEntry, LedgerEntryType, SpendTransaction

_SUFFIXES = {"inc", "llc", "llp", "ltd", "corp", "co", "company", "incorporated", "the"}


def vendor_key(name: str | None) -> str:
    """A vendor name reduced to what identifies it: lower case, no punctuation, no 'LLC'."""
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and ")).split()
    return " ".join(word for word in words if word not in _SUFFIXES)


def same_vendor(a: str | None, b: str | None) -> bool:
    key_a, key_b = vendor_key(a), vendor_key(b)
    return bool(key_a) and bool(key_b) and (key_a in key_b or key_b in key_a)


def find_ledger_entry(txn: SpendTransaction, ledger: list[LedgerEntry]) -> LedgerEntry | None:
    """The bill or expense entry recording this transaction: same vendor, same total.

    When several qualify, the one dated closest to the transaction wins.
    """
    wanted = (LedgerEntryType.BILL, LedgerEntryType.EXPENSE)
    anchor = txn.accounting_date or txn.transaction_date
    candidates = [
        entry
        for entry in ledger
        if entry.entry_type in wanted
        and entry.total_cents == txn.amount_cents
        and same_vendor(entry.vendor_name, txn.vendor_name)
    ]
    return min(candidates, key=lambda e: abs((e.txn_date - anchor).days), default=None)


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
        texts = [entry.vendor_name, entry.memo, *(line.description for line in entry.lines)]
        if key and any(key in vendor_key(text) for text in texts if text):
            found.append(entry)
    return found
