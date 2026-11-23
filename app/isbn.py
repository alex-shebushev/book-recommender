"""ISBN helpers shared by the tools.

Every tool identifies a book by the same form: **ISBN-13, digits only**. The
same edition can arrive as an ISBN-10, an ISBN-13, or either with hyphens, and
comparing those as raw strings silently breaks exclusion and dedup between
tools. Convert with `to_isbn13` before storing or comparing.

Only the shape is checked, not the check digit: Open Library holds records
under ISBNs that fail the checksum, and rejecting them would hide real books.
"""
from __future__ import annotations

import re
from typing import Any


def normalize_isbn(value: Any) -> str | None:
    """Strip separators and return the ISBN-10/13 as given, or None if malformed."""
    if not value:
        return None
    text = re.sub(r"[\s-]", "", str(value)).upper()
    if len(text) == 10 and re.fullmatch(r"\d{9}[\dX]", text):
        return text
    if len(text) == 13 and text.isdigit():
        return text
    return None


def to_isbn13(value: Any) -> str | None:
    """Return the ISBN-13 form of an ISBN-10 or ISBN-13, or None if malformed."""
    text = normalize_isbn(value)
    if text is None or len(text) == 13:
        return text
    core = "978" + text[:9]
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


def to_isbn10(value: Any) -> str | None:
    """Return the ISBN-10 form of an ISBN-10 or ISBN-13, or None if there is none.

    Only ISBN-13s starting with 978 have an ISBN-10; a 979 one returns None, as
    does malformed input. Needed because the Knowledge Base stores each book
    under its ISBN-10 while the tools share ISBN-13.
    """
    text = normalize_isbn(value)
    if text is None or len(text) == 10:
        return text
    if not text.startswith("978"):
        return None
    core = text[3:12]
    check = (11 - sum(int(d) * (10 - i) for i, d in enumerate(core)) % 11) % 11
    return core + ("X" if check == 10 else str(check))
