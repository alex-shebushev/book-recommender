"""The tool-using "What's my next read?" assistant (DESIGN §5A).

A bounded tool-use loop over the Anthropic Messages API: the model selects tools
(`agent_tools.TOOLS`), the server runs them and feeds the results back, and the
turn ends when the model stops requesting tools. The model signals its final
picks by calling `present_recommendations`; those picks are returned **unverified**
— api.py checks them against Postgres and applies exclusion before anything is
shown. Conversation history is supplied by the caller (see `agent_store`).

The Messages API is stateless, so the whole message list (replayed history + this
turn's tool exchanges) is sent on every call.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from config import AssistantConfig
from tools.agent_tools import PRESENT_TOOL, TOOLS, ToolContext, run_tool

logger = logging.getLogger(__name__)


class AssistantError(RuntimeError):
    """Raised when a turn could not be completed (API error or refusal)."""


@dataclass
class AssistantResult:
    """One assistant turn's output. `picks` are the agent's chosen ISBNs + the
    reason for each, still unverified; `tool_trace` lists the tools it called."""

    reply: str
    picks: list[dict] = field(default_factory=list)       # [{isbn, why}]
    tool_trace: list[dict] = field(default_factory=list)   # [{tool, input}]
    truncated: bool = False


def run_assistant(
    user_message: str,
    history: list[dict],
    ctx: ToolContext,
    config: AssistantConfig,
    client: anthropic.Anthropic | None = None,
) -> AssistantResult:
    """Run one turn: reason, call tools, and return the reply + picks + trace.

    Args:
        user_message: the reader's new message.
        history: prior {role, content} messages (from agent_store), oldest first.
        ctx: tool context (the signed-in user_id).
        config: model, token and loop settings.
        client: injectable Anthropic client (tests).
    """
    client = client or anthropic.Anthropic()
    messages: list[dict] = list(history) + [{"role": "user", "content": user_message}]
    tool_trace: list[dict] = []
    picks: list[dict] = []
    last_text = ""
    truncated = False

    for _ in range(config.max_tool_rounds):
        try:
            response = client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                system=config.system_prompt,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:
            raise AssistantError(f"The assistant is unavailable right now: {e}") from e

        if response.stop_reason == "refusal":
            raise AssistantError("The model declined to respond to that.")
        if response.stop_reason == "max_tokens":
            truncated = True

        text = _text(response)
        if text:
            last_text = text
        # Preserve the full assistant turn (text + tool_use blocks) in history.
        messages.append({"role": "assistant", "content": _assistant_content(response)})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            # No tools requested: this response is the final spoken reply.
            return AssistantResult(
                reply=last_text, picks=picks, tool_trace=tool_trace, truncated=truncated
            )

        tool_results = []
        for block in tool_uses:
            tool_trace.append({"tool": block.name, "input": block.input})
            result = run_tool(block.name, block.input or {}, ctx)
            if block.name == PRESENT_TOOL:
                picks = _clean_picks(block.input or {})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    # Loop budget exhausted (rare): return what we have rather than hang.
    logger.warning("assistant hit max_tool_rounds (%s)", config.max_tool_rounds)
    return AssistantResult(
        reply=last_text or "Here are a few books you might enjoy.",
        picks=picks,
        tool_trace=tool_trace,
        truncated=True,
    )


# -- helpers -----------------------------------------------------------------

def _assistant_content(response: anthropic.types.Message) -> list[dict]:
    """The response's content blocks as plain dicts, to replay as history."""
    blocks: list[dict] = []
    for b in response.content:
        if b.type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


def _text(response: anthropic.types.Message) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _clean_picks(tool_input: dict) -> list[dict]:
    out: list[dict] = []
    for item in tool_input.get("recommendations") or []:
        isbn = str((item or {}).get("isbn") or "").strip()
        why = str((item or {}).get("why") or "").strip()
        if isbn:
            out.append({"isbn": isbn, "why": why})
    return out
