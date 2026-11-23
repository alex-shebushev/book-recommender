-- 0005_agent_sessions.sql
-- Conversation history for the "What's my next read?" assistant (DESIGN.md §5A/§6).
--
--   * agent_sessions — one row per conversation thread. "New chat" starts another.
--   * agent_turns    — one row per TURN: the reader's input plus the agent's
--     structured output as JSON ({reply, recommendations, tool_trace, unverified,
--     notes}). The assistant loads the last AGENT_HISTORY_K *messages* (a turn is
--     two: the input and the reply) into context; all turns are persisted.
--
-- Integer identity PKs/FKs (isbn is never a key here). The runner (migrate.py)
-- wraps this file in one transaction; do NOT add BEGIN/COMMIT. set_updated_at()
-- comes from 0001.

CREATE TABLE agent_sessions (
    session_id integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    integer     NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title      text,                                   -- optional label (first message)
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_sessions_user_idx ON agent_sessions (user_id, updated_at DESC);
CREATE TRIGGER agent_sessions_set_updated_at
    BEFORE UPDATE ON agent_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per turn. `output` holds the whole structured reply as JSON so the
-- recommendations and tool-trace rehydrate for re-rendering a past turn; only
-- `user_input` + `output->>'reply'` are replayed into the model's context.
CREATE TABLE agent_turns (
    turn_id    integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id integer     NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    user_input text        NOT NULL,
    output     jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_turns_session_idx ON agent_turns (session_id, created_at);
