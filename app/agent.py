"""Minimal agent loop for the Book Recommender (stage 1).

This stage has **no tools**. It is a multi-turn conversation loop over the
Anthropic Messages API: maintain history, send a request, append the reply.
Tools (parse_bookshelf, find_similar, RAG, ...) are added in a later stage.

The Messages API is stateless — the full conversation history is sent on every
request. See Book Recommender/DESIGN.md for the overall architecture.
"""
from __future__ import annotations

import anthropic

from config import AgentConfig


class AgentError(RuntimeError):
    """Raised when a turn could not be completed."""


class Agent:
    """A stateful, multi-turn chat agent backed by a single Claude model.

    Usage:
        agent = Agent(AgentConfig.from_env())
        reply = agent.send("Recommend me a short sci-fi novel.")
    """

    def __init__(
        self,
        config: AgentConfig,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.config = config
        # A zero-arg client resolves credentials from the environment
        # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
        self.client = client or anthropic.Anthropic()
        self.messages: list[dict] = []
        # Why the last reply ended. "max_tokens" means it was cut off mid-answer
        # and the caller should say so rather than present it as complete.
        self.last_stop_reason: str | None = None

    # -- public API -------------------------------------------------------

    def send(self, user_message: str) -> str:
        """Send one user turn and return the assistant's text reply.

        Appends both the user message and the assistant reply to history so the
        next call carries the full context.
        """
        self.messages.append({"role": "user", "content": user_message})

        try:
            response = self.client.messages.create(**self._request_kwargs())
        except anthropic.APIError:
            # Keep history consistent: drop the user turn we optimistically added
            # so a retry doesn't stack duplicate user messages.
            self.messages.pop()
            raise

        self.last_stop_reason = response.stop_reason
        if response.stop_reason == "refusal":
            self.messages.pop()  # don't persist a half turn
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise AgentError(f"The model declined to respond (category: {category}).")

        reply = self._extract_text(response)
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def reset(self) -> None:
        """Clear conversation history (start a fresh conversation)."""
        self.messages.clear()

    # -- internals --------------------------------------------------------

    def _request_kwargs(self) -> dict:
        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": self.config.system_prompt,
            "messages": self.messages,
        }
        if self.config.use_thinking:
            # Adaptive thinking on Sonnet 4.6 (no beta header, no budget_tokens).
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    @staticmethod
    def _extract_text(response: anthropic.types.Message) -> str:
        """Join all text blocks from a response into a single string."""
        return "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
