"""Codex (ChatGPT-subscription) LLM caller — OpenAI Responses API.

When the user signs in to ChatGPT, calls go to
``https://chatgpt.com/backend-api/codex/responses`` with the OAuth bearer plus
``ChatGPT-Account-Id``. The endpoint speaks the OpenAI **Responses API**, not
chat completions, so we translate our internal chat-completions shape
(messages list, tool_calls array) into Responses-API ``input`` items and
parse the resulting SSE stream back into the same
``(text|thinking|tool_args|done)`` events our existing parsers emit.

Two entry points mirroring the existing callers:

* :func:`call_codex_chat`        — node-runtime ``ctx.call_llm`` equivalent.
* :func:`call_codex_stream_orch` — orchestrator-loop equivalent (yields
  parsed chunks the agent loop already understands).
"""
from __future__ import annotations
import itertools
import json
import logging
import sys
import threading
import traceback
from typing import Any, Callable, Iterator, Optional

import httpx


CODEX_API_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Translation: chat-completions shape  →  Responses API shape
# ---------------------------------------------------------------------------

def _to_responses_input(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
    """Convert a chat-completions ``messages`` list into Responses API input.

    Returns ``(instructions, input_items)``:
      * ``instructions`` is the concatenated text of any ``role=system``
        messages (Responses API takes them as a separate top-level field).
        When the caller didn't supply a system message we fall back to the
        first user message's text — Codex requires ``instructions`` to be
        non-empty, and the first user turn is the closest stand-in for the
        task the model should follow.
      * ``input_items`` is the input list of ``message`` /
        ``function_call`` / ``function_call_output`` items, preserving
        original order.
    """
    instructions_parts: list[str] = []
    items: list[dict] = []
    first_user_text: Optional[str] = None

    for m in messages:
        role = m.get("role")
        if role == "system":
            content = m.get("content") or ""
            if isinstance(content, str) and content:
                instructions_parts.append(content)
            continue

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id") or "",
                    "output": m.get("content") or "",
                }
            )
            continue

        if role == "assistant":
            # Assistant turn may carry visible text and/or tool calls.
            content = m.get("content") or ""
            if isinstance(content, str) and content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "arguments": fn.get("arguments") or "",
                    }
                )
            continue

        # user / anything else → input_text message
        content = m.get("content") or ""
        text = content if isinstance(content, str) else json.dumps(content)
        items.append(
            {
                "type": "message",
                "role": role or "user",
                "content": [{"type": "input_text", "text": text}],
            }
        )
        if role == "user" and first_user_text is None and text:
            first_user_text = text

    if instructions_parts:
        instructions: Optional[str] = "\n\n".join(instructions_parts)
    else:
        instructions = first_user_text
    return instructions, items


def _to_responses_tools(tool_schemas: list[dict]) -> list[dict]:
    """Chat-completions tools (``{"type":"function","function":{...}}``) →
    Responses-API tools (flat: ``{"type":"function","name":...,"parameters":...}``)."""
    out: list[dict] = []
    for t in tool_schemas:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {},
            }
        )
    return out


# ---------------------------------------------------------------------------
# SSE parsing: Responses API events  →  internal event tuples
# ---------------------------------------------------------------------------

