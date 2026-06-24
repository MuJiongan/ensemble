"""Continue-chat turn lifecycle.

Runs one turn of a continued ``agent`` conversation: spawns the
``app.runner.chat_child`` subprocess (same MCP/credential plumbing a run uses),
streams its events through the in-memory pub/sub keyed by a transient
``turn_id``, and persists the grown conversation back onto the ``CallChat`` row
when the turn completes.

Mirrors ``runner.service`` / ``runner.runner`` but for a single ``agent``
instead of a whole graph.
"""
from __future__ import annotations
import json
import shutil
import tempfile
import threading
import time
from datetime import datetime
from typing import Any

from app.db import SessionLocal
from app import models
from app.runner import events as ev_mod
from app.runner.ctx import _strip_message_attachments
from app.runner.runner import drive_child_subprocess


# The grown transcript is rewritten in full on every successful turn, so cap it:
# a long continuation with large tool outputs would otherwise grow the CallChat
# row without bound. Larger than the seed budget (a continuation is *meant* to
# accumulate), but still finite. Trimming happens at user-message boundaries so
# an assistant tool_calls message is never split from its tool results.
_TRANSCRIPT_BUDGET_BYTES = 4 * 1024 * 1024


def _serialized_len(messages: list) -> int:
    try:
        return len(json.dumps(messages, default=str))
    except (TypeError, ValueError):
        return _TRANSCRIPT_BUDGET_BYTES + 1


