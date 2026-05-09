"""Per-session turn cancellation & lifecycle.

When a new user message arrives for a session that already has an in-flight
turn, we want to stop the old one — both to free resources and (more
importantly) to avoid burning OpenRouter credits on an answer the user no
longer cares about. We track an Event per session: a new turn signals the
previous turn's event, then installs its own.
"""
from __future__ import annotations
import threading


_TURN_CANCEL_EVENTS: dict[str, threading.Event] = {}
_TURN_LOCK = threading.Lock()


def _claim_turn(session_id: str) -> threading.Event:
    """Signal any prior turn for this session to cancel, then return a fresh
    cancellation event for the new turn."""
    with _TURN_LOCK:
        old = _TURN_CANCEL_EVENTS.get(session_id)
        if old is not None:
            old.set()
        ev = threading.Event()
        _TURN_CANCEL_EVENTS[session_id] = ev
        return ev


def _release_turn(session_id: str, ev: threading.Event) -> None:
    """Drop the event from the registry once the turn has finished, but only
    if we still own it — a newer turn may have replaced it."""
    with _TURN_LOCK:
        cur = _TURN_CANCEL_EVENTS.get(session_id)
        if cur is ev:
            del _TURN_CANCEL_EVENTS[session_id]


def _signal_cancel(session_id: str) -> bool:
    """Externally signal the in-flight turn for this session to cancel.

    Unlike :func:`_claim_turn`, this does NOT replace the registry entry —
    it just sets the existing event. The running turn detects this via
    `_was_superseded` returning False (registry identity unchanged) and
    treats it as an explicit user cancel.

    Returns True if a turn was actively running and got the signal.
    """
    with _TURN_LOCK:
        ev = _TURN_CANCEL_EVENTS.get(session_id)
        if ev is None:
            return False
        ev.set()
        return True


def _was_superseded(session_id: str, my_event: threading.Event) -> bool:
    """True if some other turn replaced our event in the registry — i.e. the
    user sent a new message instead of clicking cancel."""
    with _TURN_LOCK:
        cur = _TURN_CANCEL_EVENTS.get(session_id)
        return cur is not None and cur is not my_event
