"""find_similar_by_bookshelf — books similar to a bookshelf (one or many books).

The second LangGraph-backed tool (DESIGN.md §5). The caller passes the shelf as
`Book` objects (from the catalog DB, `get_book_details`, or `parse_bookshelf`),
plus optional books to keep out of the output. The graph owns the control flow;
the LLM is used only to cluster the shelf. Retrieval reuses `find_similar`:

    prepare   validate + dedupe the given books           -> shelf: list[Book]
    cluster   LLM: genre-first grouping                   -> clusters
    retrieve  find_similar, fanned out per cluster (Send) -> hits per cluster
    merge     floor + proportional slots, dedup, exclusion, exploratory flag

Why per-cluster queries: one centroid query for a mixed shelf matches nothing
well. Each cluster gets its own query (a true OR), then `merge` gives every
cluster a floor slot and splits the rest by how much of the shelf it covers, so
the majority taste dominates while a minority taste is still shown — flagged
`exploratory`.

The per-cluster query is built WITHOUT an LLM: "Genres: <the cluster's genres>."
followed by the member books' own descriptions (see `_cluster_query`). It
mirrors how the KB records read ("<Title>. by <Author>. Genres: ..."), so it
embeds close to the right books. Measured with scripts/eval_bookshelf_query.py
(6 shelves, 2 runs, LLM-judged precision@8): the earlier LLM-written abstract
blurb scored 0.38–0.41 with 0.22–0.31 run-to-run overlap; this query scores
0.71–0.72 and is fully repeatable. An LLM blurb told to keep concrete details
tied on precision (0.69–0.72) but overlapped only ~0.5 between runs, and costs
an LLM call per cluster.

Exclusion: the shelf books and the `exclude` books never come back. They are
excluded by ISBN (every form, inside `find_similar`) and again by title in
`merge`, which catches other editions the KB files under a different ISBN (Q9).
Description queries pull a shelf book's other editions to the top, so each
cluster over-fetches to keep its slots filled after those drops.

Stability: this SDK build exposes no `temperature`, so clustering stability
comes from the prompt (the Q10 gate found Dune drifting into fantasy until the
sci-fi/fantasy rule was added). Cluster *labels* still vary in wording between
runs; membership is what matters, so nothing here keys off a label string.

Degrades rather than fails: unusable books are reported in `skipped`; an
unparseable clustering falls back to one cluster for the whole shelf; a failed
retrieval empties only that cluster. It raises only when retrieval failed for
every cluster.

Known limit: KB scores are relative per query, so they can't say "no strong
match" (Q12) and are only a rough tiebreak across clusters.

Direct test — ISBNs are resolved through Open Library first (needs
ANTHROPIC_API_KEY, BEDROCK_KB_ID, AWS profile, network):
    python -m tools.find_similar_by_bookshelf 9780441013593 9780547928227 9780307588371
"""
from __future__ import annotations

import json
import logging
import operator
import re
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Callable, TypedDict

import anthropic
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from config import AgentConfig  # importing config also loads .env
from isbn import to_isbn13
from models import Book, SimilarBook
from tools.find_similar import FindSimilarError
from tools.find_similar import find_similar as _find_similar

logger = logging.getLogger(__name__)

# (system, user) -> reply text. Injectable so tests run without the API.
LLM = Callable[[str, str], str]

CLUSTER_SYSTEM = (
    "You group books into genre clusters for a recommendation engine.\n"
    "Rules:\n"
    "- Genre-first: use each book's stated genre/subjects when present; infer a "
    "genre only when it is missing.\n"
    "- Group into a small number of coherent buckets. Handle compound genres "
    "(e.g. 'romantic fantasy') as their own bucket when warranted; do not force "
    "every book into one giant bucket, and do not make a bucket per book.\n"
    "- Every book goes in exactly one cluster.\n"
    "- Keep science fiction and fantasy in separate clusters unless a book is "
    "genuinely both; do not merge distinct primary genres for convenience.\n"
    "- A short, specific label per cluster (e.g. 'epic fantasy', not 'fiction').\n"
    'Return ONLY JSON: {"clusters":[{"label":"...","isbns":["..."]}]} — no prose.'
)

