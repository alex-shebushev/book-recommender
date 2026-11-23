"""FastAPI web API for the Book Recommender — catalog, book details, bookshelf.

Serves two things from one origin:
  * JSON API under /api/*   (Postgres catalog + the signed-in user's library)
  * the React SPA           (static files + a deep-link fallback to index.html)

Auth: email + password, HTTP-only signed session cookie (SessionMiddleware).
No roles — every signed-in user has the same access. Books + details are public;
shelves, ratings and recommendations require sign-in. See DESIGN.md §6.

A user's library is their shelves. Every user has one built-in Exclusion shelf:
its books never seed recommendations and are never recommended. A book is on
Exclusion or on normal shelves, never both (a DB trigger enforces it).

Run (host dev, needs the compose Postgres up + migrations applied + seed):
    cd app && uvicorn api:app --reload
Then open http://localhost:8000/
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware

from auth import current_user, hash_password, require_user, verify_password
from db import pool
from isbn import to_isbn10, to_isbn13
from models import Book, SimilarBook
from schemas import (
    AssistantRecommendation,
    AssistantReply,
    AssistantRequest,
    BookDetail,
    BookList,
    BookSummary,
    BulkAddRequest,
    CreateShelfRequest,
    ParseResult,
    ParsedCandidate,
    Library,
    LoginRequest,
    NamedShelf,
    RatingOut,
    RatingUpsert,
    RecommendationCluster,
    RecommendedBook,
    Recommendations,
    RenameShelfRequest,
    ShelfEntry,
    ShelfSummary,
    SignupRequest,
    SimilarResult,
    UserOut,
)
from tools.find_similar import FindSimilarError, find_similar
from tools.find_similar_by_bookshelf import (
    FindSimilarByBookshelfError,
    find_similar_by_bookshelf_detailed,
)

import agent_store
from assistant import AssistantError, run_assistant
from config import AssistantConfig
from tools.agent_tools import ToolContext
from tools.parse_bookshelf import ParseBookshelfError, parse_bookshelf
from quota import QuotaExceeded, enforce_quota

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Signs the session cookie. Set a strong value in production (.env / platform).
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-change-me")

# Columns shared by the summary shape (catalog grid + shelf cards). Qualified
# with the `b` alias so they stay unambiguous in the JOINs (books + user_library
# both have book_id / user_id). Every query below aliases books AS b.
_SUMMARY_COLS = (
    "b.book_id, b.isbn, b.title, b.authors, b.genres, b.published_year, b.cover_url"
)
# Everything the recommendation tool uses to cluster a book.
_BOOK_COLS = _SUMMARY_COLS + ", b.description, b.pages, b.source"

# Bookshelf recommendations requested per run. A little over the ~8 shown so
# that, after the reader hides Exclusion-shelf hits, several usually remain.
RECOMMENDATIONS_TOP_K = 10
# Book-page "Find similar" results requested (some may be Exclusion-shelf hits).
SIMILAR_TOP_K = 8
# Genre filter chips: show only the most-used genres (the KB books carry hundreds).
GENRE_LIMIT = 30

# Settings for the conversational "next read" assistant (model, history window).
_assistant_config = AssistantConfig.from_env()

# Bookshelf-photo import: processed in memory only (no S3). Size cap + types.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Book Recommender API", version="0.2.0", lifespan=lifespan)

# Session cookie: HTTP-only + signed. same_site=lax is fine for a same-origin SPA;
# https_only stays off for local http dev (turn on behind TLS in production).
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="bookrec_session",
    same_site="lax",
    https_only=False,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- helpers ------------------------------------------------------------------

def _normalize_authors(value) -> list[str]:
    return list(value) if value else []


def _summary(row: dict) -> dict:
    return {**row, "authors": _normalize_authors(row["authors"]), "genres": list(row["genres"] or [])}


def _catalog_book(row: dict) -> Book:
    """A catalog row (selected with _BOOK_COLS) as the tools' Book type."""
    return Book(
        isbn=row["isbn"],
        title=row["title"],
        authors=_normalize_authors(row["authors"]),
        pages=row["pages"],
        publish_year=row["published_year"],
        cover_url=row["cover_url"],
        description=row["description"],
        source=row["source"],
        genres=list(row["genres"] or []),
    )


