"""Authentication helpers: password hashing + the current-user dependency.

Sessions are a signed, HTTP-only cookie (Starlette SessionMiddleware) holding
only the user_id; the user row is loaded fresh each request. No roles yet — every
authenticated user has the same access (see DESIGN.md §6).
"""
from __future__ import annotations

import bcrypt
from fastapi import HTTPException, Request

from db import pool

# bcrypt rejects passwords longer than 72 bytes; truncate defensively so a long
# input can't raise. Real length validation happens at the API boundary.
_MAX_PW_BYTES = 72


def hash_password(plain: str) -> str:
    pw = plain.encode("utf-8")[:_MAX_PW_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_MAX_PW_BYTES], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _load_user(user_id: int) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT user_id, email, display_name FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()


def current_user(request: Request) -> dict | None:
    """The logged-in user (from the session cookie), or None. Never raises.

    Use for endpoints that are public but personalize when signed in (e.g. a
    book's shelf status).
    """
    uid = request.session.get("user_id")
    if not uid:
        return None
    return _load_user(uid)


def require_user(request: Request) -> dict:
    """Like `current_user`, but returns 401 when not signed in. Gates routes."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user
