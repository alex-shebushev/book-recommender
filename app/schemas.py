"""Pydantic response/request models for the web API.

Separate from `models.py` (the agent tools' dataclasses) on purpose: these shape
the JSON the React UI consumes, and only expose UI-facing fields. `content_warnings`
is intentionally omitted this slice (deferred — see DESIGN.md §6 / OPEN_QUESTIONS).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BookSummary(BaseModel):
    """Compact book shape for the catalog grid and shelf cards."""

    book_id: int
    isbn: str
    title: str
    authors: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    published_year: int | None = None
    cover_url: str | None = None


class BookDetail(BookSummary):
    """Full book shape for the details page."""

    description: str | None = None
    pages: int | None = None
    language: str | None = None
    source: str | None = None
    # The signed-in user's own rating/review of this book, if any.
    my_rating: int | None = None
    my_review: str | None = None
    # The signed-in user's shelves this book sits on (Exclusion included).
    shelf_ids: list[int] = Field(default_factory=list)


class BookList(BaseModel):
    """Paginated catalog response."""

    items: list[BookSummary]
    total: int
    limit: int
    offset: int


class ShelfEntry(BookSummary):
    """A book on one of the user's shelves, with the user's rating if any."""

    rating: int | None = None
    review: str | None = None


class NamedShelf(BaseModel):
    """One of the user's shelves, with its books. `is_exclusion` marks the
    built-in Exclusion shelf: its books are never used as taste and never
    recommended, and it can't be renamed or deleted."""

    shelf_id: int
    name: str
    is_exclusion: bool = False
    books: list[ShelfEntry] = Field(default_factory=list)


class ShelfSummary(BaseModel):
    """A shelf plus its book count (for lists / the book page's shelf picker)."""

    shelf_id: int
    name: str
    is_exclusion: bool = False
    book_count: int = 0


class Library(BaseModel):
    """The signed-in user's whole library: every shelf with its books, the
    Exclusion shelf first. `total` counts distinct books on normal shelves;
    `excluded` counts books on Exclusion."""

    user_id: int
    shelves: list[NamedShelf] = Field(default_factory=list)
    total: int = 0
    excluded: int = 0


class CreateShelfRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class BulkAddRequest(BaseModel):
    """Add several books to a shelf at once (used by the photo-import confirm)."""

    book_ids: list[int] = Field(default_factory=list)


class ParsedCandidate(BaseModel):
    """One book matched from a bookshelf photo, for the review/confirm panel.
    `spine_title` is what the vision model read (may differ by language);
    `already_on_shelf` marks books the shelf already has."""

    book_id: int
    isbn: str
    title: str
    author: str | None = None
    cover_url: str | None = None
    language: str | None = None
    spine_title: str | None = None
    already_on_shelf: bool = False


class ParseResult(BaseModel):
    """Response of the parse-photo endpoint: matched candidates + notes
    (e.g. "read N spines, matched M"). Unmatched spines are omitted."""

    candidates: list[ParsedCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RenameShelfRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


# -- Auth ---------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    user_id: int
    email: str
    display_name: str | None = None


class RatingUpsert(BaseModel):
    """Set the signed-in user's rating and/or review of a book. Only the fields
    sent are changed; send null to clear one. Independent of shelves."""

    rating: int | None = Field(default=None, ge=1, le=5)
    review: str | None = None


class RatingOut(BaseModel):
    rating: int | None = None
    review: str | None = None


class SimilarResult(BaseModel):
    """One book returned by `find_similar` (Bedrock KB). `book_id`/`cover_url`/
    `author` are set only when the hit's ISBN also exists in our catalog, so the
    UI can link to it; otherwise it's a KB-only result shown with its grounding
    snippet. `score` is the retrieval relevance; `snippet` is the grounding text.
    `excluded` = this book is on the signed-in reader's Exclusion shelf; it is
    returned so the UI can hide it or show it grayed."""

    title: str | None = None
    isbn: str | None = None
    score: float
    snippet: str
    source: str | None = None
    book_id: int | None = None
    cover_url: str | None = None
    author: str | None = None
    excluded: bool = False


class RecommendedBook(SimilarResult):
    """One bookshelf recommendation: a SimilarResult plus the part of the shelf
    it matches. `exploratory` = it matches a minority taste on the shelf."""

    cluster: str | None = None
    exploratory: bool = False


class RecommendationCluster(BaseModel):
    """One genre group the shelf was split into, and the query written for it."""

    label: str
    size: int
    exploratory: bool = False
    query: str | None = None  # the exact text sent to the KB for this cluster


class Recommendations(BaseModel):
    """Response of the bookshelf recommendation endpoints."""

    items: list[RecommendedBook]
    clusters: list[RecommendationCluster] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# -- "What's my next read?" assistant (DESIGN §5A) ----------------------------

class AssistantRequest(BaseModel):
    """One message to the conversational assistant. `session_id` continues an
    existing conversation; omit it to start a new one."""

    message: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None


class AssistantRecommendation(BaseModel):
    """One verified recommendation from the assistant: a real catalog book plus
    the agent's reason. `excluded` = on the reader's Exclusion shelf (kept so the
    UI can hide it or gray it, like the Find-similar / recommendations panels)."""

    book_id: int
    isbn: str
    title: str
    author: str | None = None
    cover_url: str | None = None
    why: str = ""
    excluded: bool = False


class AssistantReply(BaseModel):
    """The assistant's answer for one turn. `recommendations` are verified against
    the catalog; `unverified` holds ISBNs the agent named that aren't in it (shown
    as visible uncertainty). `tool_trace` lists the tools the agent called."""

    session_id: int
    reply: str
    recommendations: list[AssistantRecommendation] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    tool_trace: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
