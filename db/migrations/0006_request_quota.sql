-- Per-user (and global) daily request quota for the agent endpoint.
--
-- One row per user. `date` is the UTC date the counter applies to; on the first
-- request of a new UTC day the row is rolled over (date set to today, counter
-- reset). The global daily total is SUM(counter) WHERE date = <today>.
CREATE TABLE IF NOT EXISTS request_quota (
    user_id integer PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    date    date    NOT NULL,
    counter integer NOT NULL DEFAULT 0 CHECK (counter >= 0)
);

-- The global cap sums today's counters, so index by date.
CREATE INDEX IF NOT EXISTS idx_request_quota_date ON request_quota (date);
