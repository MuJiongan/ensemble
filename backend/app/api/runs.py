from __future__ import annotations
import os
import sys
import traceback
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app import models, schemas
from app.runner import service as run_service

router = APIRouter(prefix="/api", tags=["runs"])

NODE_MODEL_UNSET = "No node model configured. Set a default node model in Settings before running."


def _require_node_model(db: Session) -> str:
    """The node default model from env (forwarded from Settings) or the DB
    backwards-compat row. No hardcoded fallback — a run must use a model the
    user actually chose, so we fail loudly and point at Settings instead of
    guessing one (which silently routes to the wrong provider)."""
    model = os.getenv("DEFAULT_NODE_MODEL", "")
    if not model:
        setting = db.query(models.Setting).filter_by(key="default_node_model").first()
        model = setting.value if setting and setting.value else ""
    if not model:
        raise HTTPException(status_code=400, detail=NODE_MODEL_UNSET)
    return model


def _serialize_workflow(w: models.Workflow) -> dict:
    return {
        "id": w.id,
        "input_node_id": w.input_node_id,
        "output_node_id": w.output_node_id,
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "description": n.description or "",
                "code": n.code,
                "inputs": n.inputs or [],
                "outputs": n.outputs or [],
                "config": n.config or {},
                # Captured so a snapshot can be rendered on the canvas later
                # without an extra layout pass.
                "position": n.position or {"x": 0, "y": 0},
            }
            for n in w.nodes
        ],
        "edges": [
            {
                "id": e.id,
                "from_node_id": e.from_node_id,
                "from_output": e.from_output,
                "to_node_id": e.to_node_id,
                "to_input": e.to_input,
            }
            for e in w.edges
        ],
    }


def _run_to_out(run: models.Run, node_runs) -> schemas.RunOut:
    return schemas.RunOut(
        id=run.id,
        workflow_id=run.workflow_id,
        kind=run.kind,
        status=run.status,
        inputs=run.inputs or {},
        outputs=run.outputs or {},
        error=run.error,
        total_cost=run.total_cost or 0.0,
        workflow_snapshot=run.workflow_snapshot,
        node_runs=[
            schemas.NodeRunOut(
                id=nr.id,
                node_id=nr.node_id,
                status=nr.status,
                inputs=nr.inputs or {},
                outputs=nr.outputs or {},
                logs=nr.logs or [],
                # The persisted blob already excludes the verbatim transcript
                # (lifted into call_transcripts at persist time) and carries a
                # has_chat flag for the trace UI's "open in chat" affordance, so
                # it ships as-is — no transcript ever rides the run payload.
                llm_calls=nr.llm_calls or [],
                tool_calls=nr.tool_calls or [],
                error=nr.error,
                duration_ms=nr.duration_ms or 0,
                cost=nr.cost or 0.0,
            )
            for nr in node_runs
        ],
    )


