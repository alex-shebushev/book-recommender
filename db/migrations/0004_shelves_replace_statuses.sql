-- 0004_shelves_replace_statuses.sql
-- Shelves replace the four library statuses (see DESIGN.md §6).
--
--   * A book's place in a library is now only its shelves (shelf_books).
--   * Every user has exactly one built-in "Exclusion" shelf (shelves.is_exclusion):
--     its books are never used as taste and never recommended. It is created by a
--     trigger for every new user, and the API refuses to rename or delete it.
--   * A book is on Exclusion OR on normal shelves, never both: a trigger removes
--     it from the other kind when it is added to one.
--   * user_library keeps only the per-book rating and review (the n8n pipeline
--     reads ratings); the status column and its enum are dropped.
--
-- Data: disliked -> Exclusion; read -> "Read"; want_to_read -> "Want to read";
-- owned books that sit on no shelf -> "Owned". Existing named shelves are kept,
-- and a same-named shelf is reused rather than duplicated.
-- Forward-only; the runner wraps this file in one transaction (no BEGIN/COMMIT).

ALTER TABLE shelves ADD COLUMN is_exclusion boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX shelves_one_exclusion_per_user ON shelves (user_id) WHERE is_exclusion;

-- 1) One Exclusion shelf per user (adopt a shelf already named "Exclusion").
UPDATE shelves SET is_exclusion = true WHERE lower(name) = 'exclusion';
INSERT INTO shelves (user_id, name, is_exclusion)
SELECT u.user_id, 'Exclusion', true
FROM users u
WHERE NOT EXISTS (SELECT 1 FROM shelves s WHERE s.user_id = u.user_id AND s.is_exclusion);

-- 2) Shelves for the former statuses, only where a user has such books.
INSERT INTO shelves (user_id, name)
SELECT DISTINCT ul.user_id, m.shelf_name
FROM user_library ul
JOIN (VALUES ('read'::library_status, 'Read'),
             ('want_to_read'::library_status, 'Want to read')) AS m (status, shelf_name)
  ON ul.status = m.status
WHERE NOT EXISTS (SELECT 1 FROM shelves s
                  WHERE s.user_id = ul.user_id AND lower(s.name) = lower(m.shelf_name));

INSERT INTO shelves (user_id, name)
SELECT DISTINCT ul.user_id, 'Owned'
FROM user_library ul
WHERE ul.status = 'owned'
  AND NOT EXISTS (SELECT 1 FROM shelf_books sb JOIN shelves s ON s.shelf_id = sb.shelf_id
                  WHERE s.user_id = ul.user_id AND sb.book_id = ul.book_id)
  AND NOT EXISTS (SELECT 1 FROM shelves s WHERE s.user_id = ul.user_id AND lower(s.name) = 'owned');

-- 3) Memberships. Unfiled owned books first, while "on no shelf" still means unfiled.
INSERT INTO shelf_books (shelf_id, book_id)
SELECT s.shelf_id, ul.book_id
FROM user_library ul
JOIN shelves s ON s.user_id = ul.user_id AND lower(s.name) = 'owned' AND NOT s.is_exclusion
WHERE ul.status = 'owned'
  AND NOT EXISTS (SELECT 1 FROM shelf_books sb JOIN shelves s2 ON s2.shelf_id = sb.shelf_id
                  WHERE s2.user_id = ul.user_id AND sb.book_id = ul.book_id)
ON CONFLICT DO NOTHING;

INSERT INTO shelf_books (shelf_id, book_id)
SELECT s.shelf_id, ul.book_id
FROM user_library ul
JOIN shelves s ON s.user_id = ul.user_id
 AND (   (ul.status = 'read'         AND lower(s.name) = 'read'         AND NOT s.is_exclusion)
      OR (ul.status = 'want_to_read' AND lower(s.name) = 'want to read' AND NOT s.is_exclusion)
      OR (ul.status = 'disliked'     AND s.is_exclusion))
ON CONFLICT DO NOTHING;

-- 4) Exclusion wins: an excluded book leaves every normal shelf of that user.
DELETE FROM shelf_books sb
USING shelves s
WHERE sb.shelf_id = s.shelf_id
  AND NOT s.is_exclusion
  AND EXISTS (SELECT 1 FROM shelf_books e JOIN shelves x ON x.shelf_id = e.shelf_id
              WHERE x.user_id = s.user_id AND x.is_exclusion AND e.book_id = sb.book_id);

-- 5) user_library keeps only ratings/reviews.
DROP INDEX user_library_candidate_idx;
DELETE FROM user_library WHERE rating IS NULL AND review IS NULL;
ALTER TABLE user_library DROP COLUMN status;
DROP TYPE library_status;
CREATE INDEX user_library_rating_idx ON user_library (book_id, rating);

-- 6) Rules that every write path gets for free.

-- Every new user starts with an Exclusion shelf.
CREATE FUNCTION create_exclusion_shelf() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO shelves (user_id, name, is_exclusion) VALUES (NEW.user_id, 'Exclusion', true);
    RETURN NEW;
END;
$$;
CREATE TRIGGER users_create_exclusion_shelf
    AFTER INSERT ON users
    FOR EACH ROW EXECUTE FUNCTION create_exclusion_shelf();

-- Adding a book to Exclusion takes it off the user's normal shelves, and adding
-- it to a normal shelf takes it off Exclusion.
CREATE FUNCTION shelf_books_exclusion_rule() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target shelves%ROWTYPE;
BEGIN
    SELECT * INTO target FROM shelves WHERE shelf_id = NEW.shelf_id;
    DELETE FROM shelf_books sb
    USING shelves s
    WHERE sb.shelf_id = s.shelf_id
      AND s.user_id = target.user_id
      AND sb.book_id = NEW.book_id
      AND s.is_exclusion <> target.is_exclusion;
    RETURN NEW;
END;
$$;
CREATE TRIGGER shelf_books_exclusion
    AFTER INSERT ON shelf_books
    FOR EACH ROW EXECUTE FUNCTION shelf_books_exclusion_rule();