def _similar_fields(conn, hit: SimilarBook) -> dict:
    """SimilarResult fields for a KB hit, linked to our catalog when the hit's
    ISBN (in any form) is there; otherwise a KB-only result with its snippet."""
    fields = {"title": hit.title, "isbn": hit.isbn, "score": hit.score,
              "snippet": hit.snippet, "source": hit.source, "excluded": hit.excluded}
    forms = [f for f in {hit.isbn, to_isbn10(hit.isbn), to_isbn13(hit.isbn)} if f]
    if not forms:
        return fields
    cat = conn.execute(
        f"SELECT {_SUMMARY_COLS} FROM books b WHERE b.isbn = ANY(%s) LIMIT 1", (forms,)
    ).fetchone()
    if cat:
        authors = _normalize_authors(cat["authors"])
        fields.update(
            title=hit.title or cat["title"], book_id=cat["book_id"],
            cover_url=cat["cover_url"], author=authors[0] if authors else None,
        )
    return fields


def _shelf_rows(conn, uid: int, *, exclusion: bool) -> list[dict]:
    """Books on the user's normal shelves (exclusion=False) or on their
    Exclusion shelf (exclusion=True), as catalog rows (_BOOK_COLS)."""
    return conn.execute(
        f"SELECT {_BOOK_COLS} FROM books b WHERE b.book_id IN ("
        "  SELECT sb.book_id FROM shelf_books sb JOIN shelves s ON s.shelf_id = sb.shelf_id"
        "  WHERE s.user_id = %(uid)s AND s.is_exclusion = %(excl)s"
        ") ORDER BY b.title",
        {"uid": uid, "excl": exclusion},
    ).fetchall()


def _exclusion_isbns(conn, uid: int) -> list[str]:
    """ISBNs on the user's Exclusion shelf (for flagging book-page similar hits)."""
    return [
        r["isbn"]
        for r in conn.execute(
            "SELECT b.isbn FROM books b JOIN shelf_books sb ON sb.book_id = b.book_id "
            "JOIN shelves s ON s.shelf_id = sb.shelf_id "
            "WHERE s.user_id = %s AND s.is_exclusion",
            (uid,),
        ).fetchall()
    ]


def _recommend(seeds: list[dict], exclude: list[dict], soft_exclude: list[dict]) -> Recommendations:
    """Run find_similar_by_bookshelf on catalog rows and link hits to the catalog.

    `exclude` books are dropped; `soft_exclude` (the Exclusion shelf) are kept
    and tagged `excluded`. No DB connection is held while it runs (~20–30 s).
    """
    try:
        result = find_similar_by_bookshelf_detailed(
            [_catalog_book(r) for r in seeds],
            exclude=[_catalog_book(r) for r in exclude],
            soft_exclude=[_catalog_book(r) for r in soft_exclude],
            top_k=RECOMMENDATIONS_TOP_K,
        )
    except FindSimilarByBookshelfError as e:
        raise HTTPException(status_code=503, detail=f"Recommendations are unavailable right now: {e}")

    with pool.connection() as conn:
        items = [
            RecommendedBook(
                **_similar_fields(conn, b), cluster=b.cluster, exploratory=b.exploratory
            )
            for b in result.books
        ]
    return Recommendations(
        items=items,
        clusters=[
            RecommendationCluster(label=c.label, size=len(c.isbns), exploratory=c.exploratory, query=c.query)
            for c in result.clusters
        ],
        skipped=result.skipped,
        notes=result.notes,
    )


# -- auth ---------------------------------------------------------------------

