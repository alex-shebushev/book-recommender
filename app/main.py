"""Entry point for the stage-1 agent loop.

Two modes:
  * default      — run a few hardcoded test questions through the agent
  * --interactive / -i — a REPL you can chat with (Ctrl-C or 'exit' to quit)

Run:
    python main.py
    python main.py --interactive
"""
from __future__ import annotations

import sys

import anthropic

from agent import Agent, AgentError
from config import AgentConfig

# Hardcoded prompts for a quick smoke test of the loop.
TEST_QUESTIONS = [
    "Recommend a short science-fiction novel with a hopeful ending.",
    "I liked that. Now something similar but non-fiction.",
    "What did I ask you for first?",  # checks multi-turn memory
]


def _truncation_note(agent: Agent) -> str:
    """A visible marker when the last reply was cut off at the token limit."""
    if agent.last_stop_reason == "max_tokens":
        return "\n    [reply cut off at max_tokens - raise AGENT_MAX_TOKENS]"
    return ""


def run_test_questions(agent: Agent) -> None:
    print(f"Model: {agent.config.model} | thinking: {agent.config.use_thinking}\n")
    for i, question in enumerate(TEST_QUESTIONS, 1):
        print(f"[{i}] You: {question}")
        try:
            reply = agent.send(question)
        except AgentError as e:
            # A declined turn is reported and skipped; the run carries on.
            print(f"    [agent] {e}\n")
            continue
        print(f"    Assistant: {reply}{_truncation_note(agent)}\n")


def run_interactive(agent: Agent) -> None:
    print(f"Model: {agent.config.model} | thinking: {agent.config.use_thinking}")
    print("Chat with the book assistant. Type 'exit' or Ctrl-C to quit.\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return
        if question.lower() in {"exit", "quit"}:
            print("Bye.")
            return
        if not question:
            continue
        try:
            reply = agent.send(question)
        except AgentError as e:
            print(f"[agent] {e}\n")
            continue
        print(f"Assistant: {reply}{_truncation_note(agent)}\n")


def main(argv: list[str]) -> int:
    config = AgentConfig.from_env()

    try:
        agent = Agent(config)
    except anthropic.AnthropicError as e:
        # Most commonly a missing API key.
        print(f"Failed to initialize the Anthropic client: {e}", file=sys.stderr)
        print("Set ANTHROPIC_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    interactive = any(flag in argv for flag in ("-i", "--interactive"))

    try:
        if interactive:
            run_interactive(agent)
        else:
            run_test_questions(agent)
    except anthropic.AuthenticationError:
        print("Authentication failed — check ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2
    except anthropic.APIError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
