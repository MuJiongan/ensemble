"""Orchestrator agent loop — runs one user-message turn and yields events.

The loop calls the LLM, executes any returned tool calls (graph mutations),
appends the results to the conversation, and repeats until the LLM stops
calling tools. There's no turn cap — a runaway loop is a cancel-button
concern, matching the node-runtime ``ctx.call_llm`` model. Each significant
step is yielded as an event dict for the SSE handler to forward to the chat
UI.

Implementation is split across submodules:
  * :mod:`.session`     — per-session turn cancellation registry
  * :mod:`.persistence` — message rows + OpenAI-compatible chat-shape conversion
  * :mod:`.llm_stream`  — SSE call + chunk parsing for the configured provider

This module owns the orchestration loop itself plus :func:`render_history`
(history → chat-bubble flattener used by GET /sessions/:id/messages).
"""
from __future__ import annotations
import json
import os
import sys
import traceback
from typing import Iterator

from sqlalchemy.orm import Session as DbSession

from app import compaction, models
from app.orchestrator import tools as orch_tools
from app.orchestrator.prompt import (
    build_system_prompt,
    graph_state_message,
    mcp_tools_message,
)

from .session import (
    _TURN_CANCEL_EVENTS,
    _TURN_LOCK,
    _claim_turn,
    _release_turn,
    _signal_cancel,
    _was_superseded,
)
from .persistence import (
    _active_rows,
    _history_messages,
    _ordered_rows,
    _persist_assistant,
    _persist_compaction,
    _persist_tool_result,
    _persist_user,
    _row_to_message,
)
from .llm_stream import _call_llm_stream, _parse_sse_chunks


__all__ = [
    "render_history",
    "run_turn",
    # Re-exported for callers (api/orchestrator.py) and tests:
    "_TURN_CANCEL_EVENTS",
    "_TURN_LOCK",
    "_call_llm_stream",
    "_claim_turn",
    "_history_messages",
    "_parse_sse_chunks",
    "_release_turn",
    "_signal_cancel",
    "_was_superseded",
]


def _format_args_summary(args: dict) -> str:
    """A short, human-readable summary of a tool call's arguments for the chat
    panel (the full args go to the LLM regardless)."""
    parts: list[str] = []
    for k, v in (args or {}).items():
        if k == "code":
            n = (v or "").count("\n") + 1
            parts.append(f'code=<{n} lines>')
            continue
        if k == "description":
            short = (v or "").strip().splitlines()[0] if v else ""
            if len(short) > 40:
                short = short[:37] + "…"
            parts.append(f'description="{short}"')
            continue
        if isinstance(v, str):
            short = v if len(v) <= 40 else v[:37] + "…"
            parts.append(f'{k}="{short}"')
        elif isinstance(v, (list, tuple)):
            if not v:
                parts.append(f"{k}=[]")
            else:
                parts.append(f"{k}=[{len(v)}]")
        elif isinstance(v, dict):
            parts.append(f"{k}={{...}}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def _resolve_llm_stream(
    db: DbSession,
    model: str,
    messages: list[dict],
    tool_specs: list[dict],
    cancel_event,
):
    """Pick the right LLM transport for this turn.

    OAuth-backed presets (``codex``, ``xai``) come in via ``LLM_PROVIDER_ID``
    on the request — we fetch the active credential, refresh it if needed,
    and route through the right module. xAI uses the same chat-completions
    transport as the API-key path (its env is rewritten in place); Codex
    uses the Responses API.
    """
    from app.auth.resolve import current_provider_id, resolve

    pid = current_provider_id()
    if pid == "codex":
        creds = resolve("codex", db)
        if creds is None:
            raise RuntimeError("Codex/ChatGPT sign-in required (no active credential)")
        from app.auth.codex_api import call_codex_stream
        from app.llm import router as llm_router
        effort = llm_router.plan(
            "codex", model, os.getenv("DEFAULT_ORCHESTRATOR_VARIANT") or None
        ).variant_opts.get("reasoningEffort")
        codex_stream = call_codex_stream(
            model, messages, tool_specs, creds.access_token, creds.account_id, cancel_event,
            reasoning_effort=effort,
        )
        # The Codex SSE parser also emits ("tool_args", idx, name, delta)
        # 4-tuples so the node-runtime UI can render streaming tool-arg
        # deltas. The orchestrator loop unpacks strict 2-tuples and doesn't
        # need that granularity — tool calls land in the final ``done``
        # message anyway. Filter them out here.
        return (item for item in codex_stream if item[0] in ("text", "thinking", "done"))
    if pid == "xai":
        creds = resolve("xai", db)
        if creds is None:
            raise RuntimeError("xAI sign-in required (no active credential)")
        # xAI uses the standard chat-completions transport; we just need to
        # point the env at xAI and substitute the OAuth bearer for this call.
        os.environ["LLM_API_KEY"] = creds.access_token
        os.environ["LLM_BASE_URL"] = "https://api.x.ai/v1"

    return _call_llm_stream(model, messages, tool_specs, cancel_event)


