"""Codex (ChatGPT-subscription) LLM caller — OpenAI Responses API.

When the user signs in to ChatGPT, calls go to
``https://chatgpt.com/backend-api/codex/responses`` with the OAuth bearer plus
``ChatGPT-Account-Id``. The endpoint speaks the OpenAI **Responses API**, not
chat completions — the translation and SSE parsing live in the shared
protocol adapter (:mod:`app.llm.openai_responses`); this module layers the
Codex-specific endpoint, OAuth headers, and required-``instructions`` rule
on top.

Two entry points mirroring the existing callers:

* :func:`call_codex_chat`        — node-runtime ``ctx.call_llm`` equivalent.
* :func:`call_codex_stream_orch` — orchestrator-loop equivalent (yields
  parsed chunks the agent loop already understands).
"""
from __future__ import annotations
import itertools
import json
import logging
import os
import sys
import threading
import traceback
from typing import Any, Callable, Iterator, Optional

import httpx

from app import compaction
from app.catalog import models_dev as md
from app.llm.openai_responses import (
    parse_responses_sse,
    to_responses_input,
    to_responses_tools,
)
from app.runner.tools import prepare_tool_result


CODEX_API_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

log = logging.getLogger(__name__)


def _to_responses_input(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
    """Chat-completions ``messages`` → Responses API ``(instructions, input)``,
    with the first user message's text standing in for ``instructions`` when
    no system message exists — Codex requires it to be non-empty, and the
    first user turn is the closest stand-in for the task the model should
    follow. The actual lowering lives in :mod:`app.llm.openai_responses`."""
    return to_responses_input(messages, instructions_fallback=True)


def _parse_responses_sse(lines: Iterator[str]) -> Iterator[tuple]:
    """Parse the Codex Responses API SSE stream into the shared event tuples
    (see :func:`app.llm.openai_responses.parse_responses_sse`)."""
    return parse_responses_sse(lines)


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
        payload["tools"] = to_responses_tools(tool_schemas)
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

    # Compaction limits. The synthetic "codex" catalog entry carries the context
    # window; an unknown model leaves limits at zero and is_overflow() never
    # fires, so the loop runs unchanged.
    provider_id = (os.getenv("LLM_PROVIDER_ID") or "").strip()
    model_obj = md.get_model(provider_id, model) if provider_id else None
    ctx_limit = model_obj.limit.context if model_obj else 0
    out_limit = model_obj.limit.output if model_obj else 0
    in_limit = model_obj.limit.input if model_obj else None
    accepts_images = md.supports_image_input(model_obj)

    def _emit(ev: dict) -> None:
        if on_event is None:
            return
        if call_id is not None and "call_id" not in ev:
            ev = {**ev, "call_id": call_id}
        on_event(ev)

    def _summarize(head: list[dict], prompt: str) -> str:
        """Run one non-tool Responses-API round to summarize ``head``."""
        parts: list[str] = []
        for item in call_codex_stream(
            model, [*head, {"role": "user", "content": prompt}], [],
            access_token, account_id, reasoning_effort=reasoning_effort,
        ):
            if item[0] == "done":
                return (item[1]["message"].get("content") or "").strip()
            if item[0] == "text":
                parts.append(item[1])
        return "".join(parts).strip()

    def _maybe_compact(msgs: list[dict], last_usage: dict) -> None:
        """Prune stale tool outputs, then compact older turns once the live
        token count reaches the model's usable budget. Mutates ``msgs``."""
        if ctx_limit == 0:
            return
        compaction.prune_messages(msgs)
        token_count = (last_usage.get("prompt_tokens") or 0) + (
            last_usage.get("completion_tokens") or 0
        )
        if not compaction.is_overflow(
            token_count=token_count,
            context=ctx_limit,
            output_limit=out_limit,
            input_limit=in_limit,
        ):
            return
        result = compaction.compact_messages(
            msgs,
            summarize=_summarize,
            context=ctx_limit,
            output_limit=out_limit,
            input_limit=in_limit,
        )
        if result is not None:
            msgs[:] = result["messages"]
            _emit({"type": "context_compacted", "summarized": result["summarized"]})

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
            # function_call_output is text-only, so the output text and the
            # recorded result carry a size note; the binary payload rides on
            # the message for _to_responses_input to re-deliver as
            # input_image parts in a follow-up user message.
            recorded, attachments = prepare_tool_result(
                result, tool=fn_name, model=model, accepts_images=accepts_images
            )
            tool_calls_made.append({"name": fn_name, "args": fn_args, "result": recorded})
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": json.dumps(recorded, default=str),
            }
            if attachments:
                tool_msg["attachments"] = attachments
            messages.append(tool_msg)
            _emit({
                "type": "tool_call_finished",
                "tool": fn_name,
                "args": fn_args,
                "result": recorded,
                "via": "llm",
                "tc_index": tc_idx,
                "round": round_idx,
            })

        # Another round follows (tool calls were made) — keep its input within
        # the context window before we get there.
        _maybe_compact(messages, round_usage)
