"""Prompt-cache breakpoint placement in the native Anthropic adapter.

Pins the port of opencode's "auto" cache policy: tools → system → latest
*user* message, capped at 4 breakpoints. Only the Anthropic Messages protocol
emits inline markers, so these live with that adapter.
"""
from __future__ import annotations

from app.llm import anthropic_messages as am


def _build(messages, tool_schemas):
    """Run the adapter's request-lowering up to the cache pass and return the
    mutated (system, msgs, tools) the body would carry."""
    system, msgs = am._lower_messages(messages)
    tools = am._lower_tools(tool_schemas)
    am._apply_cache_breakpoints(system, msgs, tools)
    return system, msgs, tools


TOOLS = [{"type": "function", "function": {"name": "t1", "description": "d", "parameters": {}}}]


def test_auto_marks_last_tool_system_and_latest_user():
    system, msgs, tools = _build(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "latest user"},
        ],
        TOOLS,
    )
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    # latest user message's last content block carries the marker.
    latest_user = [m for m in msgs if m["role"] == "user"][-1]
    assert latest_user["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_breakpoint_sits_on_user_not_trailing_tool_results():
    # A turn that exploded into assistant/tool rounds: the breakpoint must stay
    # on the user message, not drift onto the trailing tool-result tail.
    system, msgs, tools = _build(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t1", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "t1", "content": "result"},
        ],
        TOOLS,
    )
    user_msg = next(m for m in msgs if m["role"] == "user" and m["content"][0].get("type") == "text")
    assert user_msg["content"][-1].get("cache_control") == {"type": "ephemeral"}
    # The tool_result-bearing user block (Anthropic folds tool results into a
    # user turn) must not be marked.
    tool_result_msg = next(
        m for m in msgs if m["role"] == "user" and m["content"][0].get("type") == "tool_result"
    )
    assert "cache_control" not in tool_result_msg["content"][-1]


def test_never_exceeds_breakpoint_cap():
    system, msgs, tools = _build(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        TOOLS,
    )
    marks = sum(
        1 for block in (tools + system + [b for m in msgs for b in m["content"]])
        if isinstance(block, dict) and "cache_control" in block
    )
    assert marks <= am._CACHE_BREAKPOINT_CAP


def test_no_tools_no_system_is_graceful():
    system, msgs, tools = _build([{"role": "user", "content": "hi"}], [])
    assert tools == []
    assert system == []
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