@app.post("/api/auth/signup", response_model=UserOut, status_code=201)
def signup(body: SignupRequest, request: Request) -> UserOut:
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Please enter a valid email.")
    display = (body.display_name or "").strip() or email.split("@")[0]
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
            raise HTTPException(status_code=400, detail="That email is already registered.")
        # Both inserts share one transaction (committed on block exit).
        row = conn.execute(
            "INSERT INTO users (email, display_name) VALUES (%s, %s) "
            "RETURNING user_id, email, display_name",
            (email, display),
        ).fetchone()
        conn.execute(
            "INSERT INTO user_details (user_id, password_hash) VALUES (%s, %s)",
            (row["user_id"], hash_password(body.password)),
        )
    request.session["user_id"] = row["user_id"]
    return UserOut(**row)


@app.post("/api/auth/login", response_model=UserOut)
def login(body: LoginRequest, request: Request) -> UserOut:
    email = body.email.strip().lower()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT u.user_id, u.email, u.display_name, d.password_hash "
            "FROM users u JOIN user_details d ON d.user_id = u.user_id "
            "WHERE u.email = %s",
            (email,),
        ).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    request.session["user_id"] = row["user_id"]
    return UserOut(user_id=row["user_id"], email=row["email"], display_name=row["display_name"])


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    request.session.clear()
    return Response(status_code=204)


@app.get("/api/auth/me", response_model=UserOut)
def me(user: dict = Depends(require_user)) -> UserOut:
    return UserOut(**user)


# -- public catalog -----------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/genres")
def list_genres() -> list[str]:
    """The most-used genres for the filter chips — the top `GENRE_LIMIT` by how
    many books carry them, most first. The KB books bring hundreds of (often
    noisy) genres, so showing them all is unusable."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT genre FROM ("
            "  SELECT unnest(genres) AS genre FROM books WHERE genres IS NOT NULL"
            ") g GROUP BY genre ORDER BY count(*) DESC, genre LIMIT %s",
            (GENRE_LIMIT,),
        ).fetchall()
    return [r["genre"] for r in rows]


@app.get("/api/books", response_model=BookList)
def list_books(
    q: str | None = Query(None, description="search title or author"),
    genre: str | None = Query(None, description="exact genre filter"),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> BookList:
    q_like = f"%{q.strip()}%" if q and q.strip() else None
    params = {"q": q_like, "genre": genre, "limit": limit, "offset": offset}
    # ::text casts: a bare parameter used only in `IS NULL` has no inferable type.
    where = (
        "WHERE (%(q)s::text IS NULL OR b.title ILIKE %(q)s::text "
        "       OR EXISTS (SELECT 1 FROM unnest(b.authors) a WHERE a ILIKE %(q)s::text)) "
        "  AND (%(genre)s::text IS NULL OR %(genre)s::text = ANY(b.genres))"
    )
    with pool.connection() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM books b {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {_SUMMARY_COLS} FROM books b {where} "
            "ORDER BY b.title LIMIT %(limit)s OFFSET %(offset)s",
            params,
        ).fetchall()
    return BookList(
        items=[BookSummary(**_summary(r)) for r in rows],
        total=total, limit=limit, offset=offset,
    )


@app.get("/api/books/{book_id}", response_model=BookDetail)
def get_book(book_id: int, user: dict | None = Depends(current_user)) -> BookDetail:
    """Full detail for one book. Public — adds the signed-in user's rating and
    the ids of their shelves this book is on."""
    uid = user["user_id"] if user else None
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_SUMMARY_COLS}, b.description, b.pages, b.language, b.source, "
            "       ul.rating AS my_rating, ul.review AS my_review "
            "FROM books b "
            "LEFT JOIN user_library ul "
            "  ON ul.book_id = b.book_id AND ul.user_id = %(user_id)s "
            "WHERE b.book_id = %(book_id)s",
            {"book_id": book_id, "user_id": uid},
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Book not found")
    shelf_ids: list[int] = []
    if uid is not None:
        with pool.connection() as conn:
            shelf_ids = [
                r["shelf_id"]
                for r in conn.execute(
                    "SELECT sb.shelf_id FROM shelf_books sb "
                    "JOIN shelves s ON s.shelf_id = sb.shelf_id "
                    "WHERE s.user_id = %s AND sb.book_id = %s",
                    (uid, book_id),
                ).fetchall()
            ]
    return BookDetail(**_summary(row), shelf_ids=shelf_ids)


@app.get("/api/books/{book_id}/similar", response_model=list[SimilarResult])
def similar_books(book_id: int, user: dict | None = Depends(current_user)) -> list[SimilarResult]:
    """Books similar to this one, via the Bedrock KB `find_similar` tool.

    The book's description (or title) is the query; the book itself is excluded.
    For a signed-in reader, hits on their Exclusion shelf are kept but tagged
    `excluded` (the UI hides or grays them). Hits whose ISBN is also in our
    catalog carry a book_id/cover so the UI links to them; the rest are KB-only
    results. Returns 503 when the KB/AWS is unavailable, [] when nothing clears.
    """
    with pool.connection() as conn:
        book = conn.execute(
            "SELECT isbn, title, description FROM books WHERE book_id = %s", (book_id,)
        ).fetchone()
        if book is None:
            raise HTTPException(status_code=404, detail="Book not found")
        flag = _exclusion_isbns(conn, user["user_id"]) if user else []

    query = (book["description"] or book["title"] or "").strip()
    if not query:
        return []
    try:
        hits = find_similar(
            query, exclude_isbns=[book["isbn"]], flag_isbns=flag,
            top_k=SIMILAR_TOP_K, min_score=0.0,
        )
    except FindSimilarError as e:
        raise HTTPException(status_code=503, detail=f"Similar search is unavailable right now: {e}")

    with pool.connection() as conn:
        return [SimilarResult(**_similar_fields(conn, h)) for h in hits]


# -- library: shelves (sign-in required) ---------------------------------------

_SHELF_ORDER = "s.is_exclusion DESC, lower(s.name)"  # Exclusion first, then A-Z


def _own_shelf(conn, shelf_id: int, uid: int) -> dict:
    """The user's shelf row (shelf_id, name, is_exclusion), or 404."""
    shelf = conn.execute(
        "SELECT shelf_id, name, is_exclusion FROM shelves WHERE shelf_id = %s AND user_id = %s",
        (shelf_id, uid),
    ).fetchone()
    if shelf is None:
        raise HTTPException(status_code=404, detail="Shelf not found")
    return shelf


