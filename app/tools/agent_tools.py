"""Tool definitions and dispatch for the "What's my next read?" assistant.

Six tools the agent may call (DESIGN §5A): five read-only data tools plus
`present_recommendations`, the control tool it calls to finish. The data tools
reuse the existing catalog queries and `tools/*`; they add no new reasoning. The
agent is the only reasoning layer, and the server verifies the agent's final
picks against Postgres before anything is shown (see api.py's validation gate).

`TOOLS` is the Anthropic tool-schema list; `run_tool` dispatches one call and
always returns a JSON-serializable value (an expected failure becomes
`{"error": ...}` so the agent can react instead of the turn crashing).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from db import pool
from isbn import to_isbn10, to_isbn13
from models import Book
from tools.find_similar import FindSimilarError, find_similar
from tools.find_similar_by_bookshelf import (
    FindSimilarByBookshelfError,
    find_similar_by_bookshelf_detailed,
)
from tools.get_book_details import GetBookDetailsError, get_book_details

logger = logging.getLogger(__name__)

# The control tool that ends the turn (the agent's final picks).
PRESENT_TOOL = "present_recommendations"

# Keep tool payloads small so one call can't flood the model's context.
_CATALOG_LIMIT = 20
_DESC_CHARS = 500


@dataclass
class ToolContext:
    """Per-request state the tools need: who is asking."""

    user_id: int


# -- schemas -----------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "search_catalog",
        "description": (
            "Search the app's book catalog by free text (title or author) and/or "
            "an exact genre. Use it for constraint-only requests and to resolve a "
            "title the reader names into a real catalog book. Returns matching "
            "books with their ISBN, title, authors, genres and a short blurb."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "title or author text"},
                "genre": {"type": "string", "description": "exact genre filter"},
                "limit": {"type": "integer", "description": f"max results (<= {_CATALOG_LIMIT})"},
            },
        },
    },
    {
        "name": "get_book_details",
        "description": "Full metadata for one book by its ISBN (from Open Library).",
        "input_schema": {
            "type": "object",
            "properties": {"isbn": {"type": "string"}},
            "required": ["isbn"],
        },
    },
    {
        "name": "find_similar",
        "description": (
            "Books similar to a description or a seed book's themes, from the "
            "knowledge base. Pass a rich description — resolve a named title with "
            "search_catalog/get_book_details first and pass its description/blurb. "
            "Returns KB hits (ISBN, title, snippet, relevance score)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_similar_by_bookshelf",
        "description": (
            "Books similar to a SET of books at once (the reader's shelf, or "
            "several titles they liked). Pass the catalog ISBNs; returns books "
            "grouped by the genre cluster they match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "isbns": {"type": "array", "items": {"type": "string"}},
                "top_k": {"type": "integer"},
            },
            "required": ["isbns"],
        },
    },
    {
        "name": "get_user_library",
        "description": (
            "The signed-in reader's shelves (with the books on each, the Exclusion "
            "shelf included) and their ratings. Use it to learn taste and to avoid "
            "recommending books they already have."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": PRESENT_TOOL,
        "description": (
            "Present your final recommendations to the reader. Call this once, at "
            "the very end. Each item is a catalog book's ISBN and a one-line reason "
            "it fits. Only include real books you found through the other tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recommendations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "isbn": {"type": "string"},
                            "why": {"type": "string"},
                        },
                        "required": ["isbn", "why"],
                    },
                }
            },
            "required": ["recommendations"],
        },
    },
]


# -- dispatch ----------------------------------------------------------------

def run_tool(name: str, tool_input: dict, ctx: ToolContext) -> Any:
    """Execute one tool call; return a JSON-serializable result.

    An expected tool failure becomes `{"error": ...}` rather than raising, so the
    agent can adjust and the turn keeps going. `present_recommendations` echoes
    the accepted picks; the loop detects that tool name and ends the turn.
    """
    try:
        if name == "search_catalog":
            return _search_catalog(tool_input)
        if name == "get_book_details":
            return _get_book_details(tool_input)
        if name == "find_similar":
            return _find_similar(tool_input)
        if name == "find_similar_by_bookshelf":
            return _find_similar_by_bookshelf(tool_input)
        if name == "get_user_library":
            return _get_user_library(ctx)
        if name == PRESENT_TOOL:
            return _present(tool_input)
    except Exception as e:  # defensive: a tool bug must not crash the turn
        logger.warning("tool %s failed: %s", name, e)
        return {"error": f"{name} failed: {e}"}
    return {"error": f"unknown tool: {name}"}


def _search_catalog(inp: dict) -> list[dict]:
    q = (inp.get("query") or "").strip() or None
    genre = (inp.get("genre") or "").strip() or None
    limit = max(1, min(int(inp.get("limit") or 8), _CATALOG_LIMIT))
    q_like = f"%{q}%" if q else None
    where = (
        "WHERE (%(q)s::text IS NULL OR b.title ILIKE %(q)s::text "
        "       OR EXISTS (SELECT 1 FROM unnest(b.authors) a WHERE a ILIKE %(q)s::text)) "
        "  AND (%(genre)s::text IS NULL OR %(genre)s::text = ANY(b.genres))"
    )
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT b.isbn, b.title, b.authors, b.genres, b.published_year, "
            "       left(b.description, %(dc)s) AS description "
            f"FROM books b {where} ORDER BY b.title LIMIT %(limit)s",
            {"q": q_like, "genre": genre, "limit": limit, "dc": _DESC_CHARS},
        ).fetchall()
    return [
        {
            "isbn": r["isbn"],
            "title": r["title"],
            "authors": list(r["authors"] or []),
            "genres": list(r["genres"] or []),
            "year": r["published_year"],
            "description": r["description"],
        }
        for r in rows
    ]


def _get_book_details(inp: dict) -> dict:
    isbn = (inp.get("isbn") or "").strip()
    if not isbn:
        return {"error": "isbn is required"}
    try:
        book = get_book_details(isbn)
    except GetBookDetailsError as e:
        return {"error": str(e)}
    if book is None:
        return {"found": False}
    return {
        "found": True,
        "isbn": book.isbn,
        "title": book.title,
        "authors": book.authors,
        "pages": book.pages,
        "year": book.publish_year,
        "subjects": book.subjects[:8],
        "description": (book.description or "")[:_DESC_CHARS],
    }


def _find_similar(inp: dict) -> Any:
    query = (inp.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    top_k = max(1, min(int(inp.get("top_k") or 5), 10))
    try:
        hits = find_similar(query, top_k=top_k)
    except FindSimilarError as e:
        return {"error": str(e)}
    return [
        {
            "isbn": h.isbn,
            "title": h.title,
            "score": round(h.score, 3),
            "snippet": (h.snippet or "")[:300],
        }
        for h in hits
    ]


def _find_similar_by_bookshelf(inp: dict) -> Any:
    isbns = [str(i) for i in (inp.get("isbns") or []) if i]
    if not isbns:
        return {"error": "isbns is required"}
    books = _resolve_books(isbns)
    if not books:
        return {"error": "none of those ISBNs are in the catalog"}
    top_k = max(1, min(int(inp.get("top_k") or 6), 12))
    try:
        result = find_similar_by_bookshelf_detailed(books, top_k=top_k)
    except FindSimilarByBookshelfError as e:
        return {"error": str(e)}
    return {
        "books": [
            {
                "isbn": b.isbn,
                "title": b.title,
                "cluster": b.cluster,
                "exploratory": b.exploratory,
                "score": round(b.score, 3),
            }
            for b in result.books
        ],
        "notes": result.notes,
    }


def _get_user_library(ctx: ToolContext) -> dict:
    with pool.connection() as conn:
        shelf_rows = conn.execute(
            "SELECT s.name, s.is_exclusion, b.isbn, b.title "
            "FROM shelves s "
            "LEFT JOIN shelf_books sb ON sb.shelf_id = s.shelf_id "
            "LEFT JOIN books b ON b.book_id = sb.book_id "
            "WHERE s.user_id = %s "
            "ORDER BY s.is_exclusion DESC, lower(s.name), b.title",
            (ctx.user_id,),
        ).fetchall()
        rating_rows = conn.execute(
            "SELECT b.isbn, b.title, ul.rating "
            "FROM user_library ul JOIN books b ON b.book_id = ul.book_id "
            "WHERE ul.user_id = %s AND ul.rating IS NOT NULL "
            "ORDER BY ul.rating DESC",
            (ctx.user_id,),
        ).fetchall()
    shelves: dict[str, dict] = {}
    for r in shelf_rows:
        shelf = shelves.setdefault(
            r["name"], {"name": r["name"], "is_exclusion": r["is_exclusion"], "books": []}
        )
        if r["isbn"]:
            shelf["books"].append({"isbn": r["isbn"], "title": r["title"]})
    return {
        "shelves": list(shelves.values()),
        "ratings": [
            {"isbn": r["isbn"], "title": r["title"], "rating": r["rating"]}
            for r in rating_rows
        ],
    }


def _present(inp: dict) -> dict:
    """Validate the shape of the agent's final picks. The loop reads the picks
    from the tool input; this just confirms receipt back to the model."""
    recs = []
    for item in inp.get("recommendations") or []:
        isbn = str((item or {}).get("isbn") or "").strip()
        why = str((item or {}).get("why") or "").strip()
        if isbn:
            recs.append({"isbn": isbn, "why": why})
    return {"accepted": len(recs)}


# -- helpers -----------------------------------------------------------------

def _resolve_books(isbns: list[str]) -> list[Book]:
    """Catalog `Book`s for the given ISBNs (any form), for the bookshelf tool."""
    forms: list[str] = []
    for value in isbns:
        forms += _isbn_forms(value)
    if not forms:
        return []
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT b.isbn, b.title, b.authors, b.pages, b.published_year, "
            "       b.cover_url, b.description, b.source, b.genres "
            "FROM books b WHERE b.isbn = ANY(%s)",
            (forms,),
        ).fetchall()
    return [
        Book(
            isbn=r["isbn"],
            title=r["title"],
            authors=list(r["authors"] or []),
            pages=r["pages"],
            publish_year=r["published_year"],
            cover_url=r["cover_url"],
            description=r["description"],
            source=r["source"],
            genres=list(r["genres"] or []),
        )
        for r in rows
    ]


def _isbn_forms(value: Any) -> list[str]:
    """Every comparable form of an ISBN: the raw value plus ISBN-13 and ISBN-10."""
    forms: set[str] = set()
    if value:
        forms.add(str(value).strip())
        for convert in (to_isbn13, to_isbn10):
            form = convert(value)
            if form:
                forms.add(form)
    return [f for f in forms if f]