@router.post("/workflows/{wid}/runs", response_model=schemas.RunOut)
def start_run(wid: str, body: schemas.RunStartIn, db: Session = Depends(get_db)):
    w = db.get(models.Workflow, wid)
    if not w:
        raise HTTPException(404)

    # Require a configured node model *before* writing the Run row, so a missing
    # model doesn't leave an orphaned "running" run behind.
    default_model = _require_node_model(db)

    # Snapshot the graph that's about to run *before* writing the Run row, so
    # the row carries a frozen copy of exactly what executed. The runner uses
    # `wf_data`, not a re-read of the DB, so they can't drift.
    wf_data = _serialize_workflow(w)

    run = models.Run(
        workflow_id=wid,
        kind=body.kind,
        status="running",
        inputs=body.inputs,
        workflow_snapshot=wf_data,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    run_service.start_run(run.id, wf_data, body.inputs, default_model)

    return _run_to_out(run, [])


@router.post("/runs/{rid}/rerun", response_model=schemas.RunOut)
def rerun_from_snapshot(rid: str, body: schemas.RunStartIn, db: Session = Depends(get_db)):
    """Re-run a frozen graph snapshot with fresh inputs. The new run executes
    against the *stored* `workflow_snapshot` of the source run — not the
    current live workflow — so the user can re-run an old graph version
    without restoring it. The new run carries a copy of the same snapshot.
    """
    src = db.get(models.Run, rid)
    if src is None:
        raise HTTPException(404)
    if not src.workflow_snapshot:
        raise HTTPException(400, detail="source run has no snapshot to re-run")
    # Defensive: if the underlying workflow row was deleted, runs against it
    # would orphan node_run rows — refuse.
    if db.get(models.Workflow, src.workflow_id) is None:
        raise HTTPException(404, detail="workflow no longer exists")

    wf_data = src.workflow_snapshot

    default_model = _require_node_model(db)

    run = models.Run(
        workflow_id=src.workflow_id,
        kind=body.kind,
        status="running",
        inputs=body.inputs,
        workflow_snapshot=wf_data,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    run_service.start_run(run.id, wf_data, body.inputs, default_model)
    return _run_to_out(run, [])


@router.post("/runs/{rid}/cancel")
def cancel_run(rid: str, db: Session = Depends(get_db)):
    """Cancel a run. SIGTERMs the live subprocess if there is one — its worker
    thread then writes the final 'cancelled' status back to the DB on exit.

    If no live subprocess got the signal but the DB row still reads
    running/pending *and* no in-memory state tracks it, the run is orphaned
    (its worker died, or this is a within-session leftover the startup sweep
    didn't catch). Reconcile it to 'cancelled' here so it doesn't stay stuck —
    un-cancellable and un-deletable. The `has_state` guard keeps us from racing
    a worker that's mid-writeback, since its state lives until `discard`.
    Idempotent."""
    if run_service.cancel(rid):
        return {"cancelled": True}
    run = db.get(models.Run, rid)
    if (
        run
        and run.status in ("running", "pending")
        and not run_service.has_state(rid)
    ):
        run.status = "cancelled"
        run.error = run.error or "cancelled (run was no longer active)"
        run.ended_at = run.ended_at or datetime.utcnow()
        db.commit()
        run_service.discard(rid)
        return {"cancelled": True}
    return {"cancelled": False}


@router.delete("/runs/{rid}")
def delete_run(rid: str, db: Session = Depends(get_db)):
    """Delete a run and its node_runs (cascade). In-flight runs must be
    cancelled first — refusing here keeps the subprocess from outliving its
    DB row and writing back to a deleted parent on completion."""
    run = db.get(models.Run, rid)
    if not run:
        raise HTTPException(404)
    if run.status in ("running", "pending"):
        raise HTTPException(409, detail="cancel the run before deleting")
    # Clean up any call_llm continuations tied to this run's node_runs. CallChat
    # uses FK-free string refs (a continuation survives incidental node_run
    # changes), but an explicit run delete is deliberate destruction — drop its
    # continuations too rather than leave them orphaned.
    nr_ids = [nr.id for nr in run.node_runs]
    if nr_ids:
        db.query(models.CallChat).filter(
            models.CallChat.node_run_id.in_(nr_ids)
        ).delete(synchronize_session=False)
    db.delete(run)
    db.commit()
    run_service.discard(rid)
    return {"ok": True}


@router.get("/runs/{rid}", response_model=schemas.RunOut)
def get_run(rid: str, db: Session = Depends(get_db)):
    run = db.get(models.Run, rid)
    if not run:
        raise HTTPException(404)
    return _run_to_out(run, run.node_runs)


@router.get("/workflows/{wid}/runs", response_model=list[schemas.RunOut])
def list_runs(wid: str, db: Session = Depends(get_db)):
    rows = (
        db.query(models.Run)
        .filter_by(workflow_id=wid)
        .order_by(models.Run.started_at.desc())
        .all()
    )
    return [_run_to_out(r, r.node_runs) for r in rows]


def _run_row_exists(rid: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(models.Run, rid) is not None
    finally:
        db.close()


@router.websocket("/runs/{rid}/events")
async def ws_run_events(websocket: WebSocket, rid: str):
    """Stream per-run events (backlog + live tail) until the run finishes."""
    await websocket.accept()
    try:
        if not run_service.has_state(rid) and not _run_row_exists(rid):
            # The run was deleted (or never existed): no event state, no DB
            # row. Say so instead of subscribing — subscribe would create a
            # fresh empty state and park the socket on it forever.
            await websocket.send_json({"type": "run_deleted", "run_id": rid})
            return
        async for event in run_service.subscribe(rid):
            await websocket.send_json(event)
    except WebSocketDisconnect:
        return
    except Exception as e:
        # Surface the traceback to stderr for diagnostics, then send a
        # structured error envelope before closing so the client can render
        # something more useful than a silent disconnect.
        traceback.print_exc(file=sys.stderr)
        try:
            await websocket.send_json(
                {"type": "error", "error": f"{type(e).__name__}: {e}"}
            )
        except Exception:
            pass
        return
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
