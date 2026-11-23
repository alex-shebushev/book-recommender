#!/usr/bin/env python3
"""Seed the Book Recommender catalog, demo user, its ratings and its shelves.

Idempotent: re-running inserts only what's missing (ON CONFLICT DO NOTHING), so
it is safe to run repeatedly. Data comes from ``seed_data.json`` next to this
file. Covers are derived from the Open Library cover API by ISBN (no API call at
seed time — the URL is built, the browser fetches it).

Connection is resolved exactly like migrate.py (DATABASE_URL, else POSTGRES_*).

Usage:
  pip install -r db/requirements.txt   # same deps as the migrator
  python db/migrate.py                  # make sure the schema is applied first
  python db/seed.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg

DB_DIR = Path(__file__).resolve().parent
SEED_FILE = DB_DIR / "seed_data.json"
# The full catalog built from the Knowledge Base (db/build_catalog_from_kb.py).
# Optional: when absent, only the curated seed books are loaded.
KB_CATALOG_FILE = DB_DIR / "kb_catalog.json"


def _load_kb_catalog() -> list[dict]:
    if not KB_CATALOG_FILE.is_file():
        return []
    return json.loads(KB_CATALOG_FILE.read_text(encoding="utf-8")).get("books", [])


# Columns + conflict clauses shared by the curated and KB book inserts.
_BOOK_INSERT = (
    "INSERT INTO books (isbn, title, authors, description, pages, published_year, "
    "language, genres, cover_url, source) VALUES "
    "(%(isbn)s, %(title)s, %(authors)s, %(description)s, %(pages)s, %(published_year)s, "
    "%(language)s, %(genres)s, %(cover_url)s, %(source)s) "
)
_BOOK_OVERWRITE = (
    "ON CONFLICT (isbn) DO UPDATE SET title = EXCLUDED.title, authors = EXCLUDED.authors, "
    "description = EXCLUDED.description, pages = EXCLUDED.pages, "
    "published_year = EXCLUDED.published_year, language = EXCLUDED.language, "
    "genres = EXCLUDED.genres, cover_url = EXCLUDED.cover_url, source = EXCLUDED.source"
)


def _book_params(b: dict, default_source: str) -> dict:
    return {
        "isbn": b["isbn"],
        "title": b["title"],
        "authors": b.get("authors") or None,
        "description": b.get("description"),
        "pages": b.get("pages"),
        "published_year": b.get("published_year"),
        "language": b.get("language"),
        "genres": b.get("genres") or None,
        "cover_url": b.get("cover_url") or _cover_url(b["isbn"]),
        "source": b.get("source") or default_source,
    }


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(DB_DIR.parent / ".env")


def dsn() -> str:
    import os

    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "bookrec")
    pwd = os.getenv("POSTGRES_PASSWORD", "")
    name = os.getenv("POSTGRES_DB", "bookrec")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


def _cover_url(isbn: str) -> str:
    # Open Library serves covers by ISBN directly; no API call needed here.
    return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"


def seed(conn: psycopg.Connection, data: dict) -> None:
    books = data.get("books", [])
    admin = data.get("admin_user", {})
    ratings = data.get("ratings", [])
    shelves = data.get("shelves", [])

    kb_books = _load_kb_catalog()
    with conn.cursor() as cur:
        # Curated books first, keeping their hand-written descriptions...
        for b in books:
            cur.execute(_BOOK_INSERT + "ON CONFLICT (isbn) DO NOTHING",
                        _book_params(b, "openlibrary"))
        # ...then the KB catalog, which is authoritative and overwrites on conflict.
        for b in kb_books:
            cur.execute(_BOOK_INSERT + _BOOK_OVERWRITE, _book_params(b, "bedrock-kb"))
        catalog_size = cur.execute("SELECT count(*) FROM books").fetchone()[0]

        # Base admin account. On a fresh database this is the first user, so it
        # takes user_id = 1 (the identity sequence starts at 1). The password is
        # stored pre-hashed (bcrypt) in the seed file — a known demo credential.
        admin_email = admin.get("email", "admin@bookshelf.com")
        cur.execute(
            """
            INSERT INTO users (email, display_name)
            VALUES (%(email)s, %(display_name)s)
            ON CONFLICT (email) DO NOTHING
            """,
            {"email": admin_email, "display_name": admin.get("display_name", "Admin")},
        )
        row = cur.execute(
            "SELECT user_id FROM users WHERE email = %s", (admin_email,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Admin user missing after insert — cannot seed shelf.")
        user_id = row[0]

        if admin.get("password_hash"):
            cur.execute(
                """
                INSERT INTO user_details (user_id, password_hash)
                VALUES (%(user_id)s, %(password_hash)s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                {"user_id": user_id, "password_hash": admin["password_hash"]},
            )

        def book_id(isbn: str) -> int | None:
            row = cur.execute("SELECT book_id FROM books WHERE isbn = %s", (isbn,)).fetchone()
            return row[0] if row else None

        # Per-book rating/review (independent of shelves).
        inserted_ratings = 0
        skipped = []
        for entry in ratings:
            bid = book_id(entry["isbn"])
            if bid is None:
                skipped.append(entry["isbn"])
                continue
            cur.execute(
                """
                INSERT INTO user_library (user_id, book_id, rating, review)
                VALUES (%(user_id)s, %(book_id)s, %(rating)s, %(review)s)
                ON CONFLICT (user_id, book_id) DO NOTHING
                """,
                {"user_id": user_id, "book_id": bid,
                 "rating": entry.get("rating"), "review": entry.get("review")},
            )
            inserted_ratings += cur.rowcount

        # Shelves. "Exclusion" is the user's built-in exclusion shelf (created by
        # a trigger with the user); any other name is a normal shelf. A book can't
        # sit on both kinds — the shelf_books trigger enforces that.
        inserted_shelves = 0
        inserted_members = 0
        for s in shelves:
            if s["name"].strip().lower() == "exclusion":
                shelf_row = cur.execute(
                    "SELECT shelf_id FROM shelves WHERE user_id = %s AND is_exclusion", (user_id,)
                ).fetchone()
            else:
                cur.execute(
                    "INSERT INTO shelves (user_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (user_id, s["name"]),
                )
                inserted_shelves += cur.rowcount
                shelf_row = cur.execute(
                    "SELECT shelf_id FROM shelves WHERE user_id = %s AND lower(name) = lower(%s)",
                    (user_id, s["name"]),
                ).fetchone()
            if shelf_row is None:
                continue
            for isbn in s.get("books", []):
                bid = book_id(isbn)
                if bid is None:
                    skipped.append(isbn)
                    continue
                cur.execute(
                    "INSERT INTO shelf_books (shelf_id, book_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (shelf_row[0], bid),
                )
                inserted_members += cur.rowcount

    conn.commit()
    print(f"Books:  {catalog_size} in catalog ({len(books)} curated + {len(kb_books)} from the KB)")
    print(f"User:   admin user_id = {user_id} ({admin.get('email')})")
    print(f"Ratings: +{inserted_ratings} inserted ({len(ratings)} in seed file)")
    print(f"Shelves: +{inserted_shelves} shelves, +{inserted_members} memberships")
    if skipped:
        print(f"Skipped (book not found): {', '.join(sorted(set(skipped)))}")


def main() -> int:
    _load_env()
    if not SEED_FILE.is_file():
        print(f"Seed file not found: {SEED_FILE}", file=sys.stderr)
        return 1
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    try:
        conn = psycopg.connect(dsn())
    except psycopg.OperationalError as exc:
        print(f"Cannot connect to the database: {exc}", file=sys.stderr)
        print("Is the container up?  docker compose up -d", file=sys.stderr)
        return 1

    with conn:
        seed(conn, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
