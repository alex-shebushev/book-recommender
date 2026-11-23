"""Persistence for the "What's my next read?" assistant (DESIGN §5A, §6).

One conversation = one `agent_sessions` row; one turn (the reader's input plus
the agent's structured output as JSON) = one `agent_turns` row. The assistant
replays the last `AGENT_HISTORY_K` *messages* — a turn is two, the input and the
reply — into the model's context; every turn is persisted regardless.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from db import pool


def create_session(user_id: int, title: str | None = None) -> int:
    """Start a new conversation thread for a user; return its session_id."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_sessions (user_id, title) VALUES (%s, %s) "
            "RETURNING session_id",
            (user_id, title),
        ).fetchone()
    return row["session_id"]


def session_owner(session_id: int) -> int | None:
    """The user_id that owns a session, or None if the session doesn't exist.

    The caller compares it to the signed-in user before continuing a session, so
    one reader can't append to or read another reader's conversation.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM agent_sessions WHERE session_id = %s", (session_id,)
        ).fetchone()
    return row["user_id"] if row else None


def load_recent_messages(session_id: int, k_messages: int) -> list[dict]:
    """The session's last `k_messages` messages as {role, content} pairs, oldest
    first — ready to prepend to the model's `messages` before the new input.

    A turn is two messages (user input, then reply), so we read the last
    ceil(k/2) turns, flatten them, trim to the last k messages, and drop any
    leading assistant message so the replayed history starts on a user turn.
    """
    if k_messages <= 0:
        return []
    turns_needed = (k_messages + 1) // 2
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT user_input, output FROM agent_turns WHERE session_id = %s "
            "ORDER BY created_at DESC, turn_id DESC LIMIT %s",
            (session_id, turns_needed),
        ).fetchall()
    rows.reverse()  # chronological order
    messages: list[dict] = []
    for r in rows:
        messages.append({"role": "user", "content": r["user_input"]})
        reply = (r["output"] or {}).get("reply") or ""
        if reply:
            messages.append({"role": "assistant", "content": reply})
    trimmed = messages[-k_messages:]
    while trimmed and trimmed[0]["role"] == "assistant":
        trimmed.pop(0)
    return trimmed


def append_turn(session_id: int, user_input: str, output: dict[str, Any]) -> int:
    """Store one completed turn and touch the session.

    The first input becomes the session's title (bumping updated_at via the
    trigger). Called only after the agent has produced `output`, so a failed turn
    is simply not persisted.
    """
    title = " ".join(user_input.split())[:80] or None
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_turns (session_id, user_input, output) "
            "VALUES (%s, %s, %s) RETURNING turn_id",
            (session_id, user_input, Jsonb(output)),
        ).fetchone()
        conn.execute(
            "UPDATE agent_sessions SET title = COALESCE(title, %s) WHERE session_id = %s",
            (title, session_id),
        )
    return row["turn_id"]
