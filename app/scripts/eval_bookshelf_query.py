"""Evaluate the per-cluster KB query of find_similar_by_bookshelf.

For each test shelf (catalog books that form one cluster) and each query
variant, build the query, run the real KB Retrieve (shelf excluded by ISBN and,
like the tool, by title), and have Claude judge the top hits: would a reader who
loves the shelf enjoy each one — the same broad genre and a similar audience?

Reports precision@K (share of judged-relevant hits) and stability (Jaccard
overlap of the hit lists between runs). Variants:

  tool          what the tool sends: "Genres: ..." + the books' descriptions
  llm_blurb     the original LLM-written abstract blurb (replaced)
  llm_concrete  an LLM blurb told to keep genres, concrete elements, audience

Results that set the current design (6 shelves, 2 runs, K=8; two evaluations,
so each figure is a range — the LLM judge adds about ±0.05 of noise):
  tool          precision 0.71–0.72   stability 1.00
  llm_concrete  precision 0.69–0.72   stability 0.52–0.55
  llm_blurb     precision 0.38–0.41   stability 0.22–0.31
The weakest shelf for every variant is classic romance — the KB holds few of them.

Needs ANTHROPIC_API_KEY, BEDROCK_KB_ID, AWS credentials, and the catalog DB
(docker compose). About a minute. Run from the app folder:

    python scripts/eval_bookshelf_query.py
    python scripts/eval_bookshelf_query.py --runs 3 --out eval_results.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api  # noqa: E402  (catalog row -> Book mapping)
from db import pool  # noqa: E402
from tools.find_similar import find_similar  # noqa: E402
from tools.find_similar_by_bookshelf import _cluster_query, _default_llm, _norm_title  # noqa: E402

# Test shelves: catalog book_ids that belong together (one cluster each).
SHELVES = {
    "adventure_fantasy": [4, 5],     # The Hobbit, A Game of Thrones
    "science_fiction": [1, 27, 30],  # Dune, The Martian, Project Hail Mary
    "crime_thriller": [17, 18],      # The Girl with the Dragon Tattoo, Gone Girl
    "classic_romance": [10, 11],     # Pride and Prejudice, Jane Eyre
    "horror": [21, 22],              # The Shining, Dracula
    "nonfiction": [23, 25],          # Sapiens, Thinking, Fast and Slow
}

LLM_BLURB_SYSTEM = (  # the tool's original blurb prompt
    "You write a short unified description for a cluster of similar books.\n"
    "It will be embedded and matched against book descriptions, so write it like "
    "a book blurb — 2-3 sentences about the shared themes, tone, setting and "
    "subject. Do NOT write a genre label or a list of titles. Return only the "
    "blurb text."
)

LLM_CONCRETE_SYSTEM = (
    "You write the search query used to find books similar to a group of books.\n"
    "The query is embedded and matched against book records that read like "
    "'<Title>. by <Author>. Genres: <genres>. <reviews and description>'.\n"
    "Write 2-3 sentences that:\n"
    "- name the shared genre and subgenres explicitly (e.g. 'epic fantasy, sword "
    "and sorcery');\n"
    "- keep the concrete recurring elements: kinds of characters, creatures, "
    "settings and plot devices (e.g. dragons, a quest, rival noble houses, a "
    "contested throne);\n"
    "- state the tone and the intended audience (adult, young adult, or middle grade).\n"
    "Do not name the given books or their authors, and do not write a bare label. "
    "Return only the query text."
)

JUDGE_SYSTEM = (
    "You judge book recommendations. A reader loves the shelf books. For each "
    "candidate decide whether it is a good recommendation for that reader: the "
    "same broad genre (or a very close one) and a similar audience (adult vs "
    "young adult vs children). Literary quality alone is not enough.\n"
    'Return ONLY JSON: {"verdicts":[{"n":1,"relevant":true},...]} — one entry per candidate.'
)

llm = _default_llm()


def _listing(books) -> str:
    return "\n".join(f"- {b.title}: {' '.join((b.description or '').split())[:200]}" for b in books)


VARIANTS = {
    "tool": lambda books: _cluster_query("", books),
    "llm_blurb": lambda books: " ".join(llm(LLM_BLURB_SYSTEM, _listing(books) + "\n\nWrite the unified blurb.").split()),
    "llm_concrete": lambda books: " ".join(llm(LLM_CONCRETE_SYSTEM, _listing(books) + "\n\nWrite the query.").split()),
}


def judge(books, hits) -> list[bool]:
    if not hits:
        return []
    shelf = "\n".join(f"- {b.title} ({', '.join(b.genres)}): {b.description}" for b in books)
    cands = "\n".join(f"{n}. {h.title}: {' '.join(h.snippet.split())[:400]}" for n, h in enumerate(hits, 1))
    for _ in range(3):
        reply = llm(JUDGE_SYSTEM, f"Shelf:\n{shelf}\n\nCandidates:\n{cands}")
        m = re.search(r"\{.*\}", reply, re.DOTALL)
        try:
            verdicts = {v["n"]: bool(v["relevant"]) for v in json.loads(m.group(0))["verdicts"]}
            return [verdicts.get(n, False) for n in range(1, len(hits) + 1)]
        except (AttributeError, ValueError, KeyError, TypeError):
            continue
    raise RuntimeError("the judge gave no parseable verdicts in 3 tries")


def run_one(job, k):
    shelf_name, variant, run, books = job
    query = VARIANTS[variant](books)
    titles = {_norm_title(b.title) for b in books}
    found = find_similar(query, exclude_isbns=[b.isbn for b in books], top_k=k * 2)
    hits = [h for h in found if _norm_title(h.title) not in titles][:k]
    return {
        "shelf": shelf_name, "variant": variant, "run": run, "query": query,
        "hits": [{"title": h.title, "isbn": h.isbn, "score": round(h.score, 3), "relevant": r}
                 for h, r in zip(hits, judge(books, hits))],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=2, help="runs per shelf and variant (default 2)")
    parser.add_argument("--k", type=int, default=8, help="hits judged per query (default 8)")
    parser.add_argument("--out", help="also write every query, hit and verdict to this JSON file")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    pool.open(wait=True, timeout=10)
    try:
        with pool.connection() as conn:
            shelves = {
                name: [api._catalog_book(r) for r in conn.execute(
                    f"SELECT {api._BOOK_COLS} FROM books b WHERE b.book_id = ANY(%s) ORDER BY b.book_id",
                    (ids,),
                ).fetchall()]
                for name, ids in SHELVES.items()
            }
    finally:
        pool.close()

    jobs = [(s, v, r, shelves[s]) for s in SHELVES for v in VARIANTS for r in range(args.runs)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda j: run_one(j, args.k), jobs))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    def precision(res):
        return sum(h["relevant"] for h in res["hits"]) / args.k

    def jaccard(rs):
        sets = [{h["isbn"] for h in r["hits"]} for r in rs]
        pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1:]]
        return sum(len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs) / len(pairs) if pairs else 1.0

    print(f"precision@{args.k} (LLM-judged), mean of {args.runs} run(s); (j) = run-to-run overlap\n")
    print(f"{'shelf':<20}" + "".join(f"{v:>18}" for v in VARIANTS))
    summary = {v: ([], []) for v in VARIANTS}
    for s in SHELVES:
        row = f"{s:<20}"
        for v in VARIANTS:
            rs = [r for r in results if r["shelf"] == s and r["variant"] == v]
            p, j = sum(map(precision, rs)) / len(rs), jaccard(rs)
            summary[v][0].append(p)
            summary[v][1].append(j)
            row += f"{p:>12.2f} (j{j:.1f})"
        print(row)
    print()
    for v, (ps, js) in summary.items():
        print(f"{v:<14} precision {sum(ps) / len(ps):.2f}   stability {sum(js) / len(js):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
