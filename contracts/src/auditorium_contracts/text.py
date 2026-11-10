"""The one way a document becomes text, and the one way a quote is compared to it.

The agent quotes documents and the eval checks those quotes. Both must read the
same text, or honest quotes fail and the fabrication check means nothing. So
both go through this file.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


def normalize(text: str) -> str:
    """Fold away differences that do not change what the words say."""
    text = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION)
    return " ".join(text.split()).casefold()


@lru_cache(maxsize=512)
def _page_texts(path: str) -> tuple[str, ...]:
    if path.lower().endswith(".pdf"):
        return tuple(page.extract_text() or "" for page in PdfReader(path).pages)
    return (Path(path).read_text(encoding="utf-8"),)


def page_texts(path: Path | str) -> tuple[str, ...]:
    """The text of each page. A file that is not a PDF is read as one page of text."""
    return _page_texts(str(path))


def quote_on_page(path: Path | str, page: int, quote: str) -> bool:
    """True if `quote` appears on that 1-based page, after normalizing both."""
    pages = page_texts(path)
    if not 1 <= page <= len(pages):
        return False
    wanted = normalize(quote)
    return bool(wanted) and wanted in normalize(pages[page - 1])
