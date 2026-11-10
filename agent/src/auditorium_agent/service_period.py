"""Find when the work was done, from the words on an invoice.

This is the plain-code version: it looks for a labelled line and reads the dates
on it. It is deliberately strict. When it finds nothing it says so, and the
period check falls back to `needs_review` rather than guessing. A model-based
reader replaces `find_service_period` later, behind the same return type.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS |= {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}
_MONTH = r"(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"

_LABEL = re.compile(
    r"services? (rendered|performed|provided)|service period|period of service"
    r"|billing period|work performed|for the month of",
    re.IGNORECASE,
)
_LONG_DATE = re.compile(
    _MONTH + r"\s+(?P<day>\d{1,2})(?!\d)(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?", re.IGNORECASE
)
_NUMERIC_DATE = re.compile(r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})")
_MONTH_YEAR = re.compile(_MONTH + r"\s+(?P<year>\d{4})", re.IGNORECASE)


@dataclass(frozen=True)
class ServicePeriod:
    start: date
    end: date
    page: int  # 1-based
    quote: str  # the line it was read from, as it appears in the document
    read_by: str | None = None  # the model that read the dates, when plain code could not


def _dates_on(line: str) -> list[date]:
    found = [date(int(m["y"]), int(m["m"]), int(m["d"])) for m in _NUMERIC_DATE.finditer(line)]
    long_dates = list(_LONG_DATE.finditer(line))
    years = [int(m["year"]) for m in long_dates if m["year"]]
    if long_dates and years:
        for match in long_dates:
            # "December 1 - December 31, 2025": the first date borrows the year.
            year = int(match["year"]) if match["year"] else years[-1]
            found.append(date(year, _MONTHS[match["month"].lower()], int(match["day"])))
    if not found:
        for match in _MONTH_YEAR.finditer(line):
            year, month = int(match["year"]), _MONTHS[match["month"].lower()]
            found.append(date(year, month, 1))
            found.append(date(year, month, calendar.monthrange(year, month)[1]))
    return found


def find_service_period(pages: tuple[str, ...]) -> ServicePeriod | None:
    """The first labelled service period in the document, or None if none is stated."""
    for page_number, text in enumerate(pages, 1):
        for line in text.splitlines():
            if not _LABEL.search(line):
                continue
            try:
                dates = _dates_on(line)
            except ValueError:  # e.g. February 30
                continue
            if not dates:
                continue
            start, end = min(dates), max(dates)
            if len(dates) == 2 and dates[0] > dates[1]:
                # "December 15 - January 14, 2026": the start is in the year before.
                start, end = dates[0].replace(year=dates[0].year - 1), dates[1]
            return ServicePeriod(start=start, end=end, page=page_number, quote=line.strip())
    return None
