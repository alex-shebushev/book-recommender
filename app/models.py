"""Shared data types for the Book Recommender."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SimilarBook:
    """A book returned by `find_similar`, resolved from a Bedrock KB hit.

    `isbn`/`title` come from the chunk's ingestion metadata; either may be None
    if the KB was ingested without it (see find_similar for the fallback).
    `score` is the retrieval relevance score; `snippet` is the matched chunk
    text; `source` is where the chunk came from (e.g. an S3 URI) — used for
    citations later.

    `cluster`/`exploratory` are set only by `find_similar_by_bookshelf`: the shelf
    cluster this hit was retrieved for, and whether that cluster is a minority
    taste ("you might also enjoy") rather than the shelf's main genre. Plain
    `find_similar` leaves them at their defaults.

    `excluded` marks a hit on the reader's Exclusion shelf: it is kept (not
    dropped) and flagged so the UI can hide it or show it grayed. Set from the
    `flag_isbns` / `soft_exclude` arguments; off by default.
    """

    isbn: str | None
    title: str | None
    score: float
    snippet: str
    source: str | None
    cluster: str | None = None
    exploratory: bool = False
    excluded: bool = False


@dataclass
class ReviewDoc:
    """One passage about a book from the Knowledge Base, as returned by
    `get_reviews_and_guides`.

    `text` is the whole passage, not a preview: it is the grounding the agent
    quotes from. `source_id` identifies this exact passage for citations (the
    Bedrock chunk id; it changes if the KB is re-ingested) and `source` is the
    file it came from. `isbn` is the ISBN-13 form of the KB's own ISBN, so it
    can differ from the ISBN that was looked up when the KB holds another
    edition. `score` only ranks passages of the same book against the question;
    it is not comparable between calls.
    """

    isbn: str | None
    title: str | None
    author: str | None
    text: str
    score: float
    source: str | None
    source_id: str | None
    doc_type: str | None


@dataclass
class Book:
    """Full catalog detail for one book, as resolved by `get_book_details`, or
    built from a catalog DB row (the web API does this for bookshelf input).

    From Open Library, fields are best-effort: its data is crowd-sourced and
    uneven, so any of these may be missing. `get_book_details` always reports
    `isbn` in ISBN-13 form; a catalog row carries the catalog's ISBN-10. `source`
    is where the data came from (for provenance / citations). `genres` is the
    catalog's clean genre list; Open Library only fills the noisier `subjects`.
    """

    isbn: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    pages: int | None = None
    publish_year: int | None = None
    subjects: list[str] = field(default_factory=list)
    cover_url: str | None = None
    description: str | None = None
    source: str | None = None
    genres: list[str] = field(default_factory=list)
