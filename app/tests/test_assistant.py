"""Offline tests for the tool-using assistant loop — no Anthropic or AWS.

A fake Anthropic client returns scripted responses, so these check the loop
itself: the clarify path (no tools), capturing the final picks + tool-trace, the
replayed history, and the max-rounds cap. The data tools aren't exercised here
(they need Postgres/Bedrock). Run from the app folder:

    python -m unittest tests.test_assistant -v
"""
from __future__ import annotations

import unittest

from assistant import _clean_picks, run_assistant
from config import AssistantConfig
from isbn import to_isbn10
from tools.agent_tools import PRESENT_TOOL, ToolContext, _isbn_forms


# -- fake Anthropic client ----------------------------------------------------

class _Block:
    def __init__(self, type, text=None, id=None, name=None, input=None):
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = input


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, script, repeat_last=False):
        self._script = list(script)
        self._repeat_last = repeat_last
        self.calls = []

    def create(self, **kwargs):
        # Snapshot messages: run_assistant mutates the same list after this call.
        snap = dict(kwargs)
        snap["messages"] = list(kwargs.get("messages", []))
        self.calls.append(snap)
        i = len(self.calls) - 1
        if i < len(self._script):
            return self._script[i]
        if self._repeat_last:
            return self._script[-1]
        raise AssertionError("fake client called more times than scripted")


class _FakeClient:
    def __init__(self, script, repeat_last=False):
        self.messages = _FakeMessages(script, repeat_last)


def _text(t):
    return _Resp([_Block("text", text=t)], stop_reason="end_turn")


def _present(recs):
    return _Resp(
        [_Block("tool_use", id="p1", name=PRESENT_TOOL, input={"recommendations": recs})],
        stop_reason="tool_use",
    )


CFG = AssistantConfig()
CTX = ToolContext(user_id=1)


# -- pure helpers -------------------------------------------------------------

class CleanPicksTest(unittest.TestCase):
    def test_keeps_isbn_and_why_drops_empty(self):
        out = _clean_picks(
            {"recommendations": [
                {"isbn": "111", "why": "a"},
                {"isbn": "", "why": "x"},
                {"why": "no isbn"},
            ]}
        )
        self.assertEqual(out, [{"isbn": "111", "why": "a"}])


class IsbnFormsTest(unittest.TestCase):
    def test_expands_forms(self):
        forms = _isbn_forms("9780441013593")  # ISBN-13 of Dune
        self.assertIn("9780441013593", forms)
        self.assertIn(to_isbn10("9780441013593"), forms)


# -- the loop -----------------------------------------------------------------

class RunAssistantTest(unittest.TestCase):
    def test_clarify_path_returns_reply_no_picks(self):
        client = _FakeClient([_text("What genre do you enjoy?")])
        result = run_assistant("recommend something", [], CTX, CFG, client=client)
        self.assertEqual(result.reply, "What genre do you enjoy?")
        self.assertEqual(result.picks, [])
        self.assertEqual(result.tool_trace, [])
        self.assertFalse(result.truncated)

    def test_present_then_reply(self):
        client = _FakeClient([
            _present([{"isbn": "111", "why": "epic"}]),
            _text("Here's one you might like!"),
        ])
        result = run_assistant("something like Dune", [], CTX, CFG, client=client)
        self.assertEqual(result.reply, "Here's one you might like!")
        self.assertEqual(result.picks, [{"isbn": "111", "why": "epic"}])
        self.assertEqual([t["tool"] for t in result.tool_trace], [PRESENT_TOOL])

    def test_history_is_prepended_and_new_message_last(self):
        client = _FakeClient([_text("ok")])
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        run_assistant("next", history, CTX, CFG, client=client)
        sent = client.messages.calls[0]["messages"]
        self.assertEqual(sent[0], {"role": "user", "content": "hi"})
        self.assertEqual(sent[-1], {"role": "user", "content": "next"})

    def test_max_rounds_truncates(self):
        # A client that always asks for a tool never ends -> hits the cap.
        cfg = AssistantConfig(max_tool_rounds=3)
        client = _FakeClient([_present([{"isbn": "1", "why": "x"}])], repeat_last=True)
        result = run_assistant("loop", [], CTX, cfg, client=client)
        self.assertTrue(result.truncated)
        self.assertEqual(len(client.messages.calls), 3)


if __name__ == "__main__":
    unittest.main()
