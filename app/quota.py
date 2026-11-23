"""Daily request quotas for the agent endpoint.

Two caps, both per UTC day:
  * PER_USER_DAILY_LIMIT  — requests one user may make.
  * GLOBAL_DAILY_LIMIT    — requests all users may make in total.

State lives in `request_quota(user_id, date, counter)`, one row per user, where
`date` is the UTC date the counter applies to. On each agent request:
  * no row yet, or the stored date is not today  -> roll over: date = today,
    counter = 1 (a new day overwrites the old counter);
  * stored date is today                         -> counter += 1, unless the
    per-user limit is already reached.
The global cap is the sum of every row whose date is today. A request that is
rejected is NOT counted.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Not more than 1,000 requests per user per UTC day.
PER_USER_DAILY_LIMIT = 1000
# Not more than 1,000,000 requests across all users per UTC day.
GLOBAL_DAILY_LIMIT = 1_000_000


class QuotaExceeded(Exception):
    """A request would exceed a daily quota. `scope` is "user" or "global"."""

    def __init__(self, scope: str, message: str) -> None:
        self.scope = scope
        super().__init__(message)


def _utc_today():
    """Today's date in UTC, taken from the system clock."""
    return datetime.now(timezone.utc).date()


def enforce_quota(conn, user_id: int) -> None:
    """Count one agent request against the user's and the global daily quota.

    Runs inside the caller's transaction (a `with pool.connection()` block that
    commits on exit) and locks the user's row with ``FOR UPDATE`` so concurrent
    requests from the same user count correctly. Raises :class:`QuotaExceeded`
    — without writing anything — when a limit is already reached.
    """
    today = _utc_today()

    row = conn.execute(
        "SELECT date, counter FROM request_quota WHERE user_id = %s FOR UPDATE",
        (user_id,),
    ).fetchone()

    # Per-user: a new UTC day (or the first ever request) resets the counter to
    # 1; the same day increments, unless the limit is already reached.
    if row is None or row["date"] != today:
        new_counter = 1
    elif row["counter"] >= PER_USER_DAILY_LIMIT:
        raise QuotaExceeded(
            "user",
            f"Daily request limit reached ({PER_USER_DAILY_LIMIT} per day). "
            "Try again tomorrow (UTC).",
        )
    else:
        new_counter = row["counter"] + 1

    # Global: today's total so far (rows still dated earlier are excluded). This
    # request is allowed only while the total is still below the cap.
    global_today = conn.execute(
        "SELECT COALESCE(SUM(counter), 0) AS n FROM request_quota WHERE date = %s",
        (today,),
    ).fetchone()["n"]
    if global_today >= GLOBAL_DAILY_LIMIT:
        raise QuotaExceeded(
            "global",
            "The service has reached its daily request capacity. "
            "Please try again tomorrow (UTC).",
        )

    if row is None:
        conn.execute(
            "INSERT INTO request_quota (user_id, date, counter) VALUES (%s, %s, %s)",
            (user_id, today, new_counter),
        )
    else:
        conn.execute(
            "UPDATE request_quota SET date = %s, counter = %s WHERE user_id = %s",
            (today, new_counter, user_id),
        )
