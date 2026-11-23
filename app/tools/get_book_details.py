"""get_book_details — fetch catalog metadata for one book from Open Library.

Pure data access (no LLM, no framework). Given an ISBN, returns a `Book` with
title, authors, pages, year, subjects, cover, and (best-effort) a description.
Returns `None` when the ISBN isn't found; raises `GetBookDetailsError` on a
malformed ISBN or a network/parse failure (timeouts included).

`Book.isbn` is always the ISBN-13 form, whatever form was passed in, so it
compares equal to the ISBNs other tools carry (see isbn.py).

No caching yet — this calls Open Library directly on every lookup. A Postgres
catalog cache will sit in front of this later (see DESIGN.md §6 / OPEN_QUESTIONS).

Open Library is crowd-sourced, so every field is treated as optional.

Calls:
  * primary:     GET /api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json
                 (title, authors, pages, publish_date, subjects, cover — 1 call)
  * description: GET /isbn/{isbn}.json -> works[0].key -> GET /works/{key}.json
                 (description lives on the *work*, not the edition — up to 2 more
                 calls; failure-tolerant, description just stays None)

Direct test:
    python -m tools.get_book_details 9780441013593
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from isbn import normalize_isbn, to_isbn13
from models import Book

logger = logging.getLogger(__name__)

OPENLIBRARY_BASE = "https://openlibrary.org"
# Generic client identifier for Open Library — not the user's email.
USER_AGENT = "BookRecommender/0.1 (AI course project)"
DEFAULT_TIMEOUT = 10.0

# A fetcher takes a URL and returns parsed JSON, None on 404, or raises
# GetBookDetailsError on any other failure. Injectable for offline tests.
Fetcher = Callable[[str], Any]


class GetBookDetailsError(RuntimeError):
    """Raised when book details could not be fetched (bad ISBN, network, parse)."""


def get_book_details(
    isbn: str,
    *,
    fetch_description: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    fetcher: Fetcher | None = None,
) -> Book | None:
    """Resolve one book's catalog details from Open Library.

    Args:
        isbn: ISBN-10 or ISBN-13 (hyphens/spaces allowed; normalized internally).
        fetch_description: If True, make the extra work-lookup calls to populate
            `description`. Failure-tolerant (description stays None on any error).
        timeout: Per-request timeout in seconds (default fetcher only).
        fetcher: Injectable JSON fetcher for tests; defaults to a urllib fetcher.

    Returns:
        A `Book`, or `None` if Open Library has no record for this ISBN.

    Raises:
        GetBookDetailsError: malformed ISBN, or a network/parse failure on the
            primary lookup.
    """
    # Look up with the form we were given (Open Library indexes both), but
    # report the book under its ISBN-13.
    lookup = normalize_isbn(isbn)
    if lookup is None:
        raise GetBookDetailsError(f"Invalid ISBN: {isbn!r}")

    fetch = fetcher or _make_urllib_fetcher(timeout)
    bibkey = f"ISBN:{lookup}"
    url = f"{OPENLIBRARY_BASE}/api/books?bibkeys={bibkey}&jscmd=data&format=json"

    data = fetch(url)
    # Open Library returns 200 with {} for an unknown ISBN (not a 404).
    record = data.get(bibkey) if isinstance(data, dict) else None
    if not isinstance(record, dict) or not record:
        return None

    description = None
    if fetch_description:
        description = _fetch_description(lookup, fetch)

    return Book(
        isbn=to_isbn13(lookup),
        title=_clean(record.get("title")),
        authors=[
            name
            for a in _dicts(record.get("authors"))
            if (name := _clean(a.get("name")))
        ],
        pages=_as_int(record.get("number_of_pages")),
        publish_year=_parse_year(record.get("publish_date")),
        subjects=[
            name
            for s in _dicts(record.get("subjects"))
            if (name := _clean(s.get("name")))
        ],
        cover_url=_pick_cover(record.get("cover")),
        description=description,
        source=_clean(record.get("url")) or f"{OPENLIBRARY_BASE}/isbn/{lookup}",
    )


# -- description (work lookup) -------------------------------------------

def _fetch_description(isbn: str, fetch: Fetcher) -> str | None:
    """Best-effort description via the edition -> work chain. Never raises."""
    try:
        edition = fetch(f"{OPENLIBRARY_BASE}/isbn/{isbn}.json")
        if not isinstance(edition, dict):
            return None
        works = _dicts(edition.get("works"))
        if not works:
            # Rare, but some editions carry their own description.
            return _extract_description(edition.get("description"))
        work_key = _clean(works[0].get("key"))
        if not work_key:
            return None
        work = fetch(f"{OPENLIBRARY_BASE}{work_key}.json")
        if not isinstance(work, dict):
            return None
        return _extract_description(work.get("description"))
    except GetBookDetailsError as e:
        logger.info("get_book_details: description lookup failed for %s: %s", isbn, e)
        return None


def _extract_description(value: Any) -> str | None:
    """Open Library description is either a plain string or {'value': '...'}."""
    if isinstance(value, dict):
        value = value.get("value")
    return _clean(value)


# -- HTTP ----------------------------------------------------------------

def _make_urllib_fetcher(timeout: float) -> Fetcher:
    def fetch(url: str) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise GetBookDetailsError(f"HTTP {e.code} for {url}") from e
        except (OSError, http.client.HTTPException) as e:
            # OSError covers URLError and the bare TimeoutError that urllib
            # lets through when the server accepts but is slow to answer.
            raise GetBookDetailsError(f"Network error for {url}: {e}") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise GetBookDetailsError(f"Invalid JSON from {url}") from e

    return fetch


# -- small helpers -------------------------------------------------------

def _dicts(value: Any) -> list[dict]:
    """The dict items of a JSON list; [] for anything else Open Library sends."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_year(publish_date: Any) -> int | None:
    if not publish_date:
        return None
    match = re.search(r"\b(1\d{3}|2\d{3})\b", str(publish_date))
    return int(match.group(1)) if match else None


def _pick_cover(cover: Any) -> str | None:
    if not isinstance(cover, dict):
        return None
    return cover.get("large") or cover.get("medium") or cover.get("small")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    query = (sys.argv[1] if len(sys.argv) > 1 else "9780441013593").strip()
    try:
        book = get_book_details(query)
    except GetBookDetailsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if book is None:
        print(f"No Open Library record for ISBN {query!r}.")
        raise SystemExit(0)

    print(f"Title:   {book.title}")
    print(f"Authors: {', '.join(book.authors) or '-'}")
    print(f"Year:    {book.publish_year or '-'}   Pages: {book.pages or '-'}")
    print(f"ISBN:    {book.isbn}")
    print(f"Cover:   {book.cover_url or '-'}")
    print(f"Subjects: {', '.join(book.subjects[:8]) or '-'}")
    print(f"Source:  {book.source}")
    if book.description:
        print(f"\n{book.description[:400]}")