def _parse_responses_sse(lines: Iterator[str]) -> Iterator[tuple]:
    """Parse Codex Responses API SSE stream.

    Yields the same tuple shapes the existing chat-completions parser does:

      ``("text",     delta_str)``                     visible content delta
      ``("thinking", delta_str)``                     reasoning delta
      ``("tool_args", tc_index, name, args_delta)``   streaming tool call args
      ``("done", {"message": assistant_msg, "usage": {...}})``

    The assembled ``assistant_msg`` is in chat-completions shape — so the
    rest of the agent loop (which keeps a single chat-completions message
    history) can append it verbatim.
    """
    content_parts: list[str] = []
    # Track function_call items by their item_id (assigned in output_item.added).
    # The Responses API uses an opaque item_id; we map it to a stable index so
    # callers can co-render multiple in-flight tool calls.
    fn_items: dict[str, dict] = {}
    fn_order: list[str] = []
    usage: dict = {}

    for line in lines:
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            evt = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        ev_type = evt.get("type") or ""

        if ev_type == "response.output_item.added":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                item_id = item.get("id") or f"_idx{len(fn_order)}"
                fn_items[item_id] = {
                    "id": item.get("call_id") or item_id,
                    "type": "function",
                    "function": {"name": item.get("name") or "", "arguments": ""},
                }
                fn_order.append(item_id)
            continue

        if ev_type == "response.output_text.delta":
            delta = evt.get("delta") or ""
            if delta:
                content_parts.append(delta)
                yield ("text", delta)
            continue

        if ev_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning.delta",
        ):
            delta = evt.get("delta") or ""
            if delta:
                yield ("thinking", delta)
            continue

        if ev_type == "response.function_call_arguments.delta":
            item_id = evt.get("item_id") or ""
            delta = evt.get("delta") or ""
            cur = fn_items.get(item_id)
            if cur is None:
                # Argument delta arrived before the item.added event — rare;
                # synthesize a placeholder so we don't drop the data.
                cur = {
                    "id": item_id,
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
                fn_items[item_id] = cur
                fn_order.append(item_id)
            cur["function"]["arguments"] += delta
            if delta:
                idx = fn_order.index(item_id)
                yield ("tool_args", idx, cur["function"]["name"], delta)
            continue

        if ev_type == "response.completed":
            resp = evt.get("response") or {}
            u = resp.get("usage") or {}
            if u:
                # Normalize Responses-API usage keys to chat-completions ones
                # so cost/token UI keeps working.
                usage = {
                    "prompt_tokens": u.get("input_tokens", u.get("prompt_tokens", 0)) or 0,
                    "completion_tokens": u.get(
                        "output_tokens", u.get("completion_tokens", 0)
                    ) or 0,
                    # Subscription-billed; no per-call USD.
                    "cost": 0.0,
                }
            continue

        if ev_type == "response.failed":
            resp = evt.get("response") or {}
            err = (resp.get("error") or {}).get("message") or "Codex response failed"
            raise RuntimeError(f"Codex: {err}")

    tool_calls = [fn_items[i] for i in fn_order if fn_items[i]["function"]["name"]]
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    yield ("done", {"message": msg, "usage": usage})


# ---------------------------------------------------------------------------
# HTTP entry points
# ---------------------------------------------------------------------------

def _request_payload(
    model: str,
    messages: list[dict],
    tool_schemas: list[dict],
    extra: dict,
) -> dict:
    instructions, input_items = _to_responses_input(messages)
    if not instructions:
        # Codex's Responses endpoint rejects requests without ``instructions``
        # (HTTP 400 ``Instructions are required``). Reaching here means the
        # caller passed no system message and no user message either — there's
        # nothing meaningful to send, so fail fast instead of guessing.
        raise RuntimeError("Codex requires a system or user message; got an empty prompt")
    payload: dict = {
        "model": model,
        "input": input_items,
        "instructions": instructions,
        # Don't have OpenAI persist response state on their side — Codex's
        # ChatGPT-account flow doesn't need a server-managed thread.
        "store": False,
    }
    if tool_schemas:
        payload["tools"] = _to_responses_tools(tool_schemas)
    payload.update(extra)
    return payload


def _request_headers(access_token: str, account_id: Optional[str]) -> dict:
    h = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        # Identify ourselves for OAuth server logs / anti-abuse heuristics.
        "originator": "emdash",
    }
    if account_id:
        h["ChatGPT-Account-Id"] = account_id
    return h


def _reasoning_extra(reasoning_effort: Optional[str]) -> dict:
    """Responses-API reasoning control derived from the selected variant."""
    if not reasoning_effort:
        return {}
    return {"reasoning": {"effort": reasoning_effort, "summary": "auto"}}


def call_codex_stream(
    model: str,
    messages: list[dict],
    tool_specs: list[dict],
    access_token: str,
    account_id: Optional[str],
    cancel_event: Optional[threading.Event] = None,
    reasoning_effort: Optional[str] = None,
) -> Iterator[tuple[str, Any]]:
    """Streaming entry point for the orchestrator agent loop.

    Same signature shape as :func:`app.orchestrator.agent.llm_stream._call_llm_stream`,
    just routed through the Codex Responses API.
    """
    payload = _request_payload(model, messages, tool_specs, {"stream": True, **_reasoning_extra(reasoning_effort)})
    headers = _request_headers(access_token, account_id)
    with httpx.Client(timeout=None) as client:
        with client.stream("POST", CODEX_API_ENDPOINT, headers=headers, json=payload) as r:
            if r.status_code >= 400:
                body = r.read().decode(errors="replace")[:500]
                raise RuntimeError(f"Codex {r.status_code}: {body}")

            def cancellable_lines():
                for line in r.iter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    yield line

            yield from _parse_responses_sse(cancellable_lines())


