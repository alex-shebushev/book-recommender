"""Try `find_similar` against the live Bedrock Knowledge Base for any query.

Pass one or more query texts (quote each one). With no arguments it runs a few
sample queries. Needs AWS credentials (your AWS CLI profile) and BEDROCK_KB_ID,
which is read from the app's `.env` — no need to export it in the shell.

Run from the app folder:
    python scripts/check_find_similar.py
    python scripts/check_find_similar.py "a hopeful novel about first contact"
    python scripts/check_find_similar.py "dark epic fantasy" "space opera" --top-k 3
    python scripts/check_find_similar.py "space opera" --min-score 0.5
    python scripts/check_find_similar.py "space opera" --exclude 0553283685
    python scripts/check_find_similar.py "space opera" --json

Exit code: 0 when every query completed (with or without hits), 1 when any
query failed (missing KB id, AWS error).
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
from tools.find_similar import FindSimilarError, find_similar  # noqa: E402

# Used when no query is given: two in-domain queries and one that should have
# no good match in a book KB (shows what weak scores look like).
SAMPLE_QUERIES = [
    "a hopeful science-fiction novel about first contact and found family",
    "grim military fantasy told by a mercenary company's chronicler",
    "quarterly tax accounting rules for small businesses",
]

SNIPPET_CHARS = 160


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("queries", nargs="*", help="query texts (quote each one)")
    parser.add_argument(
        "--top-k", type=int, default=5, help="max books per query (default 5)"
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="drop hits below this score (default 0 = keep everything)",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        metavar="ISBN",
        help="ISBNs to leave out, any form (ISBN-10 or ISBN-13)",
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
    print(f"top_k={args.top_k}  min_score={args.min_score:g}  exclude={args.exclude or '-'}")

    queries = args.queries or SAMPLE_QUERIES
    with_hits = empty = failed = 0

    for query in queries:
        started = time.perf_counter()
        try:
            books = find_similar(
                query,
                knowledge_base_id=kb.knowledge_base_id,
                region=kb.region,
                top_k=args.top_k,
                min_score=args.min_score,
                exclude_isbns=args.exclude,
            )
        except FindSimilarError as e:
            failed += 1
            print(f"\n=== {query} ===  ERROR ({_elapsed(started)})\n  {e}")
            continue

        if not books:
            empty += 1
            print(f"\n=== {query} ===  NO MATCHES ({_elapsed(started)})")
            continue

        with_hits += 1
        print(f"\n=== {query} ===  {len(books)} HIT(S) ({_elapsed(started)})")
        if args.json:
            print(json.dumps([asdict(b) for b in books], indent=2, ensure_ascii=False))
        else:
            _print_books(books)

    print(f"\n{with_hits} with hits, {empty} empty, {failed} failed")
    return 1 if failed else 0


def _print_books(books) -> None:
    for rank, book in enumerate(books, 1):
        print(
            f"  {rank}. {book.score:.3f}  {book.title or '(no title)'}"
            f"  [{book.isbn or 'no isbn'}]"
        )
        print(f"       source:  {book.source or '(none)'}")
        snippet = " ".join(book.snippet.split())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS] + "..."
        print(f"       snippet: {snippet or '(empty)'}")
    if not any(book.isbn for book in books):
        print("  note: no hit carried an ISBN - the KB metadata has no `isbn` field.")


def _elapsed(started: float) -> str:
    return f"{time.perf_counter() - started:.1f}s"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
