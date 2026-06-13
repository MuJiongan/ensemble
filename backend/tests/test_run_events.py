"""Deletion handling in the in-memory run event stream: discarding a run must
terminate live subscribers (with a synthetic ``run_deleted`` event) instead of
leaving them parked forever on a state object nothing will ever feed again."""
from __future__ import annotations

import asyncio
import threading

from app.runner import events as ev_mod


def _fresh(run_id: str):
    ev_mod.discard(run_id)
    return ev_mod.get_or_create(run_id)


def _collect_until_done(run_id: str, after_park) -> list[dict]:
    """Subscribe to a run, run `after_park` once the subscriber is parked on
    its queue, and return the events seen once the generator terminates."""

    async def go():
        out: list[dict] = []

        async def consume():
            async for ev in ev_mod.subscribe(run_id):
                out.append(ev)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        after_park()
        await asyncio.wait_for(task, timeout=2)
        return out

    return asyncio.run(go())


def test_subscribe_ends_on_run_finished():
    rid = "ev-run-finished"
    _fresh(rid)
    events = _collect_until_done(
        rid,
        lambda: ev_mod.append_event(rid, {"type": "run_finished", "status": "success"}),
    )
    assert events[-1]["type"] == "run_finished"
    ev_mod.discard(rid)


def test_discard_terminates_live_subscriber_with_run_deleted():
    rid = "ev-run-deleted"
    _fresh(rid)
    events = _collect_until_done(rid, lambda: ev_mod.discard(rid))
    assert events == [{"type": "run_deleted", "run_id": rid}]


def test_discard_unblocks_finished_event_waiters():
    # wait_for_run-style waiters block on finished_event; deleting the run
    # must release them rather than leave the orchestrator turn hanging.
    rid = "ev-run-waiter"
    st = _fresh(rid)
    released = threading.Event()

    def waiter():
        st.finished_event.wait(timeout=2)
        if st.finished:
            released.set()

    t = threading.Thread(target=waiter)
    t.start()
    ev_mod.discard(rid)
    t.join(timeout=3)
    assert released.is_set()
