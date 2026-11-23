"""Try `get_reviews_and_guides` against the live Bedrock Knowledge Base.

Pass one or more ISBNs in any form (ISBN-10, ISBN-13, with or without hyphens).
With no arguments it runs a few sample ISBNs. Needs AWS credentials (your AWS
CLI profile) and BEDROCK_KB_ID, which is read from the app's `.env`.

Run from the app folder:
    python scripts/check_get_reviews_and_guides.py
    python scripts/check_get_reviews_and_guides.py 9780553283686
    python scripts/check_get_reviews_and_guides.py 0-7653-4827-6 --question "is there romance in it"
    python scripts/check_get_reviews_and_guides.py 0765348276 --full
    python scripts/check_get_reviews_and_guides.py 0765348276 --json

Exit code: 0 when every lookup completed (found or not found), 1 when any
lookup failed (malformed ISBN, missing KB id, AWS error).
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

from config import KBConfig  # noqa: E402  (importing config also loads .env)
from tools.get_reviews_and_guides import (  # noqa: E402
    GetReviewsError,
    get_reviews_and_guides,
)

# Used when no ISBN is given: an ISBN-13 and a hyphenated ISBN-10 of books in
# the KB (which files them under ISBN-10), and one well-formed ISBN the KB does
# not have (shows the "nothing on this book" path).
SAMPLE_ISBNS = ["9780553283686", "0-7653-4827-6", "9798888888888"]

PREVIEW_CHARS = 300


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("isbns", nargs="*", help="ISBNs to look up (any form)")
    parser.add_argument(
        "--question",
        help="what you want to know; passages are ordered by relevance to it",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="max passages per book (default 5)"
    )
    parser.add_argument(
        "--full", action="store_true", help="print each passage in full"
    )
    parser.add_argument(
        "--json", action="store_true", help="print each result list as JSON"
    )
    args = parser.parse_args(argv)

    # Book text is often non-ASCII; a Windows console would otherwise garble it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    kb = KBConfig.from_env()
    print(f"Knowledge base: {kb.knowledge_base_id or '(BEDROCK_KB_ID not set)'}")
    print(f"Region:         {kb.region or '(from AWS profile)'}")
    print(f"question={args.question or '(default)'}  top_k={args.top_k}")

    isbns = args.isbns or SAMPLE_ISBNS
    found = missing = failed = 0

    for isbn in isbns:
        started = time.perf_counter()
        try:
            docs = get_reviews_and_guides(
                isbn, question=args.question, top_k=args.top_k
            )
        except GetReviewsError as e:
            failed += 1
            print(f"\n=== {isbn} ===  ERROR ({_elapsed(started)})\n  {e}")
            continue

        if not docs:
            missing += 1
            print(f"\n=== {isbn} ===  NOTHING IN THE KB ({_elapsed(started)})")
            continue

        found += 1
        print(f"\n=== {isbn} ===  {len(docs)} PASSAGE(S) ({_elapsed(started)})")
        if args.json:
            print(json.dumps([asdict(d) for d in docs], indent=2, ensure_ascii=False))
        else:
            _print_docs(docs, full=args.full)

    print(f"\n{found} found, {missing} not in the KB, {failed} failed")
    return 1 if failed else 0


def _print_docs(docs, *, full: bool) -> None:
    for rank, doc in enumerate(docs, 1):
        print(
            f"  {rank}. {doc.score:.3f}  {doc.title or '(no title)'}"
            f" - {doc.author or '(no author)'}  [{doc.isbn or 'no isbn'}]"
        )
        print(f"       type:      {doc.doc_type or '(none)'}")
        print(f"       source:    {doc.source or '(none)'}")
        print(f"       source_id: {doc.source_id or '(none)'}")
        text = doc.text if full else " ".join(doc.text.split())
        if not full and len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + f"... ({len(doc.text)} chars, --full to see all)"
        print(f"       text:      {text}")


def _elapsed(started: float) -> str:
    return f"{time.perf_counter() - started:.1f}s"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