def call_codex_chat(
    model: str,
    prompt,
    tools: list[str] | None,
    tool_registry: dict,
    tool_schemas_by_name: dict,
    on_event: Optional[Callable[[dict], None]],
    call_id: Optional[str],
    access_token: str,
    account_id: Optional[str],
    reasoning_effort: Optional[str] = None,
    **opts,
) -> dict:
    """Node-runtime entry point — mirrors ``runner.llm.call_llm``. Runs the
    agent loop (model call + tool dispatch) until the model emits a turn with
    no tool calls.
    """
    if isinstance(prompt, str):
        messages: list[dict] = [{"role": "user", "content": prompt}]
    else:
        messages = list(prompt)

    tools = tools or []
    tool_schemas = [tool_schemas_by_name[t] for t in tools if t in tool_schemas_by_name]
    tool_calls_made: list[dict] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    streaming = on_event is not None

    def _emit(ev: dict) -> None:
        if on_event is None:
            return
        if call_id is not None and "call_id" not in ev:
            ev = {**ev, "call_id": call_id}
        on_event(ev)

    for round_idx in itertools.count():
        if streaming:
            _emit({"type": "llm_round_started", "round": round_idx})
        assembled_msg: Optional[dict] = None
        round_usage: dict = {}

        if streaming:
            for item in call_codex_stream(
                model, messages, tool_schemas, access_token, account_id,
                reasoning_effort=reasoning_effort,
            ):
                kind = item[0]
                if kind == "text":
                    _emit({
                        "type": "llm_call_chunk",
                        "kind": "content",
                        "round": round_idx,
                        "delta": item[1],
                    })
                elif kind == "thinking":
                    _emit({
                        "type": "llm_call_chunk",
                        "kind": "reasoning",
                        "round": round_idx,
                        "delta": item[1],
                    })
                elif kind == "tool_args":
                    _, tc_idx, tc_name, tc_delta = item
                    _emit({
                        "type": "llm_call_chunk",
                        "kind": "tool_args",
                        "round": round_idx,
                        "tc_index": tc_idx,
                        "tool": tc_name,
                        "delta": tc_delta,
                    })
                elif kind == "done":
                    info = item[1]
                    assembled_msg = info["message"]
                    round_usage = info.get("usage") or {}
                    break
        else:
            # Non-streaming fallback: drain the stream with no callbacks.
            for item in call_codex_stream(
                model, messages, tool_schemas, access_token, account_id,
                reasoning_effort=reasoning_effort,
            ):
                if item[0] == "done":
                    assembled_msg = item[1]["message"]
                    round_usage = item[1].get("usage") or {}
                    break

        if assembled_msg is None:
            assembled_msg = {"role": "assistant", "content": ""}
        total_usage["prompt_tokens"] += round_usage.get("prompt_tokens", 0) or 0
        total_usage["completion_tokens"] += round_usage.get("completion_tokens", 0) or 0

        messages.append(assembled_msg)
        tcs = assembled_msg.get("tool_calls") or []
        if not tcs:
            return {
                "content": assembled_msg.get("content", "") or "",
                "messages": messages,
                "tool_calls_made": tool_calls_made,
                "usage": total_usage,
                # Codex bills against ChatGPT subscription — no per-call cost.
                "cost": 0.0,
            }

        for tc_idx, tc in enumerate(tcs):
            fn_name = tc.get("function", {}).get("name", "")
            try:
                fn_args = json.loads(tc.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            _emit({
                "type": "tool_call_started",
                "tool": fn_name,
                "args": fn_args,
                "via": "llm",
                "tc_index": tc_idx,
                "round": round_idx,
            })
            fn = tool_registry.get(fn_name)
            if fn is None:
                result = {"error": f"unknown tool {fn_name}"}
            else:
                try:
                    result = fn(**fn_args)
                except Exception as e:
                    traceback.print_exc(file=sys.stderr)
                    result = {"error": f"{type(e).__name__}: {e}"}
            tool_calls_made.append({"name": fn_name, "args": fn_args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": json.dumps(result, default=str),
                }
            )
            _emit({
                "type": "tool_call_finished",
                "tool": fn_name,
                "args": fn_args,
                "result": result,
                "via": "llm",
                "tc_index": tc_idx,
                "round": round_idx,
            })
