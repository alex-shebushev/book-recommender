-- 0002_books_authors_array.sql
-- Change books.author (single text) -> books.authors (text[]).
-- Decision: co-authors matter; a book can have more than one author.
-- See DESIGN.md §6. Forward-only (0001 is already applied — do not edit it).
--
-- The runner (migrate.py) wraps this file in a single transaction; no BEGIN/COMMIT.

ALTER TABLE books ADD COLUMN authors text[];

-- Carry any existing single author into the array (no-op on an empty catalog).
UPDATE books
   SET authors = ARRAY[author]
 WHERE author IS NOT NULL;

ALTER TABLE books DROP COLUMN author;