WHOLE_SHELF_LABEL = "your shelf"

# Query sizing: Bedrock accepts up to 20,000 characters; keep well under, and
# cap each book so one long description can't drown out the rest.
MAX_QUERY_CHARS = 4000
MAX_DESCRIPTION_CHARS = 400
# Retrieval per cluster = this many times the slot budget, so slots stay filled
# after merge drops other editions of shelf / excluded books.
OVERFETCH = 2


class FindSimilarByBookshelfError(RuntimeError):
    """Raised on bad arguments, or when retrieval failed for every cluster."""


@dataclass
class Cluster:
    """One group of shelf books, and the query text sent to the KB for it."""

    label: str
    isbns: list[str]  # ISBN-13s of the shelf books in this cluster
    query: str | None = None
    exploratory: bool = False


@dataclass
class SimilarByBookshelfResult:
    """Everything one run produced — the books plus what explains them."""

    books: list[SimilarBook]
    clusters: list[Cluster] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # shelf books that couldn't be used
    notes: list[str] = field(default_factory=list)  # degradations, for the UI/logs


# -- public API --------------------------------------------------------------

def find_similar_by_bookshelf(
    books: list[Book],
    *,
    top_k: int = 6,
    exclude: list[Book] | None = None,
    soft_exclude: list[Book] | None = None,
    min_score: float = 0.0,
    llm: LLM | None = None,
    find: Callable[..., list[SimilarBook]] | None = None,
) -> list[SimilarBook]:
    """Return up to `top_k` books similar to the shelf `books`.

    Each returned book carries `cluster` (which part of the shelf it matches)
    and `exploratory` (it matches a minority taste). See
    `find_similar_by_bookshelf_detailed` for the clusters (with the exact KB
    query each one sent), skipped books and notes.
    """
    return find_similar_by_bookshelf_detailed(
        books, top_k=top_k, exclude=exclude, soft_exclude=soft_exclude,
        min_score=min_score, llm=llm, find=find,
    ).books


def find_similar_by_bookshelf_detailed(
    books: list[Book],
    *,
    top_k: int = 6,
    exclude: list[Book] | None = None,
    soft_exclude: list[Book] | None = None,
    min_score: float = 0.0,
    llm: LLM | None = None,
    find: Callable[..., list[SimilarBook]] | None = None,
) -> SimilarByBookshelfResult:
    """Run the graph and return books plus clusters, skipped books and notes.

    Args:
        books: The shelf. Each needs a valid ISBN; genres and descriptions
            build the KB queries and titles/genres feed the clustering.
        top_k: Total books to return across all clusters.
        exclude: Books that must not be returned (e.g. books already on the
            reader's normal shelves). The shelf itself is always excluded.
        soft_exclude: Books on the reader's Exclusion shelf. They are NOT
            dropped: matching results are kept and tagged `excluded=True`, so
            the caller can hide or gray them. They still take a slot.
        min_score: Passed to find_similar. Scores are relative per query, so
            keep it 0 unless calibrated (Q12).
        llm, find: Injectable dependencies for tests.

    Raises:
        FindSimilarByBookshelfError: `top_k` < 1, or retrieval failed for every
            cluster.
    """
    if top_k < 1:
        raise FindSimilarByBookshelfError(f"top_k must be at least 1, got {top_k}")
    if not books:
        return SimilarByBookshelfResult(books=[], notes=["The shelf is empty."])

    graph = _build_graph(llm or _default_llm(), find or _find_similar)
    out = graph.invoke(
        {
            "given": list(books),
            "excluded": list(exclude or []),
            "soft": list(soft_exclude or []),
            "top_k": top_k,
            "min_score": min_score,
        }
    )

    clusters = out.get("clusters") or []
    notes = out.get("notes") or []
    failed = set(out.get("failed") or [])
    if clusters and len(failed) >= len(clusters):
        raise FindSimilarByBookshelfError(
            "Knowledge Base retrieval failed for every cluster: " + "; ".join(notes)
        )
    for note in notes:
        logger.info("find_similar_by_bookshelf: %s", note)
    return SimilarByBookshelfResult(
        books=out.get("books") or [],
        clusters=clusters,
        skipped=out.get("skipped") or [],
        notes=notes,
    )


