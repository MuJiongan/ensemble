"""Anthropic Messages protocol adapter.

Port of opencode's anthropic-messages.ts, scoped to gorchestra's needs:
translates chat-completions-shaped messages to Anthropic's content-block body
(system / text / thinking / tool_use / tool_result), streams ``/v1/messages``,
and re-emits gorchestra's event tuples. Reasoning variants apply as the native
``thinking: {type: enabled, budget_tokens}`` field.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from app.llm.sse import iter_sse_json, compute_cost

PROTOCOL = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# effort → thinking budget (tokens). Mirrors the spread of opencode's anthropic
# variant budgets across the low→max effort scale.
_EFFORT_BUDGET = {"low": 4096, "medium": 10000, "high": 16000, "xhigh": 24000, "max": 31999}


def _thinking_budget(variant_opts: dict | None) -> int | None:
    if not variant_opts:
        return None
    t = variant_opts.get("thinking")
    if isinstance(t, dict):
        if t.get("type") == "enabled":
            b = t.get("budgetTokens") or t.get("budget_tokens")
            if isinstance(b, int):
                return b
        eff = t.get("effort") or variant_opts.get("effort")
        if eff in _EFFORT_BUDGET:
            return _EFFORT_BUDGET[eff]
    eff = variant_opts.get("effort")
    if eff in _EFFORT_BUDGET:
        return _EFFORT_BUDGET[eff]
    return None


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _lower_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, anthropic_messages)."""
    system: list[dict] = []
    out: list[dict] = []

    def push_user_block(block: dict) -> None:
        if out and out[-1]["role"] == "user":
            out[-1]["content"].append(block)
        else:
            out.append({"role": "user", "content": [block]})

    for m in messages:
        role = m.get("role")
        if role == "system":
            txt = _as_text(m.get("content"))
            if txt:
                system.append({"type": "text", "text": txt})
            continue
        if role == "user":
            push_user_block({"type": "text", "text": _as_text(m.get("content"))})
            continue
        if role == "tool":
            push_user_block({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": _as_text(m.get("content")),
            })
            continue
        if role == "assistant":
            blocks: list[dict] = []
            for rd in (m.get("reasoning_details") or []):
                text = rd.get("text") or rd.get("thinking")
                sig = rd.get("signature")
                # Anthropic rejects unsigned thinking blocks on input.
                if text and sig:
                    blocks.append({"type": "thinking", "thinking": text, "signature": sig})
            txt = _as_text(m.get("content"))
            if txt:
                blocks.append({"type": "text", "text": txt})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "", "name": fn.get("name") or "", "input": args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
    return system, out


def _lower_tools(tool_schemas: list[dict]) -> list[dict]:
    tools = []
    for t in tool_schemas or []:
        fn = t.get("function") or {}
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        params = {k: v for k, v in params.items() if k != "$schema"}
        tools.append({"name": fn.get("name"), "description": fn.get("description") or "", "input_schema": params})
    return tools


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
    extra_body: dict | None = None,  # OpenAI-shaped caller opts; not portable here.
    cancel_event=None,
) -> Iterator[tuple]:
    system, msgs = _lower_messages(messages)
    budget = _thinking_budget(variant_opts)
    max_tokens = model_output_limit or 8192
    if budget and max_tokens <= budget:
        max_tokens = budget + 4096

    body: dict[str, Any] = {"model": model, "messages": msgs, "stream": True, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    tools = _lower_tools(tool_schemas)
    if tools:
        body["tools"] = tools
    if budget:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    headers.update(extra_headers or {})

    url = base_url.rstrip("/") + "/messages"

    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    reasoning: dict[int, dict] = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    with httpx.Client(timeout=None) as client:
        with client.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code >= 400:
                detail = r.read().decode(errors="replace")[:500]
                raise RuntimeError(f"LLM {r.status_code}: {detail}")

            def _lines():
                for line in r.iter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    yield line

            for ev in iter_sse_json(_lines(), cancel_event):
                etype = ev.get("type")
                if etype == "message_start":
                    u = (ev.get("message") or {}).get("usage") or {}
                    usage["prompt_tokens"] = (
                        (u.get("input_tokens") or 0)
                        + (u.get("cache_read_input_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0)
                    )
                elif etype == "content_block_start":
                    idx = ev.get("index", 0)
                    block = ev.get("content_block") or {}
                    bt = block.get("type")
                    if bt in ("tool_use", "server_tool_use"):
                        tool_calls[idx] = {
                            "id": block.get("id") or str(idx),
                            "type": "function",
                            "function": {"name": block.get("name") or "", "arguments": ""},
                        }
                    elif bt == "text" and block.get("text"):
                        content_parts.append(block["text"])
                        yield ("text", block["text"])
                    elif bt == "thinking" and block.get("thinking"):
                        reasoning.setdefault(idx, {"type": "thinking", "text": "", "signature": None})
                        reasoning[idx]["text"] += block["thinking"]
                        yield ("thinking", block["thinking"])
                elif etype == "content_block_delta":
                    idx = ev.get("index", 0)
                    delta = ev.get("delta") or {}
                    dt = delta.get("type")
                    if dt == "text_delta" and delta.get("text"):
                        content_parts.append(delta["text"])
                        yield ("text", delta["text"])
                    elif dt == "thinking_delta" and delta.get("thinking"):
                        reasoning.setdefault(idx, {"type": "thinking", "text": "", "signature": None})
                        reasoning[idx]["text"] += delta["thinking"]
                        yield ("thinking", delta["thinking"])
                    elif dt == "signature_delta" and delta.get("signature"):
                        reasoning.setdefault(idx, {"type": "thinking", "text": "", "signature": None})
                        reasoning[idx]["signature"] = delta["signature"]
                    elif dt == "input_json_delta" and idx in tool_calls:
                        pj = delta.get("partial_json") or ""
                        tool_calls[idx]["function"]["arguments"] += pj
                        if pj:
                            yield ("tool_args", idx, tool_calls[idx]["function"]["name"], pj)
                elif etype == "message_delta":
                    u = ev.get("usage") or {}
                    if u.get("output_tokens") is not None:
                        usage["completion_tokens"] = u["output_tokens"]
                elif etype == "error":
                    err = ev.get("error") or {}
                    raise RuntimeError(f"Anthropic stream error: {err.get('type','')}: {err.get('message','')}")

    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    if reasoning:
        msg["reasoning_details"] = [reasoning[i] for i in sorted(reasoning)]
    usage["cost"] = compute_cost(usage, cost)
    yield ("done", {"message": msg, "usage": usage})