def _normalize_head(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure the transcript head is ``system*`` then a user message.

    A CallChat seed is the recorded ``ctx.agent`` conversation verbatim, so a
    node that called ``agent(prompt=[{"role": "tool", ...}, ...])`` (or a
    leading assistant/tool_calls message) yields a head that strict providers
    reject when re-seeded — a leading ``tool`` becomes an orphaned tool_result,
    and a leading ``assistant`` makes the first message non-user. Splice out any
    leading non-system run that sits before the first user message so the
    re-seeded conversation always starts cleanly. If there is no user message at
    all, keep only the leading system run (better an empty seed than a 400)."""
    msgs = messages or []
    n = len(msgs)
    i = 0
    while i < n and isinstance(msgs[i], dict) and msgs[i].get("role") == "system":
        i += 1
    first_user = next(
        (j for j in range(i, n)
         if isinstance(msgs[j], dict) and msgs[j].get("role") == "user"),
        None,
    )
    if first_user is None:
        return msgs[:i]
    if first_user != i:
        return msgs[:i] + msgs[first_user:]
    return msgs


def _merge_consecutive_users(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold runs of consecutive user messages into one.

    A turn that errored/cancelled leaves its just-sent user message in the row
    with no assistant reply (kept so the user can retry); the next send appends
    another user message. Two user turns in a row waste tokens, confuse the
    model, and hard-400 on providers that require strict role alternation
    (Anthropic). Merge them (only string content) before they ever reach the
    provider or the persisted row."""
    out: list[dict[str, Any]] = []
    for m in messages or []:
        if (
            out
            and isinstance(m, dict)
            and isinstance(out[-1], dict)
            and out[-1].get("role") == "user"
            and m.get("role") == "user"
            and isinstance(out[-1].get("content"), str)
            and isinstance(m.get("content"), str)
        ):
            out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(m)
    return out


def prepare_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize a transcript so it is safe to persist AND to re-seed into
    ``agent``: drop attachment base64, normalize the head to ``system*``-then-
    user, and merge consecutive user turns. Idempotent."""
    return _merge_consecutive_users(
        _normalize_head(_strip_message_attachments(messages or []))
    )


def cap_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize then trim the oldest whole turns so a transcript stays under
    budget. Used both when persisting a grown continuation and when seeding a
    chat from a recorded ``agent`` transcript (which is itself stored
    uncapped). User messages delimit turns; the head is normalized to a
    system-only prefix first (so trimming can never orphan a leading tool/
    assistant message), and we never cut inside a turn.

    Known limitation: when a transcript exceeds the budget the oldest turns
    (which can include the first user instruction) are dropped silently. A live
    continuation session still shows them, so a reload of a >4 MB chat can lose
    earlier turns. The threshold is high enough that normal chats never hit it;
    we accept the divergence rather than reconcile live bubbles to the cap."""
    messages = prepare_transcript(messages)
    if _serialized_len(messages) <= _TRANSCRIPT_BUDGET_BYTES:
        return messages
    user_idx = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    # A single turn can't be trimmed at a boundary without corrupting tool
    # pairing — persist it oversized rather than risk a malformed transcript.
    if len(user_idx) < 2:
        return messages
    # _normalize_head guarantees everything before the first user message is a
    # system message, so this prefix is system-only.
    prefix = messages[: user_idx[0]]
    # Drop progressively more of the oldest turns; keep the first candidate that
    # fits (i.e. the most history we can afford).
    for cut in range(1, len(user_idx)):
        candidate = prefix + messages[user_idx[cut]:]
        if _serialized_len(candidate) <= _TRANSCRIPT_BUDGET_BYTES:
            return candidate
    # Even the latest turn alone is over budget — keep it (beats an empty chat).
    return prefix + messages[user_idx[-1]:]


# Turns are one-shot, but a WebSocket can attach a beat after a fast turn
# already finished (e.g. an instant spawn failure). If we freed the turn's event
# state the instant the subprocess loop ended, that late subscriber would miss
# the real terminal and see a generic "turn not found". So defer the discard: a
# finished turn's state lingers for a short grace window and is swept lazily when
# the next turn starts. No per-turn timer threads; bounded by the number of
# continuations (the last turn of each lingers until the next turn or process
# exit, which is benign).
_TURN_GC_TTL = 30.0
_finished_turns: list[tuple[str, float]] = []
_finished_lock = threading.Lock()


def _mark_turn_finished(turn_id: str) -> None:
    with _finished_lock:
        _finished_turns.append((turn_id, time.monotonic()))


def _gc_finished_turns() -> None:
    now = time.monotonic()
    with _finished_lock:
        expired = [t for t, ts in _finished_turns if now - ts >= _TURN_GC_TTL]
        _finished_turns[:] = [
            (t, ts) for t, ts in _finished_turns if now - ts < _TURN_GC_TTL
        ]
    for t in expired:
        ev_mod.discard(t)


def start_chat_turn(
    turn_id: str,
    chat_id: str,
    messages: list[dict[str, Any]],
    tools: list[str],
    model: str,
    child_env: dict[str, str],
) -> None:
    """Begin a chat turn in the background. Returns immediately.

    Pre-creates the turn's event state so a WebSocket client subscribing
    immediately after this call doesn't race the subprocess spawn. ``child_env``
    is snapshotted by the caller (in the request, while the settings headers are
    applied to process env) so the turn isn't subject to env mutation by a later
    request.
    """
    _gc_finished_turns()  # sweep event state of turns that finished a while ago
    ev_mod.get_or_create(turn_id)
    threading.Thread(
        target=_run_turn,
        args=(turn_id, chat_id, messages, tools, model, child_env),
        daemon=True,
    ).start()


def cancel(turn_id: str) -> bool:
    """SIGTERM the turn's subprocess, if any. Idempotent."""
    return ev_mod.cancel(turn_id)


def subscribe(turn_id: str):
    """Async generator yielding backlog + live events for a turn."""
    return ev_mod.subscribe(turn_id)


def has_state(turn_id: str) -> bool:
    return ev_mod.get(turn_id) is not None


def _run_turn(
    turn_id: str,
    chat_id: str,
    messages: list[dict[str, Any]],
    tools: list[str],
    model: str,
    child_env: dict[str, str],
) -> None:
    workdir = tempfile.mkdtemp(prefix="wfchat-")
    try:
        payload = {
            "messages": messages,
            "tools": tools,
            "model": model,
            "workdir": workdir,
            "env": child_env,
        }

        def _terminal(status: str, error: str | None) -> dict:
            # On crash/cancel the conversation didn't grow — keep the seed so
            # the user keeps their just-sent message and can retry.
            return {
                "type": "run_finished",
                "status": status,
                "error": error,
                "messages": messages,
                "usage": {},
                "cost": 0.0,
            }

        terminal = drive_child_subprocess(
            turn_id, "app.runner.chat_child", payload, _terminal
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Persist the grown conversation only on success. On error/cancel the row
    # keeps the user's just-sent message (persisted by the turn endpoint), so
    # they can retry without losing it.
    if terminal and terminal.get("status") == "success" and terminal.get("messages"):
        _persist_messages(chat_id, terminal["messages"])

    # Defer freeing the turn's event state rather than discarding it the instant
    # the subprocess loop ends. A WebSocket that attaches a beat after a fast
    # turn finishes (e.g. an instant spawn failure) can then still replay the
    # real terminal from the backlog instead of getting a generic "turn not
    # found". The state is swept on a later turn (see _gc_finished_turns).
    _mark_turn_finished(turn_id)


def _persist_messages(chat_id: str, messages: list[dict[str, Any]]) -> None:
    db = SessionLocal()
    try:
        chat = db.get(models.CallChat, chat_id)
        if chat is not None:
            chat.messages = cap_transcript(messages)
            chat.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
