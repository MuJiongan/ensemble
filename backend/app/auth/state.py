"""In-memory tracking of in-progress OAuth flows.

A login starts when the user clicks "Sign in" in the frontend; the backend
opens a loopback server and returns an authorization URL. From there the
flow is asynchronous — the user authorizes in their browser, the loopback
captures the callback, the backend exchanges the code for tokens and
persists them. The frontend polls a status endpoint to know when it's done.

This module owns the per-provider state machine that bridges those two
phases. Single-user local app — module-level state is fine.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class LoginState:
    """Status of a single provider's in-flight login."""
    status: str = "pending"  # 'pending' | 'complete' | 'error'
    error: Optional[str] = None
    # Human label (e.g. account email) attached on success for the UI.
    label: Optional[str] = None
    started_at: float = 0.0
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    # The loopback callback server bound for this attempt. Tracked so a fresh
    # ``start_login`` can shut down a stale prior attempt's server before
    # rebinding the same pinned OAuth port.
    server: Optional[Any] = field(default=None, repr=False)


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


def clear(provider: str) -> None:
    with _LOCK:
        _PENDING.pop(provider, None)


def reset(provider: str) -> None:
    """Tear down any prior login attempt for this provider.

    Stops the loopback server bound by the previous attempt (so the next
    attempt can rebind the pinned port) and clears the state entry. The
    prior worker thread, if still alive, will see the server stop and exit
    its ``wait`` early.
    """
    with _LOCK:
        prev = _PENDING.pop(provider, None)
    if prev is not None and prev.server is not None:
        try:
            prev.server.stop()
        except Exception:
            pass
