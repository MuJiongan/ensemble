"""In-memory tracking of in-progress OAuth flows.

A login starts when the user clicks "Sign in" in the frontend; the backend
owns a callback receiver and returns an authorization URL. From there the
flow is asynchronous — the user authorizes in their browser, a loopback
listener or backend HTTPS route captures the callback, and the backend
exchanges the code for tokens. The frontend polls until it is done.

This module owns the per-provider state machine that bridges those two
phases. Single-user local app — module-level state is fine.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class LoginState:
    """Status of a single provider's in-flight login."""
    status: str = "pending"  # 'pending' | 'complete' | 'error'
    error: Optional[str] = None
    # Human label (e.g. account email) attached on success for the UI.
    label: Optional[str] = None
    started_at: float = 0.0
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    # The callback receiver owned by this attempt (usually a loopback server,
    # or an in-memory waiter fed by the backend HTTPS callback route).
    server: Optional[Any] = field(default=None, repr=False)
    # Exact redirect URI owned by this attempt. MCP uses it to explain fixed
    # callback collisions and to distinguish loopback from public HTTPS mode.
    callback_uri: Optional[str] = None
    # Optional idempotent rollback hook for attempt-scoped persistence. MCP
    # uses this to restore the credential snapshot when a flow is cancelled.
    on_cancel: Optional[Callable[[], None]] = field(default=None, repr=False)


_LOCK = threading.Lock()
_PENDING: dict[str, LoginState] = {}


def get(provider: str) -> Optional[LoginState]:
    with _LOCK:
        return _PENDING.get(provider)


def claim(provider: str, state: LoginState) -> bool:
    """Register an in-progress login. Returns False if one is already running."""
    with _LOCK:
        existing = _PENDING.get(provider)
        if existing is not None and existing.status == "pending":
            return False
        _PENDING[provider] = state
        return True


def update(provider: str, **fields) -> None:
    with _LOCK:
        s = _PENDING.get(provider)
        if s is None:
            return
        for k, v in fields.items():
            setattr(s, k, v)


def update_if_owner(provider: str, owner: LoginState, **fields) -> bool:
    """Update only if ``owner`` is still the registered state for ``provider``.

    A worker thread for a stale attempt may finish after a fresh attempt has
    already claimed the slot — without this guard, the stale worker's final
    ``status="error"`` would clobber the new attempt's ``"pending"``.
    Returns True if the update applied.
    """
    with _LOCK:
        s = _PENDING.get(provider)
        if s is not owner:
            return False
        for k, v in fields.items():
            setattr(s, k, v)
        return True


def update_if_pending_owner(provider: str, owner: LoginState, **fields) -> bool:
    """Update only while ``owner`` still owns a pending attempt.

    Unlike :func:`update_if_owner`, this prevents a late worker from replacing
    a terminal ``cancelled`` status with ``complete`` or another error.
    """
    with _LOCK:
        s = _PENDING.get(provider)
        if s is not owner or s.status != "pending":
            return False
        for k, v in fields.items():
            setattr(s, k, v)
        return True


def is_pending_owner(provider: str, owner: LoginState) -> bool:
    with _LOCK:
        return _PENDING.get(provider) is owner and owner.status == "pending"


def run_if_pending_owner(
    provider: str, owner: LoginState, action: Callable[[], None]
) -> bool:
    """Run ``action`` atomically with respect to cancellation.

    The action may perform a short database commit. Holding the state lock for
    that commit establishes a clear winner: either persistence finishes first,
    after which cancellation rolls it back through ``on_cancel``, or cancel
    wins and the stale worker is denied the write.
    """
    with _LOCK:
        if _PENDING.get(provider) is not owner or owner.status != "pending":
            return False
        action()
        return True


def pending_callback_owner(callback_uri: str) -> Optional[str]:
    """Return the state key currently owning an exact callback URI."""
    with _LOCK:
        for provider, state in _PENDING.items():
            if state.status == "pending" and state.callback_uri == callback_uri:
                return provider
    return None


def _stop_server(state: LoginState) -> None:
    if state.server is not None:
        try:
            state.server.stop()
        except Exception:
            pass


def _teardown(state: LoginState) -> None:
    _stop_server(state)
    if state.on_cancel is not None:
        try:
            state.on_cancel()
        except Exception:
            pass


def cancel(provider: str, error: str = "cancelled") -> bool:
    """Make a pending attempt terminal and release its resources now."""
    with _LOCK:
        state = _PENDING.get(provider)
        if state is None or state.status != "pending":
            return False
        state.status = "error"
        state.error = error
    _teardown(state)
    return True


def clear(provider: str) -> None:
    with _LOCK:
        state = _PENDING.pop(provider, None)
    if state is not None:
        _stop_server(state)


def reset(provider: str) -> None:
    """Tear down any prior login attempt for this provider.

    Stops the loopback server bound by the previous attempt (so the next
    attempt can rebind the pinned port) and clears the state entry. The
    prior worker thread, if still alive, will see the server stop and exit
    its ``wait`` early.
    """
    with _LOCK:
        prev = _PENDING.pop(provider, None)
        if prev is not None and prev.status == "pending":
            prev.status = "error"
            prev.error = "restarted"
    if prev is not None:
        if prev.status == "error":
            _teardown(prev)
        else:
            # A worker may have marked complete just before its ``finally``
            # closes the listener. Release it here so a fixed-port restart does
            # not hit a transient collision, without rolling back success.
            _stop_server(prev)
