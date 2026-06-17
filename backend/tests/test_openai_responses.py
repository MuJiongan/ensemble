"""Tests for the OpenAI Responses protocol adapter (app/llm/openai_responses.py)
and its routing — native OpenAI must not hit chat completions, which rejects
function tools + reasoning_effort for gpt-5.5+."""
from __future__ import annotations

import json

from app.catalog.models_dev import CatalogModel, ModelLimit
from app.catalog.variants import variants as compute_variants
from app.llm import openai_responses as resp
from app.llm import router


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _catalog_model(**kw) -> CatalogModel:
    m = CatalogModel(
        id=kw.get("id", "gpt-5.5"),
        name=kw.get("id", "gpt-5.5"),
        provider_id=kw.get("provider_id", "openai"),
        api_id=kw.get("api_id", kw.get("id", "gpt-5.5")),
        npm=kw.get("npm", "@ai-sdk/openai"),
        api_url="",
        reasoning=kw.get("reasoning", True),
        release_date=kw.get("release_date", "2025-12-10"),
        limit=ModelLimit(context=400000, output=128000),
    )
    object.__setattr__(m, "variants", compute_variants(m))
    return m


def test_router_selects_responses_for_native_openai(monkeypatch):
    m = _catalog_model()
    monkeypatch.setattr(router.md, "get_model", lambda pid, mid: m)
    plan = router.plan("openai", "gpt-5.5", "high")
    assert plan.protocol == "openai_responses"
    assert plan.base_url == "https://api.openai.com/v1"
    assert plan.stream_round is resp.stream_round
    # The reasoning variant survives in the catalog's camelCase shape — the
    # adapter translates it itself (to_openai_body would have dropped the
    # Responses-only fields).
    assert plan.variant_opts["reasoningEffort"] == "high"
    assert plan.variant_opts["include"] == ["reasoning.encrypted_content"]


def test_router_selects_responses_on_catalog_miss(monkeypatch):
    monkeypatch.setattr(router.md, "get_model", lambda pid, mid: None)
    plan = router.plan("openai", "gpt-5.5", None)
    assert plan.protocol == "openai_responses"


def test_router_keeps_chat_for_openai_compatible(monkeypatch):
    m = _catalog_model(npm="@ai-sdk/openai-compatible", provider_id="groq")
    monkeypatch.setattr(router.md, "get_model", lambda pid, mid: m)
    plan = router.plan("groq", "gpt-5.5", None)
    assert plan.protocol == "openai_chat"


# ---------------------------------------------------------------------------
# Input lowering
# ---------------------------------------------------------------------------

def test_system_messages_become_instructions():
    instructions, items = resp.to_responses_input(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert instructions == "be terse"
    assert items == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_no_instructions_fallback_by_default():
    instructions, _ = resp.to_responses_input([{"role": "user", "content": "hi"}])
    assert instructions is None
    instructions, _ = resp.to_responses_input(
        [{"role": "user", "content": "hi"}], instructions_fallback=True
    )
    assert instructions == "hi"


def test_assistant_reasoning_echoed_before_tool_calls():
    rd = {
        "type": "reasoning.encrypted",
        "format": resp.REASONING_FORMAT,
        "summary": [{"type": "summary_text", "text": "thinking…"}],
        "text": "thinking…",
        "data": "ENC",
    }
    _, items = resp.to_responses_input(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                ],
                "reasoning_details": [rd],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
    )
    assert items == [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "thinking…"}],
            "encrypted_content": "ENC",
        },
        {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]
    # Item ids are never echoed back — store:false keeps requests stateless.
    assert all("id" not in i for i in items)


def test_foreign_or_pointer_reasoning_blocks_not_echoed():
    _, items = resp.to_responses_input(
        [
            {
                "role": "assistant",
                "content": "hi",
                "reasoning_details": [
                    # Anthropic-style block: wrong format → skipped.
                    {"type": "thinking", "text": "t", "signature": "sig"},
                    # Ours but summary-only (no encrypted payload) → skipped.
                    {"type": "reasoning.encrypted", "format": resp.REASONING_FORMAT, "text": "t"},
                ],
            }
        ]
    )
    assert items == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}
    ]


def test_reasoning_not_echoed_without_following_item():
    rd = {"type": "reasoning.encrypted", "format": resp.REASONING_FORMAT, "data": "ENC"}
    _, items = resp.to_responses_input(
        [{"role": "assistant", "content": "", "reasoning_details": [rd]}]
    )
    assert items == []


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

