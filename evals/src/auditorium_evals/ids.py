"""Live ids back to dataset ids.

The answer key is keyed on the dataset's transaction ids. A bill read live from
Ramp keeps Ramp's id; the loaders stamp `auditorium-ref: <dataset id>` into the
memo (Ramp) or PrivateNote (QuickBooks), and `raw` keeps it
(connectors/decisions/2026-11-10-ramp-bill-loader.md). Transactions served from the
dataset already carry the dataset id and have no stamp.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from auditorium_contracts import SpendTransaction

REF = re.compile(r"auditorium-ref: (\S+)")


def stamped_ref(raw: Any) -> str | None:
    """The dataset id stamped anywhere in a record's raw payload, if any."""
    if raw is None:
        return None
    if isinstance(raw, str):
        found = REF.search(raw)
        return found.group(1) if found else None
    if isinstance(raw, dict):
        # The memo fields first: that is where the loaders put the stamp.
        for key in ("memo", "PrivateNote"):
            if (found := stamped_ref(raw.get(key))) is not None:
                return found
        values: Iterable[Any] = raw.values()
    elif isinstance(raw, list | tuple):
        values = raw
    else:
        return None
    for value in values:
        if (found := stamped_ref(value)) is not None:
            return found
    return None


def transaction_id_map(transactions: Iterable[SpendTransaction]) -> dict[str, str]:
    """Run transaction id -> dataset transaction id, for every transaction that differs."""
    mapping = {}
    for txn in transactions:
        ref = stamped_ref(txn.raw)
        if ref and ref != txn.id:
            mapping[txn.id] = ref
    return mapping
