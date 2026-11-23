"""parse_bookshelf — read the books off a bookshelf photo and match them to the
catalog. The third LangGraph-backed tool (DESIGN.md §5 / OPEN_QUESTIONS T3).

A short graph: a vision model reads the spines, then a deterministic matcher
resolves each to a real catalog book.

    extract   Claude vision: spine -> {title, title_en, authors, language}
    match     per book, cascade catalog by language (detected -> en -> any),
              fuzzy title + author match -> best catalog book, or dropped

The image is handled **in memory only** (base64 into the vision call) and never
persisted — a deliberate stage-1 choice (no S3; see OPEN_QUESTIONS D1).

Why match against the **catalog**, not the Bedrock KB: the catalog is a superset
of the KB's books (T2) and has a real `language` column, so the language cascade
and an exact-ish title match are precise and testable there; the KB (semantic)
would add nothing and can't filter by language. Cross-language spines are handled
at the vision step instead — the model emits a canonical/English `title_en` to
match on, and authors (language-stable) anchor the match.

Unmatched spines are **omitted** (not shown); a note reports how many were read
vs matched.

Everything is injectable (`vision`, `load_catalog`) so tests run with no
Anthropic/AWS/DB. Direct test needs ANTHROPIC_API_KEY + DB + an image path:
    python -m tools.parse_bookshelf shelf.jpg
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, TypedDict

import anthropic

from config import AgentConfig  # importing config also loads .env

logger = logging.getLogger(__name__)

# (image_b64, media_type) -> model reply text. Injectable for tests.
Vision = Callable[[str, str], str]
# () -> catalog rows (dicts with book_id/isbn/title/authors/language/cover_url).
CatalogLoader = Callable[[], list[dict]]

# Title similarity needed to accept a match on its own, and the lower bar that
# is accepted only when an author also matches (authors are language-stable, so
# they strongly disambiguate — and guard against a loose title-only false hit).
_TITLE_ACCEPT = 0.82
_TITLE_WITH_AUTHOR = 0.55

VISION_SYSTEM = (
    "You read the books visible on a photo of a bookshelf and report them as data.\n"
    "For every book whose spine you can read, give:\n"
    "- title: the title exactly as printed on the spine (its own language)\n"
    "- title_en: the book's canonical English (or original) title, so it can be "
    "matched across languages; repeat `title` if it is already English\n"
    "- authors: a list of author names (empty if not legible)\n"
    "- language: the ISO 639-1 code of the spine's language (e.g. en, es, de, fr), "
    "or null if unsure\n"
    "Skip spines you cannot read; do not guess titles that aren't visible.\n"
    'Return ONLY JSON: {"books":[{"title":"...","title_en":"...","authors":["..."],'
    '"language":"en"}]} — no prose.'
)
VISION_USER = "Read the books on this bookshelf."


class ParseBookshelfError(RuntimeError):
    """Raised when the photo could not be read (vision API failure)."""


@dataclass
class ExtractedBook:
    """One book the vision model read off a spine."""

    title: str
    title_en: str | None = None
    authors: list[str] = field(default_factory=list)
    language: str | None = None


@dataclass
class BookshelfMatch:
    """A spine resolved to a real catalog book."""

    book_id: int
    isbn: str
    title: str
    authors: list[str]
    cover_url: str | None
    language: str | None
    spine_title: str          # what the vision model read (may differ by language)
    already_on_shelf: bool


@dataclass
class ParseBookshelfResult:
    matches: list[BookshelfMatch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# -- public API --------------------------------------------------------------

def parse_bookshelf(
    image_bytes: bytes,
    media_type: str,
    *,
    shelf_book_ids: set[int] | None = None,
    vision: Vision | None = None,
    load_catalog: CatalogLoader | None = None,
) -> ParseBookshelfResult:
    """Read `image_bytes` and return catalog books matched from the spines.

    Args:
        image_bytes: the raw image (jpeg/png/webp/gif). Held in memory only.
        media_type: its content type, e.g. "image/jpeg".
        shelf_book_ids: book_ids already on the target shelf (to flag duplicates).
        vision, load_catalog: injectable dependencies for tests.

    Raises:
        ParseBookshelfError: the vision call failed.
    """
    catalog = (load_catalog or _load_catalog)()
    graph = _build_graph(vision or _default_vision())
    out = graph.invoke(
        {
            "image_b64": base64.b64encode(image_bytes).decode("ascii"),
            "media_type": media_type,
            "catalog": catalog,
            "shelf_book_ids": set(shelf_book_ids or ()),
        }
    )
    return ParseBookshelfResult(matches=out.get("matches") or [], notes=out.get("notes") or [])


# -- graph -------------------------------------------------------------------

class _State(TypedDict, total=False):
    image_b64: str
    media_type: str
    catalog: list[dict]
    shelf_book_ids: set[int]
    extracted: list[ExtractedBook]
    matches: list[BookshelfMatch]
    notes: list[str]


def _build_graph(vision: Vision):
    from langgraph.graph import END, START, StateGraph

    def extract(state: dict) -> dict:
        try:
            reply = vision(state["image_b64"], state["media_type"])
        except anthropic.APIError as e:
            raise ParseBookshelfError(f"Vision model failed: {e}") from e
        return {"extracted": _parse_extracted(reply)}

    def match(state: dict) -> dict:
        catalog = state.get("catalog") or []
        shelf_ids = state.get("shelf_book_ids") or set()
        extracted = state.get("extracted") or []
        matches: list[BookshelfMatch] = []
        seen: set[int] = set()
        for book in extracted:
            row = _match_one(book, catalog)
            if row and row["book_id"] not in seen:
                seen.add(row["book_id"])
                matches.append(
                    BookshelfMatch(
                        book_id=row["book_id"],
                        isbn=row["isbn"],
                        title=row["title"],
                        authors=list(row["authors"] or []),
                        cover_url=row["cover_url"],
                        language=row["language"],
                        spine_title=book.title,
                        already_on_shelf=row["book_id"] in shelf_ids,
                    )
                )
        if not extracted:
            notes = ["No readable spines were found in the photo."]
        else:
            notes = [f"Read {len(extracted)} spine(s), matched {len(matches)} to the catalog."]
        return {"matches": matches, "notes": notes}

    g = StateGraph(_State)
    g.add_node("extract", extract)
    g.add_node("match", match)
    g.add_edge(START, "extract")
    g.add_edge("extract", "match")
    g.add_edge("match", END)
    return g.compile()


# -- matching (deterministic) ------------------------------------------------

def _match_one(book: ExtractedBook, catalog: list[dict]) -> dict | None:
    """Best catalog row for one extracted book, or None.

    Cascades by language: the spine's detected language first, then English,
    then any. Within a tier, the best title match (boosted when an author also
    matches) that clears the threshold wins; the first tier with a match returns.
    """
    target = _norm(book.title_en or book.title)
    if not target:
        return None
    target_authors = {_norm(a) for a in (book.authors or []) if a}

    for lang in _language_tiers(book.language):
        subset = catalog if lang is None else [r for r in catalog if r.get("language") == lang]
        best: dict | None = None
        best_key = (0.0, 0.0)
        for row in subset:
            ratio = SequenceMatcher(None, target, _norm(row.get("title"))).ratio()
            author_hit = 1.0 if (
                target_authors and target_authors & {_norm(a) for a in (row.get("authors") or [])}
            ) else 0.0
            accepted = ratio >= _TITLE_ACCEPT or (ratio >= _TITLE_WITH_AUTHOR and author_hit)
            if accepted and (author_hit, ratio) > best_key:
                best_key = (author_hit, ratio)
                best = row
        if best is not None:
            return best
    return None


def _language_tiers(detected: str | None) -> list[str | None]:
    """Preference order: detected language (if not English) -> English -> any."""
    tiers: list[str | None] = []
    if detected and detected != "en":
        tiers.append(detected)
    tiers.append("en")
    tiers.append(None)  # any language
    return tiers


def _norm(value: Any) -> str:
    """Comparable title/name: lowercase, no (series)/[format] notes, no subtitle."""
    text = (value or "")
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", str(text).casefold()).split(":")[0]
    return " ".join(re.findall(r"\w+", text))


# -- vision helpers ----------------------------------------------------------

def _parse_extracted(reply: str) -> list[ExtractedBook]:
    """Parse the model's JSON into ExtractedBook list, tolerating fences/prose."""
    data = _loads(reply)
    items = data.get("books") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    books: list[ExtractedBook] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title")) or _clean(item.get("title_en"))
        if not title:
            continue
        authors = [c for a in (item.get("authors") or []) if (c := _clean(a))]
        lang = _clean(item.get("language"))
        books.append(
            ExtractedBook(
                title=title,
                title_en=_clean(item.get("title_en")) or title,
                authors=authors,
                language=lang.lower() if lang else None,
            )
        )
    return books


def _loads(text: str) -> Any:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        for open_c, close_c in (("{", "}"), ("[", "]")):
            start, end = t.find(open_c), t.rfind(close_c)
            if start != -1 and end > start:
                try:
                    return json.loads(t[start : end + 1])
                except json.JSONDecodeError:
                    continue
    return {}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _default_vision() -> Vision:
    cfg = AgentConfig.from_env()
    client: anthropic.Anthropic | None = None

    def call(image_b64: str, media_type: str) -> str:
        nonlocal client
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg.model,
            max_tokens=2048,
            system=VISION_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                        },
                        {"type": "text", "text": VISION_USER},
                    ],
                }
            ],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    return call


def _load_catalog() -> list[dict]:
    from db import pool

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT book_id, isbn, title, authors, language, cover_url FROM books"
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m tools.parse_bookshelf <image-path>", file=sys.stderr)
        raise SystemExit(2)
    path = sys.argv[1]
    media = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    with open(path, "rb") as f:
        raw = f.read()
    result = parse_bookshelf(raw, media)
    for n in result.notes:
        print(f"note: {n}")
    for m in result.matches:
        print(f"  {m.title}  [{m.isbn}]  <- read as '{m.spine_title}'")