def _sse(events: list[dict]) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events]


def test_parse_stream_assembles_message_reasoning_and_usage():
    lines = _sse(
        [
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
            {"type": "response.reasoning_summary_text.delta", "delta": "first"},
            {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
            {"type": "response.reasoning_summary_text.delta", "delta": "second"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [
                        {"type": "summary_text", "text": "first"},
                        {"type": "summary_text", "text": "second"},
                    ],
                    "encrypted_content": "ENC",
                },
            },
            {"type": "response.output_text.delta", "delta": "hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "f"},
            },
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"a"'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": ":1}"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "f",
                    "arguments": '{"a":1}',
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 7,
                        "input_tokens_details": {"cached_tokens": 60},
                    }
                },
            },
        ]
    )
    events = list(resp.parse_responses_sse(iter(lines)))

    thinking = "".join(e[1] for e in events if e[0] == "thinking")
    assert thinking == "first\n\nsecond"
    assert "".join(e[1] for e in events if e[0] == "text") == "hello"
    assert [e[1:] for e in events if e[0] == "tool_args"] == [(0, "f", '{"a"'), (0, "f", ":1}")]

    done = events[-1][1]
    msg = done["message"]
    assert msg["content"] == "hello"
    assert msg["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}
    ]
    assert msg["reasoning_details"] == [
        {
            "type": "reasoning.encrypted",
            "format": resp.REASONING_FORMAT,
            "summary": [
                {"type": "summary_text", "text": "first"},
                {"type": "summary_text", "text": "second"},
            ],
            "text": "first\n\nsecond",
            "data": "ENC",
        }
    ]
    assert done["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "cache_read_tokens": 60,
    }


def test_parse_stream_raises_on_failure():
    lines = _sse(
        [
            {
                "type": "response.failed",
                "response": {"error": {"message": "boom"}},
            }
        ]
    )
    try:
        list(resp.parse_responses_sse(iter(lines)))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "boom" in str(e)


def test_parse_stream_raises_on_nested_error_event():
    """Codex and production OpenAI often nest stream errors under ``error``."""
    lines = _sse(
        [
            {
                "type": "error",
                "error": {
                    "code": "context_length_exceeded",
                    "message": "Your input exceeds the context window.",
                },
            }
        ]
    )
    try:
        list(resp.parse_responses_sse(iter(lines)))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "context_length_exceeded" in str(e)
        assert "context window" in str(e)


def test_stream_error_message_top_level():
    msg = resp._stream_error_message(
        {"type": "error", "code": "too_many_requests", "message": "Slow down"}
    )
    assert msg == "too_many_requests: Slow down"


def test_codex_payload_merges_variant_body():
    """Codex must send the same Responses variant fields as the API-key path."""
    from app.auth.codex_api import _request_payload
    from app.llm import router

    plan = router.plan("codex", "gpt-5.3-codex", "high")
    extra = {"stream": True, **resp._variant_body(plan.variant_opts)}
    payload = _request_payload(
        "gpt-5.3-codex", [{"role": "user", "content": "hi"}], [], extra
    )
    assert payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}


def test_parse_non_streaming_response_json():
    msg, usage = resp._parse_response_json(
        {
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "ENC",
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "hi"}],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "f",
                    "arguments": "{}",
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    )
    assert msg["content"] == "hi"
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["reasoning_details"][0]["data"] == "ENC"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 2}


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------

def test_variant_body_translates_responses_fields():
    body = resp._variant_body(
        {
            "reasoningEffort": "high",
            "reasoningSummary": "auto",
            "include": ["reasoning.encrypted_content"],
            "textVerbosity": "low",
        }
    )
    assert body == {
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"verbosity": "low"},
    }
    assert resp._variant_body(None) == {}


def test_build_payload_shape():
    payload = resp._build_payload(
        "gpt-5.5",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {}}}],
        {"reasoningEffort": "medium"},
        True,
        {"max_tokens": 256},
    )
    assert payload["model"] == "gpt-5.5"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == "sys"
    assert payload["tools"] == [
        {"type": "function", "name": "f", "description": "d", "parameters": {}}
    ]
    assert payload["reasoning"] == {"effort": "medium"}
    # Caller opts arrive chat-completions-shaped; remapped to Responses names.
    assert payload["max_output_tokens"] == 256
    assert "max_tokens" not in payload
