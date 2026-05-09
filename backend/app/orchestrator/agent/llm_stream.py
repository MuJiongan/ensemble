"""OpenRouter streaming + SSE parsing for the orchestrator agent.

Pulled out of the agent loop so the chunk parser can be unit-tested without
spinning up an HTTP server, and so the loop module isn't bogged down in
provider-specific protocol details.
"""
from __future__ import annotations
import json
import os
import threading
from typing import Any, Iterator

import httpx


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

REASONING_EFFORT = "medium"


def _merge_reasoning_delta(blocks: list[dict], rd: dict) -> str:
    """Fold one streaming reasoning_details delta into ``blocks`` in place.

    Multiple deltas may share an ``id`` (concatenated) or arrive without one
    (matched by ``index``). Returns the text portion of this delta — caller
    yields it as a ``thinking`` chunk if non-empty.
    """
    delta_text = rd.get("text") or ""
    rd_id = rd.get("id")
    rd_index = rd.get("index")
    target = None
    for b in blocks:
        if rd_id and b.get("id") == rd_id:
            target = b
            break
        if rd_index is not None and b.get("index") == rd_index and not rd_id:
            target = b
            break
    if target is None:
        target = {
            "type": rd.get("type") or "reasoning.text",
            "text": "",
            "id": rd_id,
            "format": rd.get("format"),
            "index": rd_index,
        }
        # Drop None values to keep the block close to what the server sent.
        target = {k: v for k, v in target.items() if v is not None}
        blocks.append(target)
    if delta_text:
        target["text"] = (target.get("text") or "") + delta_text
    # Carry through other metadata that may arrive on subsequent chunks.
    for k in ("signature", "format", "type"):
        v = rd.get(k)
        if v is not None:
            target[k] = v
    return delta_text


def _apply_tool_call_delta(by_index: dict[int, dict], tc_delta: dict) -> None:
    """Fold one streaming tool_calls delta into ``by_index`` in place.

    Tool calls arrive piece-by-piece, indexed by ``index``: id once, name
    once, arguments incrementally.
    """
    idx = tc_delta.get("index", 0)
    cur = by_index.setdefault(
        idx,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if tc_delta.get("id"):
        cur["id"] = tc_delta["id"]
    if tc_delta.get("type"):
        cur["type"] = tc_delta["type"]
    fn_delta = tc_delta.get("function") or {}
    if fn_delta.get("name"):
        cur["function"]["name"] = fn_delta["name"]
    if fn_delta.get("arguments") is not None:
        cur["function"]["arguments"] += fn_delta.get("arguments", "")


def _assemble_message(
    content: str,
    tool_calls_by_index: dict[int, dict],
    reasoning_blocks: list[dict],
) -> dict:
    """Build the final assistant message from the per-stream accumulators.

    ``reasoning_details`` is preserved verbatim so callers can persist + echo
    it back on subsequent turns — Anthropic enforces ordering of these blocks.
    """
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls_by_index:
        msg["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index.keys())
        ]
    if reasoning_blocks:
        msg["reasoning_details"] = reasoning_blocks
    return msg


def _parse_sse_chunks(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    """Parse OpenAI/OpenRouter-compatible streaming SSE lines.

    Yields:
      ``("text", delta_str)`` for each visible text delta,
      ``("thinking", delta_str)`` for each reasoning text delta,
      ``("done", {"message": <full assistant msg>, "usage": <dict>})`` once
      the stream terminates (with ``data: [DONE]`` or natural EOF).

    Pulled out as a pure generator over lines so it can be unit-tested without
    spinning up an HTTP server.
    """
    content_parts: list[str] = []
    tool_calls_by_index: dict[int, dict] = {}
    reasoning_blocks: list[dict] = []
    usage: dict = {}

    for line in lines:
        # SSE frames may carry comments (`:` prefix) or `event:`/`id:` lines —
        # we only care about `data:`.
        if not line or not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue

        # OpenRouter sometimes piggybacks usage at the tail of the stream.
        u = chunk.get("usage")
        if u:
            usage = u

        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}

        rds = delta.get("reasoning_details") or []
        for rd in rds:
            t = _merge_reasoning_delta(reasoning_blocks, rd)
            if t:
                yield ("thinking", t)
        # Some providers also surface a flat `reasoning` text delta; treat it
        # as a fallback when no structured details are provided.
        if not rds:
            flat_reasoning = delta.get("reasoning")
            if flat_reasoning:
                _merge_reasoning_delta(
                    reasoning_blocks,
                    {"type": "reasoning.text", "text": flat_reasoning},
                )
                yield ("thinking", flat_reasoning)

        content_delta = delta.get("content")
        if content_delta:
            content_parts.append(content_delta)
            yield ("text", content_delta)

        for tc_delta in (delta.get("tool_calls") or []):
            _apply_tool_call_delta(tool_calls_by_index, tc_delta)

    yield (
        "done",
        {
            "message": _assemble_message(
                "".join(content_parts), tool_calls_by_index, reasoning_blocks
            ),
            "usage": usage,
        },
    )


def _call_openrouter_stream(
    model: str,
    messages: list[dict],
    tool_specs: list[dict],
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, Any]]:
    """Open a streaming chat completion against OpenRouter and yield parsed
    chunks via :func:`_parse_sse_chunks`.

    If ``cancel_event`` is provided and gets set during streaming, the SSE
    read terminates between chunks and the parser emits a final ``done``
    with whatever partial text was assembled — letting the agent loop bail
    immediately instead of waiting for the LLM to finish."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "tools": tool_specs,
        "stream": True,
        "reasoning": {"effort": REASONING_EFFORT},
        # Opt into OpenRouter's cost accounting — without this, `usage.cost`
        # is omitted from the final stream chunk and orchestrator cost is $0.
        "usage": {"include": True},
    }
    with httpx.Client(timeout=None) as client:
        with client.stream("POST", OPENROUTER_URL, headers=headers, json=payload) as r:
            if r.status_code >= 400:
                body = r.read().decode(errors="replace")[:500]
                raise RuntimeError(f"OpenRouter {r.status_code}: {body}")

            def cancellable_lines():
                for line in r.iter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    yield line

            yield from _parse_sse_chunks(cancellable_lines())
