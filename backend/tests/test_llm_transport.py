"""Pins the SDK↔parser seam of the protocol adapters.

The adapters stream through the official provider SDKs and feed
``model_dump()``-ed events to gorchestra's own parsers. These tests push real
SDK model objects through that seam so an SDK upgrade that changes dump shapes
(aliases, bytes handling, dropped extras) fails loudly here instead of
silently corrupting a stream.
"""
from __future__ import annotations

import json

from app.llm import anthropic_messages, gemini, openai_chat


# ---------------------------------------------------------------------------
# openai chat — extension fields (OpenRouter) must survive model_dump
# ---------------------------------------------------------------------------

def _chat_chunk(delta: dict, usage: dict | None = None):
    from openai.types.chat import ChatCompletionChunk

    raw = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    if usage is not None:
        raw["usage"] = usage
    return ChatCompletionChunk.model_validate(raw).model_dump(exclude_none=True)


def test_openai_chunks_roundtrip_through_sdk_models():
    chunks = [
        _chat_chunk({"role": "assistant", "reasoning_details": [{"type": "reasoning.text", "text": "hm"}]}),
        _chat_chunk({"content": "hi"}),
        _chat_chunk(
            {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        ),
    ]
    events = list(openai_chat._parse_stream(iter(chunks)))
    assert ("thinking", "hm") in events
    assert ("text", "hi") in events
    done = events[-1][1]
    assert done["message"]["content"] == "hi"
    assert done["message"]["tool_calls"][0]["id"] == "call_1"
    # OpenRouter's extension field survives the SDK's pydantic models.
    assert done["message"]["reasoning_details"][0]["text"] == "hm"
    assert done["usage"]["prompt_tokens"] == 5


# ---------------------------------------------------------------------------
# anthropic — raw stream events dump to the wire shape the parser reads
# ---------------------------------------------------------------------------

def test_anthropic_events_roundtrip_through_sdk_models():
    from anthropic.types import (
        RawContentBlockDeltaEvent,
        RawContentBlockStartEvent,
        RawMessageDeltaEvent,
        RawMessageStartEvent,
    )

    events = [
        RawMessageStartEvent.model_validate({
            "type": "message_start",
            "message": {
                "id": "m1", "type": "message", "role": "assistant", "model": "claude",
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0,
                          "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2},
            },
        }),
        RawContentBlockStartEvent.model_validate({
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
        }),
        RawContentBlockDeltaEvent.model_validate({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        }),
        RawMessageDeltaEvent.model_validate({
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        }),
    ]
    parsed = list(anthropic_messages._parse_events(e.model_dump(exclude_none=True) for e in events))
    assert ("tool_args", 0, "f", "{}") in parsed
    done = parsed[-1][1]
    assert done["message"]["tool_calls"][0] == {
        "id": "toolu_1", "type": "function", "function": {"name": "f", "arguments": "{}"}
    }
    assert done["usage"]["prompt_tokens"] == 16  # input + cache read + cache write
    assert done["usage"]["cache_read_tokens"] == 4
    assert done["usage"]["completion_tokens"] == 3


# ---------------------------------------------------------------------------
# gemini — JSON-mode in/out keeps base64 fields lossless
# ---------------------------------------------------------------------------

def test_gemini_content_base64_roundtrip():
    from google.genai import types

    wire = {
        "role": "model",
        "parts": [
            {"text": "thought…", "thought": True, "thoughtSignature": "c2ln"},
            {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
        ],
    }
    content = types.Content.model_validate_json(json.dumps(wire))
    back = content.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert back["parts"][0]["thoughtSignature"] == "c2ln"
    assert back["parts"][1]["inlineData"]["data"] == "QUJD"


def test_gemini_chunks_roundtrip_through_sdk_models():
    from google.genai import types

    chunk = types.GenerateContentResponse.model_validate_json(json.dumps({
        "candidates": [{
            "content": {"role": "model", "parts": [
                {"text": "think", "thought": True, "thoughtSignature": "c2ln"},
                {"text": "answer"},
                {"functionCall": {"name": "f", "args": {"a": 1}}, "thoughtSignature": "c2ln"},
            ]},
        }],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3,
                          "thoughtsTokenCount": 2, "cachedContentTokenCount": 1},
    }))
    ev = chunk.model_dump(mode="json", by_alias=True, exclude_none=True)
    events = list(gemini._parse_events(iter([ev])))
    assert ("thinking", "think") in events
    assert ("text", "answer") in events
    done = events[-1][1]
    assert done["message"]["reasoning_details"][0]["thoughtSignature"] == "c2ln"
    tc = done["message"]["tool_calls"][0]
    assert tc["thoughtSignature"] == "c2ln"
    assert json.loads(tc["function"]["arguments"]) == {"a": 1}
    assert done["usage"] == {"prompt_tokens": 9, "completion_tokens": 5, "cache_read_tokens": 1}


# ---------------------------------------------------------------------------
# openai responses — typed stream events dump to what the parser reads
# ---------------------------------------------------------------------------

def test_responses_events_roundtrip_through_sdk_models():
    from openai.types.responses import (
        ResponseCompletedEvent,
        ResponseFunctionCallArgumentsDeltaEvent,
        ResponseOutputItemAddedEvent,
        ResponseTextDeltaEvent,
    )
    from app.llm import openai_responses as resp

    events = [
        ResponseOutputItemAddedEvent.model_validate({
            "type": "response.output_item.added", "output_index": 0, "sequence_number": 1,
            "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                     "name": "f", "arguments": "", "status": "in_progress"},
        }),
        ResponseFunctionCallArgumentsDeltaEvent.model_validate({
            "type": "response.function_call_arguments.delta", "item_id": "fc_1",
            "output_index": 0, "sequence_number": 2, "delta": "{}",
        }),
        ResponseTextDeltaEvent.model_validate({
            "type": "response.output_text.delta", "item_id": "msg_1", "output_index": 1,
            "content_index": 0, "sequence_number": 3, "delta": "hi", "logprobs": [],
        }),
        ResponseCompletedEvent.model_validate({
            "type": "response.completed", "sequence_number": 4,
            "response": {
                "id": "r1", "object": "response", "created_at": 1, "model": "gpt-5.5",
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "output": [],
                "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10,
                          "input_tokens_details": {"cached_tokens": 5},
                          "output_tokens_details": {"reasoning_tokens": 0}},
            },
        }),
    ]
    parsed = list(resp.parse_responses_events(e.model_dump(exclude_none=True) for e in events))
    assert ("tool_args", 0, "f", "{}") in parsed
    assert ("text", "hi") in parsed
    done = parsed[-1][1]
    assert done["message"]["tool_calls"][0]["id"] == "call_1"
    assert done["usage"] == {"prompt_tokens": 8, "completion_tokens": 2, "cache_read_tokens": 5}