# -- graph -------------------------------------------------------------------

class _State(TypedDict, total=False):
    # inputs
    given: list[Book]
    excluded: list[Book]
    soft: list[Book]
    top_k: int
    min_score: float
    # prepare
    shelf: list[Book]
    skipped: list[str]
    # cluster / retrieve (the last three are written by parallel branches)
    clusters: list[Cluster]
    hits: Annotated[list[tuple[int, list[SimilarBook]]], operator.add]
    failed: Annotated[list[int], operator.add]
    notes: Annotated[list[str], operator.add]
    # merge
    books: list[SimilarBook]


def _build_graph(llm: LLM, find: Callable[..., list[SimilarBook]]):
    """Compile the graph with its dependencies bound into the nodes."""

    def prepare(state: _State) -> dict:
        shelf: list[Book] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for book in state["given"]:
            isbn13 = to_isbn13(book.isbn)
            name = book.title or book.isbn or "(unnamed)"
            if isbn13 is None:
                skipped.append(f"{name}: no valid ISBN")
            elif isbn13 not in seen:
                seen.add(isbn13)
                # ISBN-13 is the internal key (cluster output is mapped back by it).
                shelf.append(replace(book, isbn=isbn13))
        return {"shelf": shelf, "skipped": skipped}

    def cluster(state: _State) -> dict:
        shelf = state["shelf"]
        if len(shelf) == 1:  # nothing to group — skip the LLM call
            clusters = [Cluster(label=_single_label(shelf[0]), isbns=[shelf[0].isbn])]
            notes: list[str] = []
        else:
            try:
                clusters, notes = _parse_clusters(llm(CLUSTER_SYSTEM, _cluster_prompt(shelf)), shelf)
            except anthropic.APIError as e:
                clusters = [Cluster(WHOLE_SHELF_LABEL, [b.isbn for b in shelf])]
                notes = [f"Clustering failed ({e}); searched with the whole shelf as one group."]
        # The query each cluster sends is plain text built from its books.
        by_isbn = {b.isbn: b for b in shelf}
        clusters = [
            replace(c, query=_cluster_query(c.label, [by_isbn[i] for i in c.isbns]))
            for c in clusters
        ]
        return {"clusters": clusters, "notes": notes}

    def retrieve(task: dict) -> dict:
        try:
            found = find(
                task["query"],
                exclude_isbns=task["exclude"],
                flag_isbns=task["flag"],
                top_k=task["k"],
                min_score=task["min_score"],
            )
        except FindSimilarError as e:
            return {
                "hits": [(task["idx"], [])],
                "failed": [task["idx"]],
                "notes": [f"Search for '{task['label']}' failed: {e}"],
            }
        return {"hits": [(task["idx"], list(found))]}

    # -- routing --

    def after_prepare(state: _State) -> str:
        return "cluster" if state.get("shelf") else "merge"

    def fan_out_retrieval(state: _State) -> list[Send]:
        # find_similar compares every ISBN form, so raw catalog ISBN-10s work too.
        exclude = [b.isbn for b in state["shelf"]] + [
            b.isbn for b in state.get("excluded") or [] if b.isbn
        ]
        # Exclusion-shelf books: kept but tagged, so the UI can hide or gray them.
        flag = [b.isbn for b in state.get("soft") or [] if b.isbn]
        return [
            Send(
                "retrieve",
                {
                    "idx": i,
                    "label": c.label,
                    "query": c.query,
                    "exclude": exclude,
                    "flag": flag,
                    # Enough to fill the whole budget (so merge can backfill a
                    # short cluster) even after other-edition title drops.
                    "k": state["top_k"] * OVERFETCH,
                    "min_score": state.get("min_score", 0.0),
                },
            )
            for i, c in enumerate(state["clusters"])
        ]

    g = StateGraph(_State)
    g.add_node("prepare", prepare)
    g.add_node("cluster", cluster)
    g.add_node("retrieve", retrieve)
    g.add_node("merge", _merge)

    g.add_edge(START, "prepare")
    g.add_conditional_edges("prepare", after_prepare, ["cluster", "merge"])
    g.add_conditional_edges("cluster", fan_out_retrieval, ["retrieve"])
    g.add_edge("retrieve", "merge")
    g.add_edge("merge", END)
    return g.compile()


