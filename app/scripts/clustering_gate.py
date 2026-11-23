"""Q10 gate — validate the LLM `cluster` step of `find_similar_by_bookshelf`.

A diagnostic, NOT the tool. It runs the tool's clustering prompt on a seed
"shelf" and prints the result so a human can judge quality (DESIGN.md §5,
OPEN_QUESTIONS Q10), then shows the KB query each cluster would send:

    resolve : get_book_details per ISBN  -> {isbn, title, desc, subjects}
    cluster : LLM groups the books genre-first (infer missing genre, handle
              compound genres, every book in exactly one cluster)
    query   : the tool's deterministic per-cluster query (genres + the books'
              descriptions). It replaced an LLM-written blurb, which scored far
              lower — see scripts/eval_bookshelf_query.py.

What to look for: coherent clusters, compound genres handled, missing genre
inferred sensibly, and stability across `--runs`. If these look weak, the
tool's fan-out is built on sand — fix the prompt first.

Needs ANTHROPIC_API_KEY (from the app's `.env`) and network (Open Library +
Anthropic). Run from the app folder:

    python scripts/clustering_gate.py
    python scripts/clustering_gate.py 9780441013593 9780547928227 --runs 3
    python scripts/clustering_gate.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Make the app folder importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from config import AgentConfig  # noqa: E402  (importing config also loads .env)
from tools.get_book_details import GetBookDetailsError, get_book_details  # noqa: E402

# A deliberately mixed shelf: two fantasy, one sci-fi, two crime/thriller, one
# romance/classic, one nonfiction. Exercises genre grouping, a compound-genre
# case, and a lone minority (nonfiction) that clustering must not drop.
SAMPLE_SHELF = [
    "9780441013593",  # Dune — science fiction
    "9780547928227",  # The Hobbit — fantasy
    "9780756404741",  # The Name of the Wind — fantasy
    "9780307454546",  # The Girl with the Dragon Tattoo — crime/thriller
    "9780307588371",  # Gone Girl — psychological thriller
    "9780141439518",  # Pride and Prejudice — romance/classic
    "9780062316097",  # Sapiens — nonfiction/history
]

# The tool's own prompts, so this gate always tests what the tool actually sends.
from models import Book  # noqa: E402
from tools.find_similar_by_bookshelf import CLUSTER_SYSTEM, _cluster_query  # noqa: E402


@dataclass
class Resolved:
    isbn: str
    title: str
    description: str
    subjects: list[str]


def resolve(isbns: list[str]) -> tuple[list[Resolved], list[str]]:
    """get_book_details per ISBN. Returns (resolved, unresolved-with-reason)."""
    resolved: list[Resolved] = []
    unresolved: list[str] = []
    for isbn in isbns:
        try:
            book = get_book_details(isbn)
        except GetBookDetailsError as e:
            unresolved.append(f"{isbn}: {e}")
            continue
        if book is None:
            unresolved.append(f"{isbn}: no Open Library record")
            continue
        resolved.append(
            Resolved(
                isbn=book.isbn,
                title=book.title or "(untitled)",
                description=(book.description or "").strip(),
                subjects=book.subjects[:8],
            )
        )
    return resolved, unresolved


def _complete(client: anthropic.Anthropic, cfg: AgentConfig, system: str, user: str) -> str:
    # This SDK build (Sonnet 4.6, adaptive thinking) does not expose a
    # `temperature` kwarg, so stability has to come from the prompt, not sampling.
    resp = client.messages.create(
        model=cfg.model,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _parse_json(text: str) -> dict:
    """Parse the model's JSON, tolerating ```json fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    return json.loads(t.strip())


def _book_line(b: Resolved) -> str:
    desc = " ".join(b.description.split())
    if len(desc) > 300:
        desc = desc[:300] + "..."
    subj = ", ".join(b.subjects) or "-"
    return f"- [{b.isbn}] {b.title}\n    subjects: {subj}\n    description: {desc or '(none)'}"


def cluster(client, cfg, books: list[Resolved]) -> list[dict]:
    catalog = "\n".join(_book_line(b) for b in books)
    user = f"Cluster these {len(books)} books:\n\n{catalog}"
    return _parse_json(_complete(client, cfg, CLUSTER_SYSTEM, user)).get("clusters", [])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("isbns", nargs="*", help="seed ISBNs (default: sample shelf)")
    parser.add_argument("--runs", type=int, default=1, help="repeat clustering N times to check stability")
    parser.add_argument("--json", action="store_true", help="print clusters as JSON")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = AgentConfig.from_env()
    client = anthropic.Anthropic()
    print(f"Model: {cfg.model}")

    isbns = args.isbns or SAMPLE_SHELF
    books, unresolved = resolve(isbns)
    by_isbn = {b.isbn: b for b in books}
    print(f"\nResolved {len(books)}/{len(isbns)} books:")
    for b in books:
        print(f"  [{b.isbn}] {b.title}")
    for u in unresolved:
        print(f"  UNRESOLVED {u}")
    if not books:
        print("Nothing resolved — cannot cluster.")
        return 1

    runs: list[list[dict]] = []
    for r in range(1, args.runs + 1):
        clusters = cluster(client, cfg, books)
        runs.append(clusters)
        print(f"\n=== clustering run {r}/{args.runs} ===")
        if args.json:
            print(json.dumps(clusters, ensure_ascii=False, indent=2))
        for c in clusters:
            members = [by_isbn.get(i) for i in c.get("isbns", [])]
            names = ", ".join(m.title if m else f"?{i}" for m, i in zip(members, c.get("isbns", [])))
            print(f"  • {c.get('label', '?')}: {names}")

    # The KB query the tool would send for each of the final run's clusters.
    print("\n=== KB queries (final run) ===")
    for c in runs[-1]:
        members = [by_isbn[i] for i in c.get("isbns", []) if i in by_isbn]
        if not members:
            continue
        books_ = [Book(isbn=m.isbn, title=m.title, description=m.description, subjects=m.subjects)
                  for m in members]
        print(f"\n[{c.get('label', '?')}]  ({len(members)} book(s))")
        print(f"  {_cluster_query(c.get('label', ''), books_)}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except anthropic.APIError as exc:
        print(f"Anthropic API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
