-- 0003_owned_shelves.sql
-- Named shelves within the Owned category (see DESIGN.md §6).
-- Shelves exist only for Owned; a book may sit on several shelves (many-to-many).
-- The 4 statuses in user_library are unchanged — a book on any shelf is kept at
-- status = 'owned'; un-filing (removing from all shelves) leaves it owned.
-- Forward-only; the runner wraps this file in one transaction (no BEGIN/COMMIT).

-- A user's named Owned shelves.
CREATE TABLE shelves (
    shelf_id   integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    integer     NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- No two shelves with the same name per user (case-insensitive).
CREATE UNIQUE INDEX shelves_user_name_uq ON shelves (user_id, lower(name));
CREATE TRIGGER shelves_set_updated_at
    BEFORE UPDATE ON shelves
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Book <-> shelf membership (many-to-many).
CREATE TABLE shelf_books (
    shelf_id integer     NOT NULL REFERENCES shelves(shelf_id) ON DELETE CASCADE,
    book_id  integer     NOT NULL REFERENCES books(book_id)   ON DELETE CASCADE,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shelf_id, book_id)
);
CREATE INDEX shelf_books_book_idx ON shelf_books (book_id);
