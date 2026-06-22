"""In-memory pub/sub for run events.

The runner thread writes events; WebSocket handlers (and the sync `run_workflow_sync`
compat shim) read them. Subscribers get the existing backlog plus a live tail.
"""
from __future__ import annotations
import asyncio
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunState:
    run_id: str
    events: list[dict] = field(default_factory=list)
    subscribers: list = field(default_factory=list)  # list[(loop, asyncio.Queue)]
    proc: Optional[subprocess.Popen] = None
    finished: bool = False
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    finished_event: threading.Event = field(default_factory=threading.Event)


_RUNS: dict[str, RunState] = {}
_REGISTRY_LOCK = threading.Lock()


def get_or_create(run_id: str) -> RunState:
    with _REGISTRY_LOCK:
        st = _RUNS.get(run_id)
        if st is None:
            st = RunState(run_id=run_id)
            _RUNS[run_id] = st
        return st


def get(run_id: str) -> Optional[RunState]:
    return _RUNS.get(run_id)


def append_event(run_id: str, event: dict) -> None:
    """Thread-safe: append to the run's event list and notify any live subscribers."""
    st = get_or_create(run_id)
    with st.lock:
        st.events.append(event)
        if event.get("type") == "run_finished":
            st.finished = True
        subs = list(st.subscribers)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except Exception:
            pass
    if st.finished:
        st.finished_event.set()


def set_proc(run_id: str, proc: subprocess.Popen) -> None:
    st = get_or_create(run_id)
    with st.lock:
        st.proc = proc


def discard(run_id: str) -> None:
    """Forget any in-memory state for a finished run. No-op if absent.

    Live subscribers (an open WebSocket on a run the user just deleted) get
    a synthetic ``run_deleted`` event so their generators terminate instead
    of waiting forever on a state object nothing will ever append to again.
    """
    with _REGISTRY_LOCK:
        st = _RUNS.pop(run_id, None)
    if st is None:
        return
    with st.lock:
        st.finished = True
        subs = list(st.subscribers)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(
                q.put_nowait, {"type": "run_deleted", "run_id": run_id}
            )
        except Exception:
            pass
    st.finished_event.set()


def is_active(run_id: str) -> bool:
    """Return True when a run has in-memory state that is not terminal yet."""
    st = _RUNS.get(run_id)
    if not st:
        return False
    with st.lock:
        if st.finished:
            return False
        proc = st.proc
    return proc is None or proc.poll() is None


_CANCEL_KILL_GRACE = 5.0  # seconds to wait after SIGTERM before escalating to SIGKILL


def schedule_force_kill(proc: subprocess.Popen) -> None:
    """SIGKILL ``proc`` if it ignores SIGTERM past the grace window.

    A child wedged in an uninterruptible call never delivers the SIGTERM →
    KeyboardInterrupt that lets it exit cleanly, so the parent's stdout read /
    ``proc.wait()`` would block forever and the cancel would never complete.
    Wait a bounded grace on this exact Popen handle — never on a pid, so a
    reaped/recycled process can't be mis-signalled — then kill. Runs in a daemon
    thread that lives at most the grace window and is safe to arm repeatedly."""
    def _watch() -> None:
        try:
            proc.wait(timeout=_CANCEL_KILL_GRACE)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            pass
    threading.Thread(target=_watch, daemon=True).start()


def cancel(run_id: str) -> bool:
    """Request cancellation of a run/turn. Records the intent under the lock and,
    if the subprocess is already running, SIGTERMs it (escalating to SIGKILL if
    ignored). Returns True whenever a not-yet-finished run exists — including
    during the spawn window before the proc exists, where the spawner honors the
    recorded flag the moment it owns the proc (so the caller isn't told the
    cancel failed while it is in fact pending). Idempotent."""
    st = _RUNS.get(run_id)
    if not st or st.finished:
        return False
    with st.lock:
        st.cancelled = True
        proc = st.proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        schedule_force_kill(proc)
    return True


async def subscribe(run_id: str):
    """Async generator yielding events for a run.

    Yields the existing backlog first, then live events. Returns when the run
    finishes (a `run_finished` event) or is deleted out from under the
    subscriber (a synthetic `run_deleted` event from :func:`discard`).
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    st = get(run_id)
    if st is None:
        # No in-memory state: the run/turn was never started, or (the common
        # case for one-shot chat turns) it already finished and was discarded.
        # Resurrecting an empty state via get_or_create would leave this
        # generator blocked forever on events that will never be appended —
        # so signal "gone" and return instead.
        yield {"type": "run_deleted", "run_id": run_id}
        return
    with st.lock:
        backlog = list(st.events)
        already_finished = st.finished
        st.subscribers.append((loop, q))
    try:
        for ev in backlog:
            yield ev
        if already_finished:
            return
        while True:
            ev = await q.get()
            yield ev
            if ev.get("type") in ("run_finished", "run_deleted"):
                return
    finally:
        with st.lock:
            try:
                st.subscribers.remove((loop, q))
            except ValueError:
                pass