def _shelf_books(conn, uid: int, shelf_ids: list[int] | None = None) -> dict[int, list[ShelfEntry]]:
    """Books per shelf (with the user's rating) for the given shelves, or all."""
    rows = conn.execute(
        f"SELECT sb.shelf_id, {_SUMMARY_COLS}, ul.rating, ul.review "
        "FROM shelf_books sb "
        "JOIN shelves s ON s.shelf_id = sb.shelf_id "
        "JOIN books b ON b.book_id = sb.book_id "
        "LEFT JOIN user_library ul ON ul.user_id = s.user_id AND ul.book_id = sb.book_id "
        "WHERE s.user_id = %(uid)s "
        "  AND (%(ids)s::int[] IS NULL OR sb.shelf_id = ANY(%(ids)s::int[])) "
        "ORDER BY b.title",
        {"uid": uid, "ids": shelf_ids},
    ).fetchall()
    books: dict[int, list[ShelfEntry]] = {}
    for r in rows:
        books.setdefault(r["shelf_id"], []).append(ShelfEntry(**_summary(r)))
    return books


@app.get("/api/shelf", response_model=Library)
def get_library(user: dict = Depends(require_user)) -> Library:
    """The signed-in user's whole library: every shelf with its books,
    the Exclusion shelf first."""
    uid = user["user_id"]
    with pool.connection() as conn:
        shelves = conn.execute(
            f"SELECT s.shelf_id, s.name, s.is_exclusion FROM shelves s "
            f"WHERE s.user_id = %s ORDER BY {_SHELF_ORDER}",
            (uid,),
        ).fetchall()
        books = _shelf_books(conn, uid)
    named = [NamedShelf(**s, books=books.get(s["shelf_id"], [])) for s in shelves]
    return Library(
        user_id=uid,
        shelves=named,
        total=len({b.book_id for s in named if not s.is_exclusion for b in s.books}),
        excluded=len({b.book_id for s in named if s.is_exclusion for b in s.books}),
    )


