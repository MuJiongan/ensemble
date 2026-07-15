"""Node-embedded custom tool registration and dispatch."""
from __future__ import annotations

import pytest

from app.runner import llm as llm_mod
from app.runner import chat_child
from app.runner.ctx import Ctx
from app.runner.node_tools import execute_node_source
from app.runner.tools import REGISTRY


TOOL_SOURCE = '''
from app.runner.node_tools import NodeTool

class lookup_order(NodeTool):
    description = "Look up an order by id."
    parameters = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    }

    def execute(self, ctx, order_id):
        ctx.log(f"looked up {order_id}")
        return {"order_id": order_id, "status": "shipped"}

def run(inputs, ctx):
    result = ctx.agent(
        prompt=f"Help with order {inputs['order_id']}",
        tools=["lookup_order"],
    )
    return {"answer": result["content"]}
'''


def test_embedded_tool_auto_registers_on_source_execution(tmp_path):
    ctx = Ctx(workdir=tmp_path, default_model="test-model")

    namespace = execute_node_source(TOOL_SOURCE, ctx.register_node_tool)

    assert "lookup_order" in ctx.tool_registry
    assert "lookup_order" in ctx.tool_schemas
    assert ctx.tool_schemas["lookup_order"]["function"] == {
        "name": "lookup_order",
        "description": "Look up an order by id.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    }
    assert callable(namespace["run"])
    # Registration is local to this node context, never process-global.
    assert "lookup_order" not in REGISTRY


def test_custom_tool_uses_existing_quoted_agent_surface(tmp_path, monkeypatch):
    ctx = Ctx(workdir=tmp_path, default_model="test-model")
    namespace = execute_node_source(TOOL_SOURCE, ctx.register_node_tool)
    captured = {}

    def fake_call_llm(
        model,
        prompt,
        tools,
        tool_registry,
        tool_schemas_by_name,
        **kwargs,
    ):
        captured.update(
            model=model,
            prompt=prompt,
            tools=tools,
            registry=tool_registry,
            schemas=tool_schemas_by_name,
        )
        tool_result = tool_registry["lookup_order"](order_id="ord-7")
        return {
            "content": tool_result["status"],
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": tool_result["status"]},
            ],
            "tool_calls_made": [{
                "name": "lookup_order",
                "args": {"order_id": "ord-7"},
                "result": tool_result,
            }],
            "usage": {},
            "cost": 0.0,
        }

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    result = namespace["run"]({"order_id": "ord-7"}, ctx)

    assert result == {"answer": "shipped"}
    assert captured["tools"] == ["lookup_order"]
    assert captured["registry"] is ctx.tool_registry
    assert captured["schemas"] is ctx.tool_schemas
    assert ctx.llm_calls[0]["tools"] == ["lookup_order"]
    assert ctx.tool_calls[0]["name"] == "lookup_order"
    assert "looked up ord-7" in ctx.logs


def test_custom_tool_is_available_through_direct_proxy(tmp_path):
    ctx = Ctx(workdir=tmp_path, default_model="test-model")
    execute_node_source(TOOL_SOURCE, ctx.register_node_tool)

    # Direct custom calls follow the native tools' positional-or-keyword
    # behavior even though agentic dispatch always supplies JSON kwargs.
    result = ctx.tools.lookup_order("ord-9")

    assert result == {"order_id": "ord-9", "status": "shipped"}
    assert ctx.tool_calls[-1] == {
        "name": "lookup_order",
        "args": {"order_id": "ord-9"},
        "via": "direct",
        "result": result,
    }


def test_same_custom_name_is_isolated_between_node_contexts(tmp_path):
    source = '''
from app.runner.node_tools import NodeTool
class local_tool(NodeTool):
    description = "Return this node's value."
    parameters = {"type": "object", "properties": {}}
    def execute(self, ctx):
        return {"value": VALUE}
'''
    first = Ctx(workdir=tmp_path, default_model="test-model")
    second = Ctx(workdir=tmp_path, default_model="test-model")

    execute_node_source("VALUE = 'first'\n" + source, first.register_node_tool)
    execute_node_source("VALUE = 'second'\n" + source, second.register_node_tool)

    assert first.tools.local_tool() == {"value": "first"}
    assert second.tools.local_tool() == {"value": "second"}
    assert first.tool_registry["local_tool"] is not second.tool_registry["local_tool"]


def test_continued_chat_restores_tool_from_frozen_node_source(tmp_path, monkeypatch):
    events = []
    seen = {}
    messages = [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(chat_child, "_install_sigterm_handler", lambda: None)
    monkeypatch.setattr(chat_child, "_load_mcp_tools", lambda: None)
    monkeypatch.setattr(chat_child, "_emit", events.append)
    monkeypatch.setattr(chat_child, "_read_payload", lambda: {
        "messages": messages,
        "tools": ["lookup_order"],
        "node_code": TOOL_SOURCE,
        "model": "test-model",
        "workdir": str(tmp_path),
    })

    def fake_agent(self, model=None, prompt=None, tools=None, **kwargs):
        seen["tools"] = tools
        seen["result"] = self.tool_registry["lookup_order"](order_id="ord-11")
        return {
            "messages": [*messages, {"role": "assistant", "content": "done"}],
            "usage": {},
            "cost": 0.0,
        }

    monkeypatch.setattr(Ctx, "agent", fake_agent)

    chat_child.main()

    assert seen == {
        "tools": ["lookup_order"],
        "result": {"order_id": "ord-11", "status": "shipped"},
    }
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["status"] == "success"


@pytest.mark.parametrize("name", ["LookupOrder", "lookupOrder", "LOOKUP_ORDER"])
def test_custom_tool_requires_native_lowercase_casing(tmp_path, name):
    source = f'''
from app.runner.node_tools import NodeTool
class {name}(NodeTool):
    description = "bad casing"
    parameters = {{"type": "object", "properties": {{}}}}
    def execute(self, ctx):
        return {{}}
'''
    ctx = Ctx(workdir=tmp_path, default_model="test-model")

    with pytest.raises(ValueError, match="lowercase snake_case"):
        execute_node_source(source, ctx.register_node_tool)


def test_custom_tool_cannot_shadow_native_tool(tmp_path):
    source = '''
from app.runner.node_tools import NodeTool
class shell(NodeTool):
    description = "shadow shell"
    parameters = {"type": "object", "properties": {}}
    def execute(self, ctx):
        return {}
'''
    ctx = Ctx(workdir=tmp_path, default_model="test-model")

    with pytest.raises(ValueError, match="conflicts with an existing tool"):
        execute_node_source(source, ctx.register_node_tool)
