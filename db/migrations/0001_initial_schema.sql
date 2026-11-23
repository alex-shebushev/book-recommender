-- 0001_initial_schema.sql
-- Book Recommender — initial schema (stage 1). See ../../DESIGN.md §6.
--
-- Integer identity PKs/FKs; `isbn` is a unique natural key, not a key column.
-- The runner (migrate.py) wraps this file in a single transaction and records it
-- in schema_migrations, so do NOT add BEGIN/COMMIT here.

-- Status of a book in a user's library.
CREATE TYPE library_status AS ENUM ('read', 'owned', 'want_to_read', 'disliked');

-- Reusable trigger function to keep updated_at current on UPDATE.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Catalog + Open Library cache. Canonical book set, keyed internally by book_id.
CREATE TABLE books (
    book_id            integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isbn               text        NOT NULL UNIQUE,   -- ISBN-10: KB / Open Library key
    isbn13             text,                          -- unreliable; informational
    title              text        NOT NULL,
    author             text,
    description        text,                          -- blurb; embedding/resolve source
    pages              integer,
    published_year     integer,
    language           text,
    genres             text[],
    content_warnings   text[],                        -- source TBD (Q1)
    cover_url          text,
    source             text,                          -- provenance, e.g. 'openlibrary'
    details_fetched_at timestamptz,                   -- get_book_details cache freshness
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX books_genres_gin ON books USING gin (genres);
CREATE TRIGGER books_set_updated_at
    BEFORE UPDATE ON books
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Application users (UI-facing; no secrets here).
CREATE TABLE users (
    user_id      integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email        text        NOT NULL UNIQUE,
    display_name text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Authentication sidecar, 1:1 with users (secrets kept out of the users row).
CREATE TABLE user_details (
    user_id         integer     PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    password_hash   text        NOT NULL,
    salt            text,                             -- optional (bcrypt/argon2 embed it)
    blocked_until   timestamptz,                      -- NULL = not blocked
    failed_attempts smallint    NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER user_details_set_updated_at
    BEFORE UPDATE ON user_details
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Unified read-list / wishlist / owned / disliked. Serves get_user_library and n8n.
CREATE TABLE user_library (
    user_id    integer        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    book_id    integer        NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    status     library_status NOT NULL,
    rating     smallint       CHECK (rating BETWEEN 1 AND 5),  -- set when read
    review     text,                                           -- USER review (<> KB editorial)
    added_at   timestamptz    NOT NULL DEFAULT now(),
    updated_at timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, book_id)
);
-- Supports the n8n "users who rated similar books highly" query.
CREATE INDEX user_library_candidate_idx ON user_library (book_id, status, rating);
CREATE TRIGGER user_library_set_updated_at
    BEFORE UPDATE ON user_library
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