# -- the per-cluster KB query ------------------------------------------------

def _cluster_query(label: str, members: list[Book]) -> str:
    """The KB query for one cluster: its genres, then its books' descriptions.

    "Genres: Fantasy, Adventure, Epic Fantasy. <description> <description>"

    Genres are the members' catalog genres (deduped, in order); without any,
    their Open Library subjects; without those, the cluster label. A book with
    no description contributes "<title> by <authors>" instead. Each description
    is capped, and so is the whole query. Deterministic — no LLM.
    """
    genres: list[str] = []
    for book in members:
        for g in book.genres or book.subjects[:5]:
            if g.casefold() not in {x.casefold() for x in genres}:
                genres.append(g)
    head = f"Genres: {', '.join(genres or [label])}."

    parts = []
    for book in members:
        text = " ".join((book.description or "").split())
        if not text:
            authors = ", ".join(book.authors)
            text = f"{book.title or ''}{f' by {authors}' if authors else ''}".strip()
        if len(text) > MAX_DESCRIPTION_CHARS:
            text = text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
        if text:
            parts.append(text)
    query = " ".join([head, *parts])
    return query[:MAX_QUERY_CHARS]


# -- merge: slots, dedup, exclusion, exploratory -----------------------------

def _merge(state: _State) -> dict:
    clusters = state.get("clusters") or []
    if not clusters:
        return {"books": [], "notes": ["No usable book on the shelf; nothing to search for."]}

    sizes = [len(c.isbns) for c in clusters]
    biggest = max(sizes)
    clusters = [
        # Minority taste: a smaller part of the shelf than the main genre.
        replace(c, exploratory=len(clusters) > 1 and sizes[i] < biggest)
        for i, c in enumerate(clusters)
    ]

    # Ranked hits per cluster, minus other editions of shelf / excluded books.
    blocked_titles = {
        _norm_title(b.title)
        for b in (state.get("shelf") or []) + (state.get("excluded") or [])
        if b.title
    }
    per_cluster: dict[int, list[SimilarBook]] = {i: [] for i in range(len(clusters))}
    dropped = 0
    for idx, found in state.get("hits") or []:
        for hit in found:
            if hit.title and _norm_title(hit.title) in blocked_titles:
                dropped += 1
                continue
            per_cluster[idx].append(hit)

    # A book found by several clusters belongs to the one where it scored best.
    owner: dict[str, tuple[float, int]] = {}
    for idx, found in per_cluster.items():
        for hit in found:
            key = _book_key(hit)
            if key not in owner or hit.score > owner[key][0]:
                owner[key] = (hit.score, idx)

    top_k = state["top_k"]
    quota = _allocate(sizes, top_k)
    priority = sorted(range(len(clusters)), key=lambda i: (-sizes[i], i))
    taken: dict[str, int] = {}  # book key -> cluster it was placed under

    # Phase 1: each cluster fills its own quota with books it owns.
    for idx in priority:
        own = [h for h in per_cluster[idx] if owner[_book_key(h)][1] == idx]
        for hit in sorted(own, key=lambda h: h.score, reverse=True)[: quota[idx]]:
            taken[_book_key(hit)] = idx

    # Phase 2: slots a cluster couldn't fill go to the best remaining books.
    # (Scores come from different queries, so this is only a rough tiebreak.)
    leftovers: dict[str, SimilarBook] = {}
    for found in per_cluster.values():
        for hit in found:
            key = _book_key(hit)
            if key not in taken and (key not in leftovers or hit.score > leftovers[key].score):
                leftovers[key] = hit
    backfill = sorted(leftovers.values(), key=lambda h: h.score, reverse=True)
    for hit in backfill[: max(0, top_k - len(taken))]:
        taken[_book_key(hit)] = owner[_book_key(hit)][1]

    # Build the result, grouped by cluster priority, best score first within.
    best_hit: dict[str, SimilarBook] = {}
    for found in per_cluster.values():
        for hit in found:
            key = _book_key(hit)
            if key in taken and (key not in best_hit or hit.score > best_hit[key].score):
                best_hit[key] = hit
    # Ordered by cluster index (never by label — labels aren't unique or stable).
    rank = {idx: n for n, idx in enumerate(priority)}
    placed = sorted(taken.items(), key=lambda kv: (rank[kv[1]], -best_hit[kv[0]].score))
    books = [
        replace(best_hit[key], cluster=clusters[idx].label, exploratory=clusters[idx].exploratory)
        for key, idx in placed
    ]

    notes: list[str] = []
    if dropped:
        notes.append(f"Dropped {dropped} hit(s) that matched a shelf or excluded book by title.")
    if len(books) < top_k:
        notes.append(f"Only {len(books)} of {top_k} slots could be filled.")
    return {"books": books[:top_k], "clusters": clusters, "notes": notes}


