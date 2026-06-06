"""Streaming entry point for the orchestrator agent.

One round of the orchestrator's model call is routed through the native
protocol layer (:mod:`app.llm`), which picks OpenAI-chat / Anthropic Messages /
Gemini by provider and applies the reasoning variant. The OpenAI-compatible SSE
parser lives in :mod:`app.llm.openai_chat`; it's re-exported here as
``_parse_sse_chunks`` for the orchestrator unit tests.
"""
from __future__ import annotations
import os
import threading
from typing import Any, Iterator

from app.llm.openai_chat import parse_sse_lines


def _parse_sse_chunks(lines: Iterator[str], cancel_event=None) -> Iterator[tuple[str, Any]]:
    """OpenAI-compatible SSE parser, restricted to the orchestrator's 2-tuple
    contract (``text`` / ``thinking`` / ``done``). Tool calls are read from the
    final ``done`` message, so per-arg ``tool_args`` deltas are dropped."""
    for item in parse_sse_lines(lines, cancel_event):
        if item[0] in ("text", "thinking", "done"):
            yield item


def _call_llm_stream(
    model: str,
    messages: list[dict],
    tool_specs: list[dict],
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, Any]]:
    """Stream one orchestrator round through the native protocol layer.

    Picks the protocol (OpenAI-chat / Anthropic Messages / Gemini) for the
    configured provider+model, applies the reasoning variant, and yields
    ``("text"|"thinking"|"done", ...)`` chunks. ``tool_args`` deltas are
    filtered out — the orchestrator loop reads tool calls from the final
    ``done`` message. If ``cancel_event`` is set during streaming the read
    terminates between chunks and the adapter emits a final ``done`` with the
    partial text assembled."""
    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY not set")

    from app.llm import router as llm_router

    plan = llm_router.plan(
        (os.getenv("LLM_PROVIDER_ID") or "").strip(),
        model,
        os.getenv("DEFAULT_ORCHESTRATOR_VARIANT") or None,
        base_url=os.getenv("LLM_BASE_URL", ""),
    )
    for item in plan.stream_round(
        model=model,
        messages=messages,
        tool_schemas=tool_specs,
        base_url=plan.base_url,
        api_key=api_key,
        variant_opts=plan.variant_opts,
        extra_headers=plan.extra_headers,
        model_output_limit=plan.model_output_limit,
        cost=plan.cost,
        streaming=True,
        cancel_event=cancel_event,
    ):
        if item[0] in ("text", "thinking", "done"):
            yield item
