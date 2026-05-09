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


def subscribe(run_id: str):
    """Async generator yielding backlog + live events for a run."""
    return ev_mod.subscribe(run_id)


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
            db.add(
                models.NodeRun(
                    run_id=run_id,
                    node_id=nr["node_id"],
                    status=nr.get("status") or "error",
                    inputs=nr.get("inputs") or {},
                    outputs=nr.get("outputs") or {},
                    logs=nr.get("logs") or [],
                    llm_calls=nr.get("llm_calls") or [],
                    tool_calls=nr.get("tool_calls") or [],
                    error=nr.get("error"),
                    duration_ms=int(nr.get("duration_ms") or 0),
                    cost=float(nr.get("cost") or 0.0),
                )
            )
        db.commit()
    finally:
        db.close()