def _resolve_model(db: DbSession) -> str:
    # localStorage (forwarded as a header by the frontend, applied to env in
    # main.middleware) wins; the DB row is only a backwards-compat fallback.
    # No hardcoded default — an unset model returns "" and the caller surfaces a
    # "configure it in Settings" error rather than guessing one.
    env_val = os.getenv("DEFAULT_ORCHESTRATOR_MODEL", "")
    if env_val:
        return env_val
    s = db.query(models.Setting).filter_by(key="default_orchestrator_model").first()
    if s and s.value:
        return s.value
    return ""


def _summarize_orch(
    db: DbSession, model: str, head: list[dict], prompt: str, cancel_event=None
) -> str:
    """Run one non-tool model round to summarize ``head`` into an anchor."""
    parts: list[str] = []
    for kind, payload in _resolve_llm_stream(
        db, model, [*head, {"role": "user", "content": prompt}], [], cancel_event
    ):
        if kind == "text":
            parts.append(payload)
        elif kind == "done":
            content = (payload.get("message") or {}).get("content") or ""
            return content.strip() or "".join(parts).strip()
    return "".join(parts).strip()


def _compact_session(
    db: DbSession, session_id: str, model: str, model_obj, cancel_event=None
) -> bool:
    """Summarize the older turns of this session and persist a compaction
    anchor so future turns replay less history. Returns True if compaction
    actually happened (head large enough + non-empty summary)."""
    active, prev_summary = _active_rows(_ordered_rows(db, session_id))
    view: list[dict] = []
    view_rows: list = []
    for r in active:
        msg = _row_to_message(r)
        if msg is None:
            continue
        view.append(msg)
        view_rows.append(r)

    tail_start = compaction.select_tail(
        view,
        context=model_obj.limit.context,
        output_limit=model_obj.limit.output,
        input_limit=model_obj.limit.input,
    )
    head = view[:tail_start]
    if len(head) < 2:
        return False

    head_msgs = ([compaction.summary_message(prev_summary)] if prev_summary else []) + head
    prompt = compaction.build_prompt(previous_summary=prev_summary)
    summary = _summarize_orch(db, model, head_msgs, prompt, cancel_event)
    if not summary:
        return False

    tail_start_id = view_rows[tail_start].id if tail_start < len(view_rows) else None
    _persist_compaction(db, session_id, summary, tail_start_id)
    return True


def _maybe_compact(
    db: DbSession, session_id: str, model: str, usage: dict, cancel_event=None
) -> bool:
    """Compact the session when the last round's token count has reached the
    model's usable context budget. No-op for models with an unknown context
    window (catalog miss)."""
    import os

    from app.catalog import models_dev as md

    provider_id = (os.getenv("LLM_PROVIDER_ID") or "").strip()
    model_obj = md.get_model(provider_id, model) if provider_id else None
    if not model_obj or model_obj.limit.context == 0:
        return False
    token_count = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
    if not compaction.is_overflow(
        token_count=token_count,
        context=model_obj.limit.context,
        output_limit=model_obj.limit.output,
        input_limit=model_obj.limit.input,
    ):
        return False
    return _compact_session(db, session_id, model, model_obj, cancel_event)


