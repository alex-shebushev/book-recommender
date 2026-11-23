"""Try `get_book_details` against live Open Library for any ISBN.

Pass one or more ISBNs in any form (ISBN-10, ISBN-13, with or without hyphens).
With no arguments it runs a few sample ISBNs. Needs internet access only — no
API key, no AWS.

Run from the app folder:
    python scripts/check_get_book_details.py
    python scripts/check_get_book_details.py 9780441013593
    python scripts/check_get_book_details.py 0-441-01359-7 9780061120084
    python scripts/check_get_book_details.py 9780441013593 --json
    python scripts/check_get_book_details.py 9780441013593 --no-description

Exit code: 0 when every lookup completed (found or not found), 1 when any
lookup failed (malformed ISBN, network error).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Make the app folder importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.get_book_details import (  # noqa: E402
    DEFAULT_TIMEOUT,
    GetBookDetailsError,
    get_book_details,
)

# Used when no ISBN is given: an ISBN-13, a hyphenated ISBN-10, and one that
# is well-formed but has no Open Library record (shows the "not found" path).
SAMPLE_ISBNS = ["9780441013593", "0-06-112008-1", "9798888888888"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("isbns", nargs="*", help="ISBNs to look up (any form)")
    parser.add_argument(
        "--no-description",
        action="store_true",
        help="skip the extra calls that fetch the description",
    )
    parser.add_argument(
        "--json", action="store_true", help="print each result as JSON"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)

    # Book text is often non-ASCII; a Windows console would otherwise garble it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    isbns = args.isbns or SAMPLE_ISBNS
    found = missing = failed = 0

    for isbn in isbns:
        started = time.perf_counter()
        try:
            book = get_book_details(
                isbn,
                fetch_description=not args.no_description,
                timeout=args.timeout,
            )
        except GetBookDetailsError as e:
            failed += 1
            print(f"\n=== {isbn} ===  ERROR ({_elapsed(started)})\n  {e}")
            continue

        if book is None:
            missing += 1
            print(f"\n=== {isbn} ===  NOT FOUND ({_elapsed(started)})")
            continue

        found += 1
        print(f"\n=== {isbn} ===  FOUND ({_elapsed(started)})")
        if args.json:
            print(json.dumps(asdict(book), indent=2, ensure_ascii=False))
        else:
            _print_book(book)

    print(f"\n{found} found, {missing} not found, {failed} failed")
    return 1 if failed else 0


def _print_book(book) -> None:
    rows = [
        ("ISBN-13", book.isbn),
        ("Title", book.title),
        ("Authors", ", ".join(book.authors)),
        ("Year", book.publish_year),
        ("Pages", book.pages),
        ("Subjects", ", ".join(book.subjects)),
        ("Cover", book.cover_url),
        ("Source", book.source),
        ("Description", book.description),
    ]
    for label, value in rows:
        print(f"  {label + ':':<13}{value if value else '(none)'}")


def _elapsed(started: float) -> str:
    return f"{time.perf_counter() - started:.1f}s"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