def _allocate(sizes: list[int], top_k: int) -> list[int]:
    """Slots per cluster: one floor slot each, the rest proportional to size.

    With more clusters than slots, the largest clusters get one slot each.
    The remainder is split by the largest-remainder method; ties go to the
    larger cluster, then to the earlier one.
    """
    n = len(sizes)
    by_size = sorted(range(n), key=lambda i: (-sizes[i], i))
    quota = [0] * n
    if n >= top_k:
        for i in by_size[:top_k]:
            quota[i] = 1
        return quota

    quota = [1] * n
    spare = top_k - n
    total = sum(sizes)
    # Integer arithmetic on purpose: with floats, equal remainders differ in the
    # last bit (4*3/9 - 1 < 1*3/9), so the "larger cluster wins ties" rule
    # silently failed and a 1-book cluster took a 4-book cluster's slot.
    whole = [spare * s // total for s in sizes]
    remainder = [spare * s % total for s in sizes]
    for i in range(n):
        quota[i] += whole[i]
    left = spare - sum(whole)
    for i in sorted(range(n), key=lambda i: (-remainder[i], -sizes[i], i))[:left]:
        quota[i] += 1
    return quota


# -- helpers -----------------------------------------------------------------

def _default_llm() -> LLM:
    """The project's Claude model via the raw anthropic SDK, created lazily."""
    cfg = AgentConfig.from_env()
    client: anthropic.Anthropic | None = None

    def call(system: str, user: str) -> str:
        nonlocal client
        client = client or anthropic.Anthropic()
        # No `temperature`: this SDK build doesn't accept it (see module doc).
        resp = client.messages.create(
            model=cfg.model,
            # Room for the cluster JSON of a whole library (every ISBN is echoed).
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    return call


def _cluster_prompt(shelf: list[Book]) -> str:
    lines = []
    for b in shelf:
        desc = " ".join((b.description or "").split())
        if len(desc) > 300:
            desc = desc[:300] + "..."
        authors = ", ".join(b.authors) or "unknown author"
        line = f"- [{b.isbn}] {b.title or '(untitled)'} — {authors}\n"
        if b.genres:
            line += f"    genres: {', '.join(b.genres)}\n"
        if b.subjects:
            line += f"    subjects: {', '.join(b.subjects[:8])}\n"
        line += f"    description: {desc or '(none)'}"
        lines.append(line)
    return f"Cluster these {len(shelf)} books:\n\n" + "\n".join(lines)


def _parse_json(text: str) -> Any:
    """Parse a JSON reply, tolerating ```json fences and prose around the object."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        return json.loads(t.strip())
    except json.JSONDecodeError:
        # "Here are the clusters: {...}" — take the outermost object.
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(t[start : end + 1])


def _parse_clusters(text: str, shelf: list[Book]) -> tuple[list[Cluster], list[str]]:
    """Validate the model's clustering: every shelf book in exactly one cluster.

    Unknown ISBNs are ignored, duplicates keep their first cluster, and books
    the model left out each get their own cluster. Unparseable output falls back
    to one cluster for the whole shelf.
    """
    by_isbn = {b.isbn: b for b in shelf}
    try:
        raw = _parse_json(text).get("clusters")
        if not isinstance(raw, list):
            raise ValueError("no 'clusters' list")
    except (ValueError, AttributeError) as e:  # JSONDecodeError is a ValueError
        return (
            [Cluster(WHOLE_SHELF_LABEL, list(by_isbn))],
            [f"Clustering output was unusable ({e}); searched with the whole shelf as one group."],
        )

    clusters: list[Cluster] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        members = []
        for value in item.get("isbns") or []:
            key = to_isbn13(value)
            if key in by_isbn and key not in seen:
                seen.add(key)
                members.append(key)
        if members:
            label = str(item.get("label") or "").strip() or "unlabeled"
            clusters.append(Cluster(label, members))

    notes: list[str] = []
    missing = [i for i in by_isbn if i not in seen]
    for isbn in missing:
        clusters.append(Cluster(_single_label(by_isbn[isbn]), [isbn]))
    if missing:
        notes.append(f"{len(missing)} book(s) the model left unclustered got a group of their own.")
    return clusters, notes


def _single_label(book: Book) -> str:
    return f"like {book.title}" if book.title else "like this book"


def _norm_title(title: str | None) -> str:
    """Comparable title: lowercase, no (series)/[format] notes, no subtitle."""
    t = (title or "").casefold()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t).split(":")[0]
    return " ".join(re.findall(r"\w+", t))


def _book_key(hit: SimilarBook) -> str:
    if hit.isbn:
        return to_isbn13(hit.isbn) or hit.isbn
    if hit.title:
        return f"title:{_norm_title(hit.title)}"
    return f"chunk:{hit.snippet[:48]}"


if __name__ == "__main__":
    import sys

    from tools.get_book_details import GetBookDetailsError, get_book_details

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    isbns = sys.argv[1:] or ["9780441013593", "9780547928227", "9780756404741", "9780307588371"]

    shelf: list[Book] = []
    for isbn in isbns:
        try:
            found = get_book_details(isbn)
        except GetBookDetailsError as exc:
            print(f"SKIPPED {isbn}: {exc}")
            continue
        if found is None:
            print(f"SKIPPED {isbn}: no Open Library record")
        else:
            shelf.append(found)

    try:
        result = find_similar_by_bookshelf_detailed(shelf)
    except FindSimilarByBookshelfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    for s in result.skipped:
        print(f"SKIPPED {s}")
    print("\nClusters (and the query each sent to the KB):")
    for c in result.clusters:
        flag = "  (exploratory)" if c.exploratory else ""
        print(f"  • {c.label}{flag}: {len(c.isbns)} book(s)")
        print(f"      query: {c.query[:200]}{'…' if len(c.query or '') > 200 else ''}")
    print("\nRecommendations:")
    for b in result.books:
        flag = " *exploratory*" if b.exploratory else ""
        print(f"  {b.score:.3f}  {b.title or '?'}  [{b.isbn or 'no-isbn'}]  <- {b.cluster}{flag}")
    for n in result.notes:
        print(f"note: {n}")
