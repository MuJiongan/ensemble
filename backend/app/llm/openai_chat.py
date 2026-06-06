"""OpenAI chat-completions protocol adapter.

The default protocol — used by OpenAI and every OpenAI-compatible upstream
(OpenRouter, Groq, Mistral, xAI, DeepSeek, Together, Cerebras, DeepInfra,
Perplexity, …). Reasoning variants apply as ``reasoning_effort`` /
``reasoning:{effort}`` (the latter for OpenRouter).
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from app.catalog.variants import to_openai_body
from app.llm.sse import iter_sse_json, compute_cost

PROTOCOL = "openai_chat"


def _is_openrouter(base_url: str) -> bool:
    return "openrouter" in base_url.lower()


def _merge_reasoning_delta(blocks: list[dict], rd: dict) -> str:
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
        target = {k: v for k, v in {
            "type": rd.get("type") or "reasoning.text",
            "text": "",
            "id": rd_id,
            "format": rd.get("format"),
            "index": rd_index,
        }.items() if v is not None}
        blocks.append(target)
    if delta_text:
        target["text"] = (target.get("text") or "") + delta_text
    for k in ("signature", "format", "type"):
        v = rd.get(k)
        if v is not None:
            target[k] = v
    return delta_text


def _parse_stream(chunks: Iterator[dict]) -> Iterator[tuple]:
    content_parts: list[str] = []
    tool_calls_by_index: dict[int, dict] = {}
    reasoning_blocks: list[dict] = []
    usage: dict = {}

    for chunk in chunks:
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
        if not rds:
            flat = delta.get("reasoning")
            if flat:
                _merge_reasoning_delta(reasoning_blocks, {"type": "reasoning.text", "text": flat})
                yield ("thinking", flat)

        c = delta.get("content")
        if c:
            content_parts.append(c)
            yield ("text", c)

        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            cur = tool_calls_by_index.setdefault(
                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if tc.get("id"):
                cur["id"] = tc["id"]
            if tc.get("type"):
                cur["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["function"]["name"] = fn["name"]
            args_delta = fn.get("arguments")
            if args_delta is not None:
                cur["function"]["arguments"] += args_delta
                if args_delta:
                    yield ("tool_args", idx, cur["function"]["name"], args_delta)

    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls_by_index:
        msg["tool_calls"] = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
    if reasoning_blocks:
        msg["reasoning_details"] = reasoning_blocks
    yield ("done", {"message": msg, "usage": usage})


def parse_sse_lines(lines: Iterator[str], cancel_event=None) -> Iterator[tuple]:
    """Parse raw OpenAI-compatible SSE ``data:`` lines into gorchestra event
    tuples. The canonical chat-completions stream parser, shared by the node
    runner, the orchestrator, and the protocol layer."""
    return _parse_stream(iter_sse_json(lines, cancel_event))


def _build_payload(model, messages, tool_schemas, base_url, variant_opts, streaming, extra_body):
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if _is_openrouter(base_url):
        payload["usage"] = {"include": True}
    if tool_schemas:
        payload["tools"] = tool_schemas
    if streaming:
        payload["stream"] = True
    for k, v in to_openai_body(variant_opts or {}).items():
        payload[k] = v
    # Caller-supplied opts win (merged last).
    for k, v in (extra_body or {}).items():
        payload[k] = v
    return payload


def stream_round(
    *,
    model: str,
    messages: list[dict],
    tool_schemas: list[dict],
    base_url: str,
    api_key: str,
    variant_opts: dict | None = None,
    extra_headers: dict | None = None,
    model_output_limit: int = 0,
    cost: dict | None = None,
    streaming: bool = True,
    extra_body: dict | None = None,
    cancel_event=None,
) -> Iterator[tuple]:
    chat_url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    payload = _build_payload(model, messages, tool_schemas, base_url, variant_opts, streaming, extra_body)

    with httpx.Client(timeout=None) as client:
        if streaming:
            with client.stream("POST", chat_url, headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    body = r.read().decode(errors="replace")[:500]
                    raise RuntimeError(f"LLM {r.status_code}: {body}")

                def _lines():
                    for line in r.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        yield line

                for item in parse_sse_lines(_lines(), cancel_event):
                    if item[0] == "done" and not item[1]["usage"].get("cost"):
                        item[1]["usage"]["cost"] = compute_cost(item[1]["usage"], cost)
                    yield item
        else:
            r = client.post(chat_url, headers=headers, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"LLM {r.status_code}: {r.text[:500]}")
            data = r.json()
            usage = data.get("usage") or {}
            msg = (data.get("choices") or [{}])[0].get("message") or {"role": "assistant", "content": ""}
            if not usage.get("cost"):
                usage["cost"] = compute_cost(usage, cost)
            yield ("done", {"message": msg, "usage": usage})