# -- bookshelf recommendations (sign-in required) -----------------------------
# Both run find_similar_by_bookshelf. Seeds never include the Exclusion shelf.
# Normal-shelf books are dropped from the results; Exclusion-shelf books are
# kept but tagged `excluded`, so the UI can hide them or show them grayed.

@app.get("/api/shelf/recommendations", response_model=Recommendations)
def recommend_for_library(user: dict = Depends(require_user)) -> Recommendations:
    """Recommendations seeded by every book on the user's normal shelves."""
    uid = user["user_id"]
    with pool.connection() as conn:
        seeds = _shelf_rows(conn, uid, exclusion=False)
        soft = _shelf_rows(conn, uid, exclusion=True)
    if not seeds:
        raise HTTPException(
            status_code=400,
            detail="Put some books on a shelf first — books on Exclusion don't count as taste.",
        )
    return _recommend(seeds=seeds, exclude=seeds, soft_exclude=soft)


@app.get("/api/shelves/{shelf_id}/recommendations", response_model=Recommendations)
def recommend_for_shelf(shelf_id: int, user: dict = Depends(require_user)) -> Recommendations:
    """Recommendations seeded by the books on one normal shelf."""
    uid = user["user_id"]
    with pool.connection() as conn:
        if _own_shelf(conn, shelf_id, uid)["is_exclusion"]:
            raise HTTPException(
                status_code=400,
                detail="Books on Exclusion are never used as taste — pick another shelf.",
            )
        seeds = conn.execute(
            f"SELECT {_BOOK_COLS} FROM shelf_books sb JOIN books b ON b.book_id = sb.book_id "
            "WHERE sb.shelf_id = %s ORDER BY b.title",
            (shelf_id,),
        ).fetchall()
        on_normal_shelves = _shelf_rows(conn, uid, exclusion=False)
        soft = _shelf_rows(conn, uid, exclusion=True)
    if not seeds:
        raise HTTPException(status_code=400, detail="This shelf is empty — add some books first.")
    return _recommend(seeds=seeds, exclude=on_normal_shelves, soft_exclude=soft)


# -- shelves (sign-in required) -----------------------------------------------

