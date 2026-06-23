"""Run lifecycle service.

Owns end-to-end execution of a run: pre-creating its event state, spawning
the worker thread that drives the streaming subprocess, and persisting the
materialized result + node runs to the DB once the subprocess exits.

API and orchestrator callers should go through this module rather than
importing `runner.runner` or `runner.events` directly.
"""
from __future__ import annotations
import threading
from datetime import datetime
from typing import Any

from app.db import SessionLocal
from app import models
from app.runner import events as ev_mod
from app.runner.runner import run_workflow_streaming, materialize_run_result


def start_run(
    run_id: str,
    workflow: dict,
    inputs: dict[str, Any],
    default_model: str,
) -> None:
    """Begin a run in the background. Returns immediately.

    Pre-creates the run's event state so a WebSocket client subscribing
    immediately after this call doesn't race the subprocess spawn.
    """
    ev_mod.get_or_create(run_id)
    threading.Thread(
        target=_execute,
        args=(run_id, workflow, inputs, default_model),
        daemon=True,
    ).start()


def cancel(run_id: str) -> bool:
    """SIGTERM the run's subprocess, if any. Idempotent."""
    return ev_mod.cancel(run_id)


def is_active(run_id: str) -> bool:
    """True when a run is currently executing or about to execute."""
    return ev_mod.is_active(run_id)


def discard(run_id: str) -> None:
    """Forget in-memory event/proc state for a run, notifying subscribers."""
    ev_mod.discard(run_id)


def reconcile_interrupted_runs() -> int:
    """Mark runs the DB still thinks are in flight as interrupted.

    No run outlives the backend process — its subprocess dies when the process
    does, but the row may still read ``running``/``pending`` and the in-memory
    event state (the only thing that tracks the live subprocess) is gone. Left
    alone, such a row is a ghost: it can't be cancelled (cancel only signals a
    live subprocess) and can't be deleted (delete refuses in-flight runs),
    which in turn blocks deleting its parent workflow. Called once at startup
    to heal any such rows left behind by a crash/restart.

    Returns the number of rows reconciled.
    """
    db = SessionLocal()
    try:
        stale = (
            db.query(models.Run)
            .filter(models.Run.status.in_(("running", "pending")))
            .all()
        )
        for run in stale:
            run.status = "error"
            run.error = run.error or (
                "interrupted: the backend restarted while this run was in flight"
            )
            run.ended_at = run.ended_at or datetime.utcnow()
        if stale:
            db.commit()
        return len(stale)
    finally:
        db.close()


def has_state(run_id: str) -> bool:
    """True when in-memory event state exists for a run."""
    return ev_mod.get(run_id) is not None


def subscribe(run_id: str):
    """Async generator yielding backlog + live events for a run."""
    return ev_mod.subscribe(run_id)


def _split_call_transcripts(
    llm_calls: list,
) -> tuple[list, list[tuple[str, list]]]:
    """Separate each call's verbatim ``messages`` transcript from its trace
    record. Returns ``(lean_calls, transcripts)`` where ``lean_calls`` is the
    blob to persist on the NodeRun (no ``messages``, with a ``has_chat`` flag)
    and ``transcripts`` is the list of ``(call_id, messages)`` to persist as
    their own rows. Non-dict / call_id-less entries pass through untouched and
    carry no transcript."""
    lean_calls: list = []
    transcripts: list[tuple[str, list]] = []
    for call in llm_calls:
        if not isinstance(call, dict):
            lean_calls.append(call)
            continue
        messages = call.get("messages")
        call_id = call.get("call_id")
        entry = {k: v for k, v in call.items() if k != "messages"}
        entry["has_chat"] = bool(messages and call_id)
        lean_calls.append(entry)
        if messages and call_id:
            transcripts.append((call_id, messages))
    return lean_calls, transcripts


def _execute(run_id: str, workflow: dict, inputs: dict, default_model: str) -> None:
    run_workflow_streaming(run_id, workflow, inputs, default_model)
    result = materialize_run_result(run_id)
    db = SessionLocal()
    try:
        run = db.get(models.Run, run_id)
        if not run:
            return
        run.status = result.get("status", "error")
        run.outputs = result.get("outputs") or {}
        run.error = result.get("error")
        run.total_cost = result.get("total_cost", 0.0) or 0.0
        run.ended_at = datetime.utcnow()
        for nr in result.get("node_runs") or []:
            # Lift each call's verbatim transcript out of the llm_calls blob into
            # its own call_transcripts row, leaving a `has_chat` flag behind. The
            # blob is deserialized on every run-list load, so keeping transcripts
            # out of it is the whole point — they're read only when a continuation
            # is opened. Stored uncapped here; the continue-chat path trims to fit.
            lean_calls, transcripts = _split_call_transcripts(nr.get("llm_calls") or [])
            node_run = models.NodeRun(
                run_id=run_id,
                node_id=nr["node_id"],
                status=nr.get("status") or "error",
                inputs=nr.get("inputs") or {},
                outputs=nr.get("outputs") or {},
                logs=nr.get("logs") or [],
                llm_calls=lean_calls,
                tool_calls=nr.get("tool_calls") or [],
                error=nr.get("error"),
                duration_ms=int(nr.get("duration_ms") or 0),
                cost=float(nr.get("cost") or 0.0),
            )
            db.add(node_run)
            db.flush()  # assign node_run.id before keying transcripts to it
            for call_id, messages in transcripts:
                db.add(
                    models.CallTranscript(
                        node_run_id=node_run.id,
                        call_id=call_id,
                        messages=messages,
                    )
                )
        db.commit()
    finally:
        db.close()
