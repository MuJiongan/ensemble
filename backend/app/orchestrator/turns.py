"""Durable in-process orchestrator turn runner.

The chat API streams orchestrator events to the browser, but the work must not
belong to that browser connection. This module starts each turn on a background
thread, keeps a replayable event backlog, and lets one or more SSE subscribers
attach, disconnect, and reattach while the turn continues.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator

from app.db import SessionLocal
from app.orchestrator import agent


_TURN_GC_TTL = 10 * 60.0

_ENV_KEYS = (
    "LLM_PROVIDER_ID",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "DEFAULT_ORCHESTRATOR_MODEL",
    "DEFAULT_ORCHESTRATOR_VARIANT",
    "NODE_PROVIDER_ID",
    "NODE_API_KEY",
    "NODE_BASE_URL",
    "DEFAULT_NODE_MODEL",
    "DEFAULT_NODE_VARIANT",
    "MCP_SERVERS",
    "ORCHESTRATOR_CUSTOM_INSTRUCTIONS",
    "PARALLEL_API_KEY",
)


@dataclass
class TurnState:
    turn_id: str
    session_id: str
    events: list[dict] = field(default_factory=list)
    finished: bool = False
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    changed: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)


_REGISTRY_LOCK = threading.Lock()
_TURNS: dict[str, TurnState] = {}
_ACTIVE_BY_SESSION: dict[str, str] = {}


def _snapshot_env() -> dict[str, str | None]:
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _apply_env(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _gc_finished_turns() -> None:
    now = time.monotonic()
    with _REGISTRY_LOCK:
        expired = [
            turn_id
            for turn_id, state in _TURNS.items()
            if state.finished and state.finished_at is not None and now - state.finished_at >= _TURN_GC_TTL
        ]
        for turn_id in expired:
            _TURNS.pop(turn_id, None)


def _get(turn_id: str) -> TurnState | None:
    with _REGISTRY_LOCK:
        return _TURNS.get(turn_id)


def active_turn_id(session_id: str) -> str | None:
    with _REGISTRY_LOCK:
        turn_id = _ACTIVE_BY_SESSION.get(session_id)
        if not turn_id:
            return None
        state = _TURNS.get(turn_id)
        if not state or state.finished:
            return None
        return turn_id


def is_active(session_id: str) -> bool:
    return active_turn_id(session_id) is not None


def turn_belongs_to_session(turn_id: str, session_id: str) -> bool:
    state = _get(turn_id)
    return bool(state and state.session_id == session_id)


def start_turn(
    session_id: str,
    user_text: str,
    attachments: list[dict] | None = None,
    *,
    auto_user: bool = False,
) -> str:
    """Start an orchestrator turn in the background and return its turn id."""
    _gc_finished_turns()
    turn_id = "orch-" + uuid.uuid4().hex[:12]
    state = TurnState(turn_id=turn_id, session_id=session_id)
    env_snapshot = _snapshot_env()

    # Pre-claim before exposing the turn id. A new turn supersedes any old turn
    # for the same session immediately, even if the old client stream vanished.
    cancel_event = agent._claim_turn(session_id)
    with _REGISTRY_LOCK:
        _TURNS[turn_id] = state
        _ACTIVE_BY_SESSION[session_id] = turn_id

    threading.Thread(
        target=_run_turn,
        args=(state, user_text, attachments or [], auto_user, cancel_event, env_snapshot),
        daemon=True,
    ).start()

    # The first yielded event is the persisted user message. Waiting briefly
    # avoids a reload seeing "active turn, empty history" in the spawn window.
    deadline = time.monotonic() + 1.0
    with state.changed:
        while not state.events and not state.finished and time.monotonic() < deadline:
            state.changed.wait(timeout=0.05)

    return turn_id


def cancel(session_id: str) -> bool:
    """Signal the active turn for this session to stop."""
    if active_turn_id(session_id) is None:
        return False
    return agent._signal_cancel(session_id)


def append_event(turn_id: str, event: dict) -> None:
    state = _get(turn_id)
    if state is None:
        return
    terminal = event.get("kind") == "done"
    with state.changed:
        if state.finished:
            return
        state.events.append(event)
        if terminal:
            state.finished = True
            state.finished_at = time.monotonic()
        state.changed.notify_all()
    if terminal:
        with _REGISTRY_LOCK:
            if _ACTIVE_BY_SESSION.get(state.session_id) == turn_id:
                _ACTIVE_BY_SESSION.pop(state.session_id, None)


def subscribe(turn_id: str) -> Iterator[dict]:
    """Yield the turn backlog followed by live events until ``done``."""
    state = _get(turn_id)
    if state is None:
        yield {"kind": "error", "message": "orchestrator turn not found"}
        yield {"kind": "done"}
        return

    idx = 0
    while True:
        with state.changed:
            while idx >= len(state.events) and not state.finished:
                state.changed.wait()
            if idx < len(state.events):
                event = state.events[idx]
                idx += 1
            elif state.finished:
                return
            else:
                continue
        yield event
        if event.get("kind") == "done":
            return


def _run_turn(
    state: TurnState,
    user_text: str,
    attachments: list[dict],
    auto_user: bool,
    cancel_event: threading.Event,
    env_snapshot: dict[str, str | None],
) -> None:
    _apply_env(env_snapshot)
    terminal = False
    db = None
    try:
        db = SessionLocal()
        for event in agent.run_turn(
            db,
            state.session_id,
            user_text,
            attachments=attachments,
            auto_user=auto_user,
            cancel_event=cancel_event,
        ):
            append_event(state.turn_id, event)
            if event.get("kind") == "done":
                terminal = True
                break
    except Exception as exc:  # pragma: no cover - defensive; run_turn catches internally
        append_event(state.turn_id, {"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        agent._release_turn(state.session_id, cancel_event)
        if db is not None:
            db.close()
        if not terminal:
            append_event(state.turn_id, {"kind": "done"})
