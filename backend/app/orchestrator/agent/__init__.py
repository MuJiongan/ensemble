"""Orchestrator agent loop — runs one user-message turn and yields events.

The loop calls the LLM, executes any returned tool calls (graph mutations),
appends the results to the conversation, and repeats until the LLM stops
calling tools. There's no turn cap — a runaway loop is a cancel-button
concern, matching the node-runtime ``ctx.call_llm`` model. Each significant
step is yielded as an event dict for the SSE handler to forward to the chat
UI.

Implementation is split across submodules:
  * :mod:`.session`     — per-session turn cancellation registry
  * :mod:`.persistence` — message rows + OpenRouter chat-shape conversion
  * :mod:`.llm_stream`  — OpenRouter SSE call + chunk parsing

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

from app import models
from app.orchestrator import tools as orch_tools
from app.orchestrator.prompt import SYSTEM_PROMPT, graph_state_message

from .session import (
    _TURN_CANCEL_EVENTS,
    _TURN_LOCK,
    _claim_turn,
    _release_turn,
    _signal_cancel,
    _was_superseded,
)
from .persistence import (
    _history_messages,
    _persist_assistant,
    _persist_tool_result,
    _persist_user,
)
from .llm_stream import _call_openrouter_stream, _parse_sse_chunks


__all__ = [
    "DEFAULT_MODEL_FALLBACK",
    "render_history",
    "run_turn",
    # Re-exported for callers (api/orchestrator.py) and tests:
    "_TURN_CANCEL_EVENTS",
    "_TURN_LOCK",
    "_call_openrouter_stream",
    "_claim_turn",
    "_history_messages",
    "_parse_sse_chunks",
    "_release_turn",
    "_signal_cancel",
    "_was_superseded",
]


DEFAULT_MODEL_FALLBACK = "anthropic/claude-opus-4.7"


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


def _resolve_model(db: DbSession) -> str:
    # localStorage (forwarded as a header by the frontend, applied to env in
    # main.middleware) wins; the DB row is only a backwards-compat fallback.
    env_val = os.getenv("DEFAULT_ORCHESTRATOR_MODEL", "")
    if env_val:
        return env_val
    s = db.query(models.Setting).filter_by(key="default_orchestrator_model").first()
    if s and s.value:
        return s.value
    return DEFAULT_MODEL_FALLBACK


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
            messages = (
                [{"role": "system", "content": SYSTEM_PROMPT}]
                + [graph_state_message(db, workflow_id)]
                + history
            )

            # Stream the LLM response, forwarding each text delta to the chat.
            # The final assembled message (with tool_calls if any) lands at
            # the "done" marker; we only persist *once* per round.
            assembled_msg: dict | None = None
            round_usage: dict = {}
            for kind, payload in _call_openrouter_stream(
                model, messages, tool_specs, cancel_event
            ):
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


def render_history(db: DbSession, session_id: str) -> list[dict]:
    """Collapse persisted Messages into chat-panel render units.

    Each user Message → one user bubble. Each assistant Message + its
    immediately-following tool result Messages → one assistant bubble whose
    content interleaves a paragraph block with one tool-card block per call.
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
    for r in rows:
        if r.role == "user":
            bubbles.append({"role": "user", "text": r.content or ""})
        elif r.role == "assistant":
            content: list[dict] = []
            # Re-assemble the visible reasoning text from reasoning_details
            # blocks. Multiple blocks are concatenated with double-newlines so
            # the panel can render them as paragraphs inside one collapsible.
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
                        # Surface the persisted result so the chat panel can
                        # render rich tool cards (e.g. `run_workflow` snapshot
                        # summary) on history reload, not just live streams.
                        "result": tr.get("result") if tr else None,
                    }
                )
            bubble: dict = {"role": "assistant", "content": content}
            if (r.cost or 0) > 0:
                bubble["cost"] = float(r.cost)
            bubbles.append(bubble)
        # skip tool / system rows in user-facing render
    return bubbles