def run_turn(db: DbSession, session_id: str, user_text: str) -> Iterator[dict]:
    """Run one user-message turn end-to-end. Yields event dicts:

      {kind: "user_message",            ...}    — echo of the persisted user msg
      {kind: "assistant_thinking_chunk", text}  — reasoning-token delta
      {kind: "assistant_text_chunk",     text}  — visible-content delta
      {kind: "tool_call_start", tool, args, args_summary}
      {kind: "tool_call_end",   tool, args_summary, status, result}
      {kind: "error", message}
      {kind: "done"}
    """
    sess = db.get(models.Session, session_id)
    if not sess:
        yield {"kind": "error", "message": f"session {session_id} not found"}
        return
    workflow_id = sess.workflow_id

    # 1) persist + announce the user message
    user_msg = _persist_user(db, session_id, user_text)
    yield {"kind": "user_message", "id": user_msg.id, "text": user_text}

    # Claim this session's turn slot. If a prior turn was running, this signals
    # it to wind down (the prior generator will bail at its next checkpoint).
    cancel_event = _claim_turn(session_id)

    model = _resolve_model(db)
    if not model:
        yield {
            "kind": "error",
            "message": "No orchestrator model configured. Set a default orchestrator model in Settings.",
        }
        yield {"kind": "done"}
        return
    tool_specs = orch_tools.llm_tool_specs()

    def _cancellation_events():
        """Yield the right tail-events when a cancel is observed: a noisy
        error banner if we were superseded by a newer message, nothing if the
        user just clicked cancel — followed always by ``done``."""
        if _was_superseded(session_id, cancel_event):
            yield {"kind": "error", "message": "superseded by a newer message"}
        yield {"kind": "done"}

    try:
        while True:
            # Bail between LLM turns.
            if cancel_event.is_set():
                yield from _cancellation_events()
                return

            # Refresh history every turn — including a fresh system snapshot of
            # the graph as it stands. We DON'T persist these system messages.
            history = _history_messages(db, session_id)
            mcp_msg = mcp_tools_message()
            messages = (
                [{"role": "system", "content": build_system_prompt()}]
                + [graph_state_message(db, workflow_id)]
                + ([mcp_msg] if mcp_msg else [])
                + history
            )

            # Stream the LLM response, forwarding each text delta to the chat.
            # The final assembled message (with tool_calls if any) lands at
            # the "done" marker; we only persist *once* per round.
            assembled_msg: dict | None = None
            round_usage: dict = {}
            stream = _resolve_llm_stream(db, model, messages, tool_specs, cancel_event)
            for kind, payload in stream:
                if kind == "text":
                    yield {"kind": "assistant_text_chunk", "text": payload}
                elif kind == "thinking":
                    yield {"kind": "assistant_thinking_chunk", "text": payload}
                elif kind == "done":
                    assembled_msg = payload.get("message") or {}
                    round_usage = payload.get("usage") or {}
                    break

            # Cancelled mid-stream: don't persist a partial assistant message
            # (especially one with half-formed tool_calls — that would corrupt
            # subsequent history). Just exit cleanly. The user's message is
            # still in history; on the next turn the LLM picks up fresh.
            if cancel_event.is_set():
                yield from _cancellation_events()
                return

            if assembled_msg is None:
                # Empty stream — synthesise an empty assistant turn so we
                # exit cleanly rather than looping.
                assembled_msg = {"role": "assistant", "content": ""}

            text = assembled_msg.get("content") or ""
            tcs = assembled_msg.get("tool_calls") or []
            rds = assembled_msg.get("reasoning_details") or []
            try:
                round_cost = float(round_usage.get("cost") or 0.0)
            except (TypeError, ValueError):
                round_cost = 0.0

            # Persist the assistant turn now so subsequent tool messages can
            # reference its tool_calls (OpenRouter wants the assistant message
            # with tool_calls to appear before its tool results). The
            # reasoning_details array is preserved verbatim so the next turn
            # can echo it back — Anthropic enforces ordering of these blocks.
            _persist_assistant(
                db,
                session_id,
                text,
                tcs if tcs else None,
                reasoning_details=rds if rds else None,
                cost=round_cost,
            )

            # Surface the round's $ cost so the chat panel can show a
            # running total on the streaming assistant bubble. Only emit
            # when OpenRouter actually returned a cost (free models, local
            # providers, or transient missing-usage replies stay quiet).
            if round_cost > 0:
                yield {"kind": "assistant_cost", "cost": round_cost}

            if not tcs:
                yield {"kind": "done"}
                return

            # Execute each tool call sequentially; persist its result; emit
            # start/end events. We check the cancel event between calls — once
            # the assistant message is persisted with its tool_calls, we must
            # also persist a tool result for *every* one of them, otherwise the
            # next turn's history would be malformed (OpenRouter rejects an
            # assistant tool_call without a paired tool message). So once
            # cancelled, we synthesise cancellation results for the remaining
            # tool calls and exit cleanly.
            cancelled_mid_turn = False
            for tc in tcs:
                tc_id = tc.get("id") or ""
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                args_summary = _format_args_summary(args)

                if cancelled_mid_turn or cancel_event.is_set():
                    cancelled_mid_turn = True
                    result = {"error": "cancelled — orchestrator turn was stopped"}
                    _persist_tool_result(db, session_id, tc_id, name, result)
                    yield {
                        "kind": "tool_call_end",
                        "tool": name,
                        "args": args_summary,
                        "status": "err",
                        "result": result,
                    }
                    continue

                yield {
                    "kind": "tool_call_start",
                    "tool": name,
                    "args": args_summary,
                    "args_full": args,
                }

                result = orch_tools.execute(db, workflow_id, name, args)

                # `run_workflow` returns immediately with `{run_id, status:"running"}`
                # — the actual run executes in a background thread. Emit a
                # `run_started` event so the frontend can attach its run panel
                # to the live WS (same code path the manual Run button uses),
                # then block here until the run finishes and replace `result`
                # with the materialised final state before the LLM sees it.
                if (
                    name == "run_workflow"
                    and isinstance(result, dict)
                    and result.get("status") == "running"
                    and result.get("run_id")
                ):
                    run_id = result["run_id"]
                    yield {
                        "kind": "run_started",
                        "run_id": run_id,
                        "workflow_id": workflow_id,
                    }
                    result = orch_tools.wait_for_run(
                        db, workflow_id, run_id, cancel_event=cancel_event
                    )

                # `run_workflow` (and a few others) always include an `error`
                # key, set to None on success — so check the *value*, not the
                # key's presence.
                ok = not (result or {}).get("error")

                _persist_tool_result(db, session_id, tc_id, name, result)

                yield {
                    "kind": "tool_call_end",
                    "tool": name,
                    "args": args_summary,
                    "status": "ok" if ok else "err",
                    "result": result,
                }

            if cancelled_mid_turn:
                yield from _cancellation_events()
                return

            # The loop will run another round (tool calls were made) — keep its
            # input within the context window before we get there. Emit an event
            # so the chat panel can show that history was compacted.
            if _maybe_compact(db, session_id, model, round_usage, cancel_event):
                yield {"kind": "context_compacted"}
    except Exception as e:  # pragma: no cover — defensive
        # The chat panel only sees the brief error string; surface the full
        # traceback to stderr so server logs retain it for diagnostics.
        traceback.print_exc(file=sys.stderr)
        yield {"kind": "error", "message": f"{type(e).__name__}: {e}"}
        yield {"kind": "done"}
    finally:
        _release_turn(session_id, cancel_event)


