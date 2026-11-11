"""Read a receipt or invoice well enough to tell whether it belongs to a transaction.

Plain code, like service_period.py: it finds the line that names the vendor, the
line that shows the amount, and the document's own stated total. Where these
cannot tie a receipt to its charge, the receipt check tries the name the books
use, and then the model reader (reader.py) if one is in use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .matching import vendor_key

_AMOUNT = re.compile(
    r"(?<![\d.,])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?!\d)|\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?![\d.,])"
)
_TOTAL_LABEL = re.compile(
    r"\b(total|amount due|balance due|amount charged|charged)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Line:
    page: int  # 1-based
    text: str


def lines_of(pages: tuple[str, ...]) -> list[Line]:
    return [
        Line(number, text.strip())
        for number, page in enumerate(pages, 1)
        for text in page.splitlines()
        if text.strip()
    ]


def amounts_on(text: str) -> list[int]:
    """Every money amount on a line, in cents."""
    found = []
    for match in _AMOUNT.finditer(text):
        dollars, cents = (match[1], match[2]) if match[1] else (match[3], "00")
        found.append(int(dollars.replace(",", "")) * 100 + int(cents))
    return found


def find_amount(lines: list[Line], cents: int) -> Line | None:
    """The line showing exactly this amount, preferring one labelled as a total."""
    showing = [line for line in lines if abs(cents) in amounts_on(line.text)]
    labelled = [line for line in showing if _TOTAL_LABEL.search(line.text)]
    return (labelled or showing or [None])[0]


def stated_total(lines: list[Line]) -> tuple[Line, int] | None:
    """What the document itself says the total is: the last labelled total, else the largest amount."""
    labelled = [(line, amounts_on(line.text)) for line in lines if _TOTAL_LABEL.search(line.text)]
    labelled = [(line, amounts) for line, amounts in labelled if amounts]
    if labelled:
        line, amounts = labelled[-1]
        return line, amounts[-1]
    priced = [(line, max(amounts_on(line.text))) for line in lines if amounts_on(line.text)]
    return max(priced, key=lambda pair: pair[1], default=None)


def find_name(lines: list[Line], vendor: str, invoice_number: str | None) -> Line | None:
    """The line that ties the document to the transaction: the vendor's name or the invoice number."""
    key = vendor_key(vendor)
    for line in lines:
        if key and key in vendor_key(line.text):
            return line
    number = re.sub(r"[^a-z0-9]", "", (invoice_number or "").lower())
    if len(number) >= 3:
        for line in lines:
            if number in re.sub(r"[^a-z0-9]", "", line.text.lower()):
                return line
    return None
