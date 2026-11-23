"""Configuration for the Book Recommender agent (stage 1).

Values come from environment variables (loaded from a local `.env` if present),
with sensible defaults. See `.env.example`.
"""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

from dotenv import dotenv_values

# Load the app's .env (if present) into the process environment. No-op in
# production where real env vars are set by the platform (they take precedence).
#  * Explicit path: a bare load_dotenv() guesses the folder from the caller or
#    the cwd, which breaks when the server is started from another folder.
#  * An EMPTY env var counts as unset: python-dotenv never overrides a variable
#    that exists, so a blank placeholder (some launchers set ANTHROPIC_API_KEY="")
#    would otherwise hide the key in .env and every LLM call would fail.
ENV_FILE = Path(__file__).resolve().parent / ".env"
for _key, _value in dotenv_values(ENV_FILE).items():
    if _value is not None and not os.environ.get(_key):
        os.environ[_key] = _value


# The model the user chose for testing. Named explicitly (not a date-suffixed
# variant). See Book Recommender/DESIGN.md §9.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Domain framing. There are no tools yet, so the agent can only talk about books
# from its own knowledge — grounding (RAG) and catalog tools come later.
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful book recommendation assistant.\n"
    "Help the reader find their next book based on their taste, mood, and "
    "constraints (genre, length, content to avoid).\n"
    "Be concise. When you are unsure whether a book exists or recall details "
    "imperfectly, say so plainly rather than inventing titles, authors, or ISBNs.\n"
    "Note: you do not yet have access to a live catalog or knowledge base, so "
    "flag when a claim would need verification."
)


# How many prior session messages the "next read" assistant loads into context.
# A turn is two messages (the reader's input and the reply), so k=5 is the last
# ~2–3 turns. Decided in OPEN_QUESTIONS Q19.
DEFAULT_HISTORY_K = 5

# System prompt for the tool-using "What's my next read?" assistant (DESIGN §5A).
# Encodes: conversation-only context, verify-against-catalog (never invent),
# exclude the reader's shelves, abstain when weak, read-only, finish via
# present_recommendations. The box's "why" is the agent's own reasoning, not a
# cited review (Q17 — grounding tool deliberately omitted from this set).
ASSISTANT_SYSTEM_PROMPT = (
    "You are the \"What's my next read?\" assistant for a book-recommendation "
    "app. A signed-in reader tells you what they feel like reading; you find real "
    "books for them from the app's catalog and knowledge base, using tools.\n"
    "\n"
    "## Your job\n"
    "1. Read the reader's message and the recent conversation, and work out their "
    "intent: genres, mood/tone, length, era, language, things to avoid, and any "
    "books or authors they mention.\n"
    "2. Use your tools to find candidate books. Decide which tools you actually "
    "need — don't call tools you don't.\n"
    "3. Finish by calling `present_recommendations` with your final picks and a "
    "one-line reason for each. That call is how your recommendations reach the "
    "reader.\n"
    "\n"
    "## Tools\n"
    "- `get_user_library` — the reader's shelves and ratings. Call this early when "
    "taste or \"what I've already read\" matters, so you don't recommend books "
    "they already have.\n"
    "- `search_catalog` — find books by genre / title / author / constraints. Use "
    "it for constraint-only requests and to turn a title the reader names into a "
    "real catalog book.\n"
    "- `get_book_details` — full details for one book by ISBN.\n"
    "- `find_similar` — books similar to a description or a seed book. Resolve the "
    "seed first (search_catalog / get_book_details), then pass its description.\n"
    "- `find_similar_by_bookshelf` — books similar to a SET of books (the reader's "
    "shelf, or several titles they liked); pass their ISBNs.\n"
    "- `present_recommendations` — call once, at the end, with [{isbn, why}].\n"
    "\n"
    "## Rules\n"
    "- Only recommend real books you found through tools. Never invent a title, "
    "author, or ISBN. If you are not sure a book exists, do not present it. Every "
    "book you present is verified against the catalog; unverified ones are dropped.\n"
    "- Exclude what they have. Don't recommend books already on the reader's "
    "shelves, and never recommend anything on their Exclusion shelf.\n"
    "- Say when you're not sure. If the tools don't turn up a good match, say so "
    "plainly and recommend fewer — or none — rather than padding with weak picks.\n"
    "- Ask when the request is too vague to act on (no genre, mood, or example): "
    "ask one short clarifying question instead of guessing.\n"
    "- Stay read-only. You cannot change shelves or ratings; don't claim you did.\n"
    "- Keep your spoken reply short and friendly — the detailed cards come from "
    "`present_recommendations`. Your \"why\" is your own honest reasoning about "
    "fit, not a quote from a review."
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class KBConfig:
    """Settings for the Bedrock Knowledge Base (find_similar, RAG tools)."""

    knowledge_base_id: str | None = None
    # None -> boto3 resolves the region from the AWS CLI profile (~/.aws).
    region: str | None = None
    top_k: int = 5
    min_score: float = 0.4

    @classmethod
    def from_env(cls) -> "KBConfig":
        return cls(
            knowledge_base_id=os.getenv("BEDROCK_KB_ID") or None,
            region=os.getenv("AWS_REGION") or None,
            top_k=int(os.getenv("FIND_SIMILAR_TOP_K", "5")),
            min_score=float(os.getenv("FIND_SIMILAR_MIN_SCORE", "0.4")),
        )


@dataclass(frozen=True)
class AgentConfig:
    """Runtime settings for the agent loop."""

    model: str = DEFAULT_MODEL
    max_tokens: int = 4096
    # Adaptive thinking (Sonnet 4.6): off by default for a fast, bare Q&A loop.
    use_thinking: bool = False
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return cls(
            model=os.getenv("AGENT_MODEL", DEFAULT_MODEL),
            max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "4096")),
            use_thinking=_env_bool("AGENT_THINKING", False),
            system_prompt=os.getenv("AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        )


@dataclass(frozen=True)
class AssistantConfig:
    """Runtime settings for the tool-using "next read" assistant (DESIGN §5A)."""

    model: str = DEFAULT_MODEL
    max_tokens: int = 4096
    system_prompt: str = ASSISTANT_SYSTEM_PROMPT
    # Prior session messages replayed into context (a turn is two messages).
    history_k: int = DEFAULT_HISTORY_K
    # Safety cap on tool-use rounds per turn, so a loop can't run away.
    max_tool_rounds: int = 8

    @classmethod
    def from_env(cls) -> "AssistantConfig":
        return cls(
            model=os.getenv("AGENT_MODEL", DEFAULT_MODEL),
            max_tokens=int(os.getenv("ASSISTANT_MAX_TOKENS", "4096")),
            system_prompt=os.getenv("ASSISTANT_SYSTEM_PROMPT", ASSISTANT_SYSTEM_PROMPT),
            history_k=int(os.getenv("AGENT_HISTORY_K", str(DEFAULT_HISTORY_K))),
            max_tool_rounds=int(os.getenv("ASSISTANT_MAX_TOOL_ROUNDS", "8")),
        )