# ---------------------------------------------------------------------------
# history → chat-bubble flattener (used by GET /sessions/:id/messages)
# ---------------------------------------------------------------------------


def _assistant_content_blocks(
    r: models.Message, tool_results: dict[str, dict]
) -> list[dict]:
    """Render one persisted assistant row into chat-panel content blocks."""
    content: list[dict] = []
    # Re-assemble the visible reasoning text from reasoning_details blocks.
    # Multiple blocks are concatenated with double-newlines so the panel can
    # render them as paragraphs inside one collapsible.
    rds = r.reasoning_details or []
    thinking_text = "\n\n".join(
        (b.get("text") or "").strip()
        for b in rds
        if isinstance(b, dict) and (b.get("text") or "").strip()
    )
    if thinking_text:
        content.append({"t": "thinking", "text": thinking_text})
    if r.content:
        content.append({"t": "p", "text": r.content})
    for tc in (r.tool_calls or []):
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        summary = _format_args_summary(args)
        tr = tool_results.get(tc_id)
        ok = bool(tr) and not (tr.get("result") or {}).get("error")
        content.append(
            {
                "t": "tool",
                "tool": name,
                "args": summary,
                "status": "ok" if ok else ("err" if tr else "pending"),
                # Surface the persisted result so the chat panel can render rich
                # tool cards (e.g. `run_workflow` snapshot summary) on history
                # reload, not just live streams.
                "result": tr.get("result") if tr else None,
            }
        )
    return content


def _flush_assistant_bubble(content: list[dict], cost: float) -> dict | None:
    if not content:
        return None
    bubble: dict = {"role": "assistant", "content": content}
    if cost > 0:
        bubble["cost"] = cost
    return bubble


def render_history(db: DbSession, session_id: str) -> list[dict]:
    """Collapse persisted Messages into chat-panel render units.

    Each user Message → one user bubble. All assistant Messages belonging to
    the same user turn (every LLM round until the next user message) merge into
    one assistant bubble whose content interleaves paragraphs, thinking traces,
    and tool-card blocks — matching how the live SSE stream renders a turn.
    """
    rows = (
        db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.ts.asc(), models.Message.id.asc())
        .all()
    )

    # Index tool results by tool_call_id for quick lookup.
    tool_results: dict[str, dict] = {}
    for r in rows:
        if r.role == "tool" and r.tool_call_id:
            try:
                payload = json.loads(r.content) if r.content else {}
            except json.JSONDecodeError:
                payload = {"raw": r.content}
            tool_results[r.tool_call_id] = {"name": r.name, "result": payload}

    bubbles: list[dict] = []
    turn_content: list[dict] = []
    turn_cost = 0.0
    for r in rows:
        if r.role == "user":
            if bubble := _flush_assistant_bubble(turn_content, turn_cost):
                bubbles.append(bubble)
            turn_content = []
            turn_cost = 0.0
            bubbles.append({"role": "user", "text": r.content or ""})
        elif r.role == "assistant":
            turn_content.extend(_assistant_content_blocks(r, tool_results))
            turn_cost += float(r.cost or 0)
        # skip tool / system rows in user-facing render
    if bubble := _flush_assistant_bubble(turn_content, turn_cost):
        bubbles.append(bubble)
    return bubbles
