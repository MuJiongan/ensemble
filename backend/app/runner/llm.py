"""call_llm — provider-agnostic LLM call with an optional tool-calling loop.

The provider + model + reasoning variant are configured by the user in Settings
and forwarded as headers, which the FastAPI middleware maps to env vars
(``LLM_PROVIDER_ID`` / ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``DEFAULT_NODE_VARIANT``).
Each round is dispatched through the native protocol layer (:mod:`app.llm`),
which picks OpenAI-chat / Anthropic Messages / Gemini by provider and applies
the catalog-computed reasoning variant. ChatGPT-subscription (Codex) uses the
Responses API and is dispatched separately at the top.

When an ``on_event`` callback is provided, the call streams the response and
emits per-token events tagged with ``call_id`` so the run panel can render
live content/reasoning/tool-arg deltas. Multiple ``ctx.call_llm`` invocations
within the same node are disambiguated by their ``call_id``. Without
``on_event`` the adapters still stream internally and assemble the final
message — the caller just doesn't see deltas.
"""
from __future__ import annotations
import itertools
import json
import sys
import traceback
import os
from typing import Callable

from app import compaction
from app.catalog import models_dev as md
from app.runner.tools import REGISTRY, TOOL_SCHEMAS


def call_llm(
    model: str,
    prompt,
    tools: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
    call_id: str | None = None,
    **opts,
) -> dict:
    """
    Call an LLM over an OpenAI-compatible chat completions API.

    Args:
        model:    model id, as expected by the configured provider (e.g.
                  "anthropic/claude-sonnet-4.5" on OpenRouter, "gpt-4o" on OpenAI).
        prompt:   str or list of messages [{role, content}].
        tools:    list of tool names exposed to the LLM (subset of REGISTRY keys).
        on_event: optional callback for streaming events. When provided, the
                  call streams from the provider and emits ``llm_call_chunk``
                  and ``tool_call_started``/``tool_call_finished`` events
                  tagged with ``call_id``.
        call_id:  unique id for this call, included on every emitted event so
                  concurrent calls within one node can be disambiguated.
        **opts:   forwarded as additional fields in the request body.

    Returns:
        {content, messages, tool_calls_made, usage, cost}
    """
    # Subscription-OAuth provider dispatch — the runner subprocess gets an
    # OAuth bearer + (for Codex) account id pre-resolved in env at spawn time.
    # Codex uses the Responses API, not chat completions, so the call shape
    # diverges enough to live in its own module.
    if (os.getenv("LLM_PROVIDER_ID") or "").strip() == "codex":
        from app.auth.codex_api import call_codex_chat
        from app.llm import router as llm_router
        effort = llm_router.plan(
            "codex", model, os.getenv("DEFAULT_NODE_VARIANT") or None
        ).variant_opts.get("reasoningEffort")
        return call_codex_chat(
            model=model,
            prompt=prompt,
            tools=tools,
            tool_registry=REGISTRY,
            tool_schemas_by_name=TOOL_SCHEMAS,
            on_event=on_event,
            call_id=call_id,
            access_token=os.getenv("LLM_API_KEY", ""),
            account_id=os.getenv("LLM_ACCOUNT_ID") or None,
            reasoning_effort=effort,
            **opts,
        )

    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY not set")

    # Pick the native protocol (OpenAI-chat / Anthropic Messages / Gemini) for
    # this provider+model and resolve its endpoint + reasoning variant. provider
    # id + variant arrive via env (set by the runner spawner). Degrades to the
    # OpenAI-chat default if the catalog can't be resolved.
    from app.llm import router as llm_router

    exec_plan = llm_router.plan(
        (os.getenv("LLM_PROVIDER_ID") or "").strip(),
        model,
        os.getenv("DEFAULT_NODE_VARIANT") or None,
        base_url=os.getenv("LLM_BASE_URL", ""),
    )

    if isinstance(prompt, str):
        messages: list[dict] = [{"role": "user", "content": prompt}]
    else:
        messages = list(prompt)

    tools = tools or []
    tool_schemas = [TOOL_SCHEMAS[t] for t in tools if t in TOOL_SCHEMAS]

    # Model limits drive compaction. Unknown model (catalog miss) → limits stay
    # zero and is_overflow() never fires, so a long node loop runs unchanged.
    provider_id = (os.getenv("LLM_PROVIDER_ID") or "").strip()
    model_obj = md.get_model(provider_id, model) if provider_id else None
    ctx_limit = model_obj.limit.context if model_obj else 0
    out_limit = model_obj.limit.output if model_obj else 0
    in_limit = model_obj.limit.input if model_obj else None

    def _summarize(head: list[dict], prompt: str) -> str:
        """Run one non-tool model round to summarize ``head`` into an anchor."""
        parts: list[str] = []
        for item in exec_plan.stream_round(
            model=model,
            messages=[*head, {"role": "user", "content": prompt}],
            tool_schemas=[],
            base_url=exec_plan.base_url,
            api_key=api_key,
            variant_opts=exec_plan.variant_opts,
            extra_headers=exec_plan.extra_headers,
            model_output_limit=exec_plan.model_output_limit,
            cost=exec_plan.cost,
            streaming=False,
        ):
            if item[0] == "done":
                return (item[1]["message"].get("content") or "").strip()
            if item[0] == "text":
                parts.append(item[1])
        return "".join(parts).strip()

    def _maybe_compact(msgs: list[dict], last_usage: dict) -> None:
        """Prune stale tool outputs, then compact older turns if the live token
        count has reached the model's usable budget. Mutates ``msgs`` in place.
        """
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

    tool_calls_made: list[dict] = []
    total_cost = 0.0
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    streaming = on_event is not None

    def _emit(ev: dict) -> None:
        if on_event is None:
            return
        if call_id is not None and "call_id" not in ev:
            ev = {**ev, "call_id": call_id}
        on_event(ev)

    # No turn cap — the agent loop runs until the LLM produces a final message
    # with no tool calls. Node code that hangs is the user's cancel button to
    # address; matches the orchestrator runtime model (see app/runner/child.py
    # for the SIGTERM → KeyboardInterrupt path).
    for round_idx in itertools.count():
        if streaming:
            _emit({"type": "llm_round_started", "round": round_idx})
        assembled_msg: dict | None = None
        round_usage: dict = {}
        for item in exec_plan.stream_round(
            model=model,
            messages=messages,
            tool_schemas=tool_schemas,
            base_url=exec_plan.base_url,
            api_key=api_key,
            variant_opts=exec_plan.variant_opts,
            extra_headers=exec_plan.extra_headers,
            model_output_limit=exec_plan.model_output_limit,
            cost=exec_plan.cost,
            streaming=streaming,
            extra_body=opts,
        ):
            kind = item[0]
            if kind == "text":
                _emit({"type": "llm_call_chunk", "kind": "content", "round": round_idx, "delta": item[1]})
            elif kind == "thinking":
                _emit({"type": "llm_call_chunk", "kind": "reasoning", "round": round_idx, "delta": item[1]})
            elif kind == "tool_args":
                _, tc_idx, tc_name, tc_delta = item
                _emit({
                    "type": "llm_call_chunk", "kind": "tool_args", "round": round_idx,
                    "tc_index": tc_idx, "tool": tc_name, "delta": tc_delta,
                })
            elif kind == "done":
                assembled_msg = item[1]["message"]
                round_usage = item[1].get("usage") or {}
                break
        if assembled_msg is None:
            assembled_msg = {"role": "assistant", "content": ""}
        usage = round_usage
        msg = assembled_msg

        total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0) or 0
        cost = (usage.get("cost") or 0.0) if isinstance(usage, dict) else 0.0
        total_cost += float(cost or 0.0)

        messages.append(msg)
        tcs = msg.get("tool_calls") or []
        if not tcs:
            return {
                "content": msg.get("content", "") or "",
                "messages": messages,
                "tool_calls_made": tool_calls_made,
                "usage": total_usage,
                "cost": total_cost,
            }

        for tc_idx, tc in enumerate(tcs):
            fn_name = tc.get("function", {}).get("name", "")
            try:
                fn_args = json.loads(tc.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            _emit(
                {
                    "type": "tool_call_started",
                    "tool": fn_name,
                    "args": fn_args,
                    "via": "llm",
                    "tc_index": tc_idx,
                    "round": round_idx,
                }
            )
            fn = REGISTRY.get(fn_name)
            if fn is None:
                result = {"error": f"unknown tool {fn_name}"}
            else:
                try:
                    result = fn(**fn_args)
                except Exception as e:
                    # Surface the full traceback to stderr so the runner
                    # subprocess parent can capture it for diagnostics —
                    # the LLM only sees the brief error string.
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
            _emit(
                {
                    "type": "tool_call_finished",
                    "tool": fn_name,
                    "args": fn_args,
                    "result": result,
                    "via": "llm",
                    "tc_index": tc_idx,
                    "round": round_idx,
                }
            )

        # The loop will continue with another round — keep its input within the
        # context window before we get there.
        _maybe_compact(messages, usage)
