from __future__ import annotations
import sys
import traceback
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.db import get_db
from app import models, schemas
from app.runner import service as run_service

router = APIRouter(prefix="/api", tags=["runs"])


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

    import os as _os
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
    # localStorage (forwarded via header → env by middleware) wins; DB row is
    # the backwards-compat fallback. Final fallback: a sane current default.
    default_model = _os.getenv("DEFAULT_NODE_MODEL", "")
    if not default_model:
        setting = db.query(models.Setting).filter_by(key="default_node_model").first()
        default_model = setting.value if setting and setting.value else ""
    if not default_model:
        default_model = "anthropic/claude-sonnet-4.6"

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

    import os as _os
    default_model = _os.getenv("DEFAULT_NODE_MODEL", "")
    if not default_model:
        setting = db.query(models.Setting).filter_by(key="default_node_model").first()
        default_model = setting.value if setting and setting.value else ""
    if not default_model:
        default_model = "anthropic/claude-sonnet-4.6"

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
def cancel_run(rid: str):
    """SIGTERM the run's subprocess, if any. Idempotent."""
    ok = run_service.cancel(rid)
    return {"cancelled": ok}


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
    db.delete(run)
    db.commit()
    from app.runner import events as _ev
    _ev.discard(rid)
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
        .limit(20)
        .all()
    )
    return [_run_to_out(r, r.node_runs) for r in rows]


@router.websocket("/runs/{rid}/events")
async def ws_run_events(websocket: WebSocket, rid: str):
    """Stream per-run events (backlog + live tail) until the run finishes."""
    await websocket.accept()
    try:
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