@app.get("/api/shelves", response_model=list[ShelfSummary])
def list_shelves(user: dict = Depends(require_user)) -> list[ShelfSummary]:
    """The user's shelves with book counts, Exclusion first."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT s.shelf_id, s.name, s.is_exclusion, count(sb.book_id) AS book_count "
            "FROM shelves s LEFT JOIN shelf_books sb ON sb.shelf_id = s.shelf_id "
            "WHERE s.user_id = %s GROUP BY s.shelf_id, s.name, s.is_exclusion "
            f"ORDER BY {_SHELF_ORDER}",
            (user["user_id"],),
        ).fetchall()
    return [ShelfSummary(**r) for r in rows]


@app.get("/api/shelves/{shelf_id}", response_model=NamedShelf)
def get_one_shelf(shelf_id: int, user: dict = Depends(require_user)) -> NamedShelf:
    """One shelf with all its books (for the shelf's own screen)."""
    uid = user["user_id"]
    with pool.connection() as conn:
        shelf = _own_shelf(conn, shelf_id, uid)
        books = _shelf_books(conn, uid, [shelf_id]).get(shelf_id, [])
    return NamedShelf(**shelf, books=books)


@app.post("/api/shelves", response_model=ShelfSummary, status_code=201)
def create_shelf(body: CreateShelfRequest, user: dict = Depends(require_user)) -> ShelfSummary:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Shelf name is required.")
    uid = user["user_id"]
    with pool.connection() as conn:
        dup = conn.execute(
            "SELECT 1 FROM shelves WHERE user_id = %s AND lower(name) = lower(%s)", (uid, name)
        ).fetchone()
        if dup:
            raise HTTPException(status_code=400, detail="You already have a shelf with that name.")
        row = conn.execute(
            "INSERT INTO shelves (user_id, name) VALUES (%s, %s) RETURNING shelf_id, name",
            (uid, name),
        ).fetchone()
    return ShelfSummary(shelf_id=row["shelf_id"], name=row["name"], book_count=0)


@app.patch("/api/shelves/{shelf_id}", response_model=ShelfSummary)
def rename_shelf(shelf_id: int, body: RenameShelfRequest, user: dict = Depends(require_user)) -> ShelfSummary:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Shelf name is required.")
    uid = user["user_id"]
    with pool.connection() as conn:
        if _own_shelf(conn, shelf_id, uid)["is_exclusion"]:
            raise HTTPException(status_code=400, detail="The Exclusion shelf can't be renamed.")
        if conn.execute(
            "SELECT 1 FROM shelves WHERE user_id = %s AND lower(name) = lower(%s) AND shelf_id <> %s",
            (uid, name, shelf_id),
        ).fetchone():
            raise HTTPException(status_code=400, detail="You already have a shelf with that name.")
        conn.execute("UPDATE shelves SET name = %s WHERE shelf_id = %s", (name, shelf_id))
        count = conn.execute(
            "SELECT count(*) AS n FROM shelf_books WHERE shelf_id = %s", (shelf_id,)
        ).fetchone()["n"]
    return ShelfSummary(shelf_id=shelf_id, name=name, book_count=count)


@app.delete("/api/shelves/{shelf_id}", status_code=204)
def delete_shelf(shelf_id: int, user: dict = Depends(require_user)) -> Response:
    """Delete a normal shelf (its books simply leave that shelf)."""
    with pool.connection() as conn:
        if _own_shelf(conn, shelf_id, user["user_id"])["is_exclusion"]:
            raise HTTPException(status_code=400, detail="The Exclusion shelf can't be deleted.")
        conn.execute("DELETE FROM shelves WHERE shelf_id = %s", (shelf_id,))
    return Response(status_code=204)


@app.put("/api/shelves/{shelf_id}/books/{book_id}", status_code=204)
def add_book_to_shelf(shelf_id: int, book_id: int, user: dict = Depends(require_user)) -> Response:
    """Put a book on a shelf. Adding to Exclusion takes it off every normal
    shelf, and adding to a normal shelf takes it off Exclusion (DB trigger)."""
    with pool.connection() as conn:
        _own_shelf(conn, shelf_id, user["user_id"])
        if not conn.execute("SELECT 1 FROM books WHERE book_id = %s", (book_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Book not found")
        conn.execute(
            "INSERT INTO shelf_books (shelf_id, book_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (shelf_id, book_id),
        )
    return Response(status_code=204)


@app.delete("/api/shelves/{shelf_id}/books/{book_id}", status_code=204)
def remove_book_from_shelf(shelf_id: int, book_id: int, user: dict = Depends(require_user)) -> Response:
    """Take a book off one shelf (it stays on any other shelves)."""
    with pool.connection() as conn:
        _own_shelf(conn, shelf_id, user["user_id"])
        conn.execute(
            "DELETE FROM shelf_books WHERE shelf_id = %s AND book_id = %s", (shelf_id, book_id)
        )
    return Response(status_code=204)


# -- bookshelf photo import (sign-in required) --------------------------------

@app.post("/api/shelves/{shelf_id}/parse-photo", response_model=ParseResult)
def parse_shelf_photo(
    shelf_id: int, file: UploadFile = File(...), user: dict = Depends(require_user)
) -> ParseResult:
    """Read books off an uploaded bookshelf photo and match them to the catalog.

    The image is processed in memory and never stored. Returns matched candidates
    for the reader to review before confirming. Not allowed on the Exclusion shelf.
    """
    uid = user["user_id"]
    with pool.connection() as conn:
        if _own_shelf(conn, shelf_id, uid)["is_exclusion"]:
            raise HTTPException(status_code=400, detail="Can't add scanned books to the Exclusion shelf.")
        shelf_book_ids = {
            r["book_id"]
            for r in conn.execute(
                "SELECT book_id FROM shelf_books WHERE shelf_id = %s", (shelf_id,)
            ).fetchall()
        }

    media_type = (file.content_type or "").lower()
    if media_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Upload a JPEG, PNG, WebP or GIF image.")
    data = file.file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The image is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is too large (max 5 MB).")

    try:
        result = parse_bookshelf(data, media_type, shelf_book_ids=shelf_book_ids)
    except ParseBookshelfError as e:
        raise HTTPException(status_code=503, detail=f"Couldn't read the photo right now: {e}")

    candidates = [
        ParsedCandidate(
            book_id=m.book_id,
            isbn=m.isbn,
            title=m.title,
            author=m.authors[0] if m.authors else None,
            cover_url=m.cover_url,
            language=m.language,
            spine_title=m.spine_title,
            already_on_shelf=m.already_on_shelf,
        )
        for m in result.matches
    ]
    return ParseResult(candidates=candidates, notes=result.notes)


@app.post("/api/shelves/{shelf_id}/books/bulk", response_model=NamedShelf)
def bulk_add_books(
    shelf_id: int, body: BulkAddRequest, user: dict = Depends(require_user)
) -> NamedShelf:
    """Add several books to a shelf at once (the photo-import confirm). Unknown
    book_ids are skipped; the Exclusion trigger still applies. Returns the shelf."""
    uid = user["user_id"]
    with pool.connection() as conn:
        shelf = _own_shelf(conn, shelf_id, uid)
        for book_id in dict.fromkeys(body.book_ids):  # de-dupe, keep order
            if conn.execute("SELECT 1 FROM books WHERE book_id = %s", (book_id,)).fetchone():
                conn.execute(
                    "INSERT INTO shelf_books (shelf_id, book_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (shelf_id, book_id),
                )
        books = _shelf_books(conn, uid, [shelf_id]).get(shelf_id, [])
    return NamedShelf(**shelf, books=books)


# -- ratings (sign-in required) -----------------------------------------------

@app.put("/api/books/{book_id}/rating", response_model=RatingOut)
def set_rating(book_id: int, body: RatingUpsert, user: dict = Depends(require_user)) -> RatingOut:
    """Set the user's rating and/or review of a book, independent of shelves.
    Only the fields sent change; when both end up empty the record is removed."""
    uid = user["user_id"]
    sent = body.model_dump(exclude_unset=True)
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM books WHERE book_id = %s", (book_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Book not found")
        current = conn.execute(
            "SELECT rating, review FROM user_library WHERE user_id = %s AND book_id = %s",
            (uid, book_id),
        ).fetchone() or {"rating": None, "review": None}
        rating = sent["rating"] if "rating" in sent else current["rating"]
        review = sent["review"] if "review" in sent else current["review"]
        review = (review or "").strip() or None
        if rating is None and review is None:
            conn.execute(
                "DELETE FROM user_library WHERE user_id = %s AND book_id = %s", (uid, book_id)
            )
        else:
            conn.execute(
                "INSERT INTO user_library (user_id, book_id, rating, review) "
                "VALUES (%(uid)s, %(bid)s, %(rating)s, %(review)s) "
                "ON CONFLICT (user_id, book_id) DO UPDATE "
                "   SET rating = EXCLUDED.rating, review = EXCLUDED.review",
                {"uid": uid, "bid": book_id, "rating": rating, "review": review},
            )
    return RatingOut(rating=rating, review=review)


# -- "What's my next read?" assistant (sign-in required) ----------------------

def _validate_picks(
    conn, uid: int, picks: list[dict]
) -> tuple[list[AssistantRecommendation], list[str]]:
    """Verify the agent's picks against the catalog and apply exclusion.

    Every pick must resolve to a real `books` row (anti-hallucination); ISBNs that
    don't are returned separately as `unverified`. Books already on the reader's
    normal shelves are dropped; books on their Exclusion shelf are kept and tagged
    `excluded` (the UI hides or grays them, as on the other recommendation panels).
    """
    shelved = conn.execute(
        "SELECT sb.book_id, s.is_exclusion FROM shelf_books sb "
        "JOIN shelves s ON s.shelf_id = sb.shelf_id WHERE s.user_id = %s",
        (uid,),
    ).fetchall()
    normal_ids = {r["book_id"] for r in shelved if not r["is_exclusion"]}
    exclusion_ids = {r["book_id"] for r in shelved if r["is_exclusion"]}

    recs: list[AssistantRecommendation] = []
    unverified: list[str] = []
    seen: set[int] = set()
    for pick in picks:
        isbn = pick.get("isbn") or ""
        forms = [f for f in {isbn, to_isbn10(isbn), to_isbn13(isbn)} if f]
        row = (
            conn.execute(
                f"SELECT {_SUMMARY_COLS} FROM books b WHERE b.isbn = ANY(%s) LIMIT 1",
                (forms,),
            ).fetchone()
            if forms
            else None
        )
        if row is None:
            unverified.append(isbn)
            continue
        bid = row["book_id"]
        if bid in normal_ids or bid in seen:
            continue  # already on a normal shelf, or a duplicate pick
        seen.add(bid)
        authors = _normalize_authors(row["authors"])
        recs.append(
            AssistantRecommendation(
                book_id=bid,
                isbn=row["isbn"],
                title=row["title"],
                author=authors[0] if authors else None,
                cover_url=row["cover_url"],
                why=pick.get("why") or "",
                excluded=bid in exclusion_ids,
            )
        )
    return recs, unverified


@app.post("/api/assistant", response_model=AssistantReply)
def assistant_turn(body: AssistantRequest, user: dict = Depends(require_user)) -> AssistantReply:
    """One turn of the conversational "next read" assistant.

    Continues `session_id` (must be the caller's) or starts a new conversation.
    Runs the tool-using agent, verifies its picks against the catalog, applies
    exclusion, persists the turn, and returns the reply + recommendations. The
    agent call can take tens of seconds; no DB connection is held while it runs.
    """
    uid = user["user_id"]
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Tell me what you're in the mood to read.")

    # Daily request quota (per-user + global). Counted before the expensive
    # agent run; a rejected request is not counted. 429 when a limit is hit.
    with pool.connection() as conn:
        try:
            enforce_quota(conn, uid)
        except QuotaExceeded as e:
            raise HTTPException(status_code=429, detail=str(e))

    session_id = body.session_id
    if session_id is not None:
        if agent_store.session_owner(session_id) != uid:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        session_id = agent_store.create_session(uid)

    history = agent_store.load_recent_messages(session_id, _assistant_config.history_k)

    try:
        result = run_assistant(message, history, ToolContext(user_id=uid), _assistant_config)
    except AssistantError as e:
        raise HTTPException(status_code=503, detail=str(e))

    with pool.connection() as conn:
        recs, unverified = _validate_picks(conn, uid, result.picks)

    notes: list[str] = []
    if unverified:
        notes.append(f"Couldn't verify {len(unverified)} suggestion(s) against the catalog.")
    if result.truncated:
        notes.append("The reply was cut short.")

    agent_store.append_turn(
        session_id,
        message,
        {
            "reply": result.reply,
            "recommendations": [r.model_dump() for r in recs],
            "unverified": unverified,
            "tool_trace": result.tool_trace,
            "notes": notes,
        },
    )
    return AssistantReply(
        session_id=session_id,
        reply=result.reply,
        recommendations=recs,
        unverified=unverified,
        tool_trace=result.tool_trace,
        notes=notes,
    )


# -- React SPA (static files + deep-link fallback) ----------------------------
# Defined AFTER the API routes so /api/* always wins. Serves a real file when it
# exists (js/css/index), else falls back to index.html for client-side routes.

@app.get("/{full_path:path}")
def spa(full_path: str) -> FileResponse:
    if full_path.startswith("api"):
        raise HTTPException(status_code=404, detail="Not found")
    candidate = (STATIC_DIR / full_path).resolve()
    if full_path and STATIC_DIR in candidate.parents and candidate.is_file():
        return FileResponse(candidate)
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Frontend not built")
    return FileResponse(index)
