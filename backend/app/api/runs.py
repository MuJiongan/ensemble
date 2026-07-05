from __future__ import annotations
import os
import sys
import traceback
from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
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


def _snapshot_summary(snapshot: dict | None) -> dict | None:
    """Code-free graph shape for snapshot canvas rendering."""
    if not snapshot:
        return None
    return {
        "id": snapshot.get("id"),
        "input_node_id": snapshot.get("input_node_id"),
        "output_node_id": snapshot.get("output_node_id"),
        "nodes": [
            {
                "id": n.get("id"),
                "name": n.get("name") or n.get("id") or "",
                "description": n.get("description") or "",
                "inputs": n.get("inputs") or [],
                "outputs": n.get("outputs") or [],
                "position": n.get("position") or {"x": 0, "y": 0},
                "has_code": bool(n.get("code")),
            }
            for n in (snapshot.get("nodes") or [])
            if isinstance(n, dict) and n.get("id")
        ],
        "edges": [
            {
                "id": e.get("id"),
                "from_node_id": e.get("from_node_id"),
                "from_output": e.get("from_output"),
                "to_node_id": e.get("to_node_id"),
                "to_input": e.get("to_input"),
            }
            for e in (snapshot.get("edges") or [])
            if isinstance(e, dict)
        ],
    }


_NODE_RUN_FIELDS = ("inputs", "outputs", "logs", "llm_calls", "tool_calls")


def _parse_node_run_fields(fields: list[str] | None) -> tuple[str, ...]:
    # FastAPI sends repeated query params as a list, but accepting comma-joined
    # values keeps the helper convenient for direct callers and URLs.
    raw: list[str] = []
    for item in fields or []:
        raw.extend(part.strip() for part in item.split(","))
    selected = tuple(dict.fromkeys(f for f in raw if f))
    if not selected:
        raise HTTPException(
            400,
            detail=f"`fields` is required; choose from {list(_NODE_RUN_FIELDS)}",
        )
    bad = [f for f in selected if f not in _NODE_RUN_FIELDS]
    if bad:
        raise HTTPException(
            400,
            detail=f"unknown field(s) {bad}; allowed: {list(_NODE_RUN_FIELDS)}",
        )
    return selected


def _node_run_detail(nr: models.NodeRun, fields: tuple[str, ...]) -> schemas.NodeRunDetailOut:
    payload: dict = {
        "id": nr.id,
        "node_id": nr.node_id,
        "status": nr.status,
        "error": nr.error,
        "duration_ms": nr.duration_ms or 0,
        "cost": nr.cost or 0.0,
    }
    if "inputs" in fields:
        payload["inputs"] = nr.inputs or {}
    if "outputs" in fields:
        payload["outputs"] = nr.outputs or {}
    if "logs" in fields:
        payload["logs"] = nr.logs or []
    if "llm_calls" in fields:
        payload["llm_calls"] = nr.llm_calls or []
    if "tool_calls" in fields:
        payload["tool_calls"] = nr.tool_calls or []
    return schemas.NodeRunDetailOut(**payload)


def _node_run_summary(nr: models.NodeRun) -> schemas.NodeRunSummaryOut:
    return schemas.NodeRunSummaryOut(
        id=nr.id,
        node_id=nr.node_id,
        status=nr.status,
        error=nr.error,
        duration_ms=nr.duration_ms or 0,
        cost=nr.cost or 0.0,
        log_count=len(nr.logs or []),
        llm_call_count=len(nr.llm_calls or []),
        tool_call_count=len(nr.tool_calls or []),
    )


def _run_model_stats(node_runs) -> list[schemas.RunModelStatOut]:
    by_model: dict[str, dict] = {}
    for nr in node_runs:
        for call in nr.llm_calls or []:
            if not isinstance(call, dict):
                continue
            model = (call.get("model") or "unknown").strip() or "unknown"
            cur = by_model.setdefault(
                model,
                {
                    "model": model,
                    "calls": 0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "cost": 0.0,
                },
            )
            cur["calls"] += 1
            usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
            cur["promptTokens"] += int(usage.get("prompt_tokens") or 0)
            cur["completionTokens"] += int(usage.get("completion_tokens") or 0)
            cur["cost"] += float(call.get("cost") or 0.0)
    rows = [schemas.RunModelStatOut(**v) for v in by_model.values()]
    return sorted(rows, key=lambda r: (-r.cost, -r.calls))


def _run_tool_call_count(node_runs) -> int:
    return sum(len(nr.tool_calls or []) for nr in node_runs)


def _run_overview(run: models.Run, node_runs) -> schemas.RunOverviewOut:
    return schemas.RunOverviewOut(
        id=run.id,
        workflow_id=run.workflow_id,
        kind=run.kind,
        status=run.status,
        inputs=run.inputs or {},
        error=run.error,
        started_at=run.started_at,
        ended_at=run.ended_at,
        total_cost=run.total_cost or 0.0,
        workflow_snapshot=_snapshot_summary(run.workflow_snapshot),
        node_runs=[_node_run_summary(nr) for nr in node_runs],
        model_stats=_run_model_stats(node_runs),
        tool_call_count=_run_tool_call_count(node_runs),
    )


def _run_summary(run: models.Run) -> schemas.RunSummaryOut:
    return schemas.RunSummaryOut(
        id=run.id,
        workflow_id=run.workflow_id,
        kind=run.kind,
        status=run.status,
        inputs=run.inputs or {},
        error=run.error,
        started_at=run.started_at,
        ended_at=run.ended_at,
        total_cost=run.total_cost or 0.0,
    )


def _run_card(run, status_by_node: dict[str, str]) -> schemas.RunCardOut:
    snapshot = run.workflow_snapshot or {}
    nodes = snapshot.get("nodes") or []
    node_names = {
        n.get("id"): n.get("name") or n.get("id")
        for n in nodes
        if isinstance(n, dict)
    }
    card_nodes = [
        schemas.RunCardNodeOut(
            id=n.get("id") or "",
            name=n.get("name") or n.get("id") or "",
            status=status_by_node.get(n.get("id"), "pending"),
        )
        for n in nodes
        if isinstance(n, dict) and n.get("id")
    ]
    return schemas.RunCardOut(
        id=run.id,
        workflow_id=run.workflow_id,
        status=run.status,
        error=run.error,
        total_cost=run.total_cost or 0.0,
        node_count=len(nodes),
        nodes=card_nodes,
        input_node_name=node_names.get(snapshot.get("input_node_id")),
        output_node_name=node_names.get(snapshot.get("output_node_id")),
    )


@router.post("/workflows/{wid}/runs", response_model=schemas.RunOverviewOut)
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

    return _run_overview(run, [])


@router.post("/runs/{rid}/rerun", response_model=schemas.RunOverviewOut)
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
    return _run_overview(run, [])


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
    # Clean up any agent continuations tied to this run's node_runs. CallChat
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


@router.get("/runs/{rid}", response_model=schemas.RunOverviewOut)
def get_run(rid: str, db: Session = Depends(get_db)):
    run = db.get(models.Run, rid)
    if not run:
        raise HTTPException(404)
    return _run_overview(run, run.node_runs)


@router.get("/runs/{rid}/outputs", response_model=schemas.RunOutputsOut)
def get_run_outputs(rid: str, db: Session = Depends(get_db)):
    run = db.get(models.Run, rid)
    if not run:
        raise HTTPException(404)
    return schemas.RunOutputsOut(outputs=run.outputs or {})


@router.get("/runs/{rid}/snapshot", response_model=schemas.RunSnapshotOut)
def get_run_snapshot(rid: str, db: Session = Depends(get_db)):
    run = db.get(models.Run, rid)
    if not run:
        raise HTTPException(404)
    return schemas.RunSnapshotOut(workflow_snapshot=run.workflow_snapshot)


@router.get("/runs/{rid}/snapshot/nodes/{nid}/code", response_model=schemas.SnapshotNodeCodeOut)
def get_run_snapshot_node_code(rid: str, nid: str, db: Session = Depends(get_db)):
    run = db.get(models.Run, rid)
    if not run:
        raise HTTPException(404)
    for node in (run.workflow_snapshot or {}).get("nodes", []) or []:
        if isinstance(node, dict) and node.get("id") == nid:
            return schemas.SnapshotNodeCodeOut(
                node_id=nid,
                code=node.get("code") or schemas.DEFAULT_CODE,
            )
    raise HTTPException(404, detail="snapshot node not found")


@router.get("/runs/{rid}/card", response_model=schemas.RunCardOut)
def get_run_card(rid: str, db: Session = Depends(get_db)):
    run = (
        db.query(
            models.Run.id,
            models.Run.workflow_id,
            models.Run.status,
            models.Run.error,
            models.Run.total_cost,
            models.Run.workflow_snapshot,
        )
        .filter(models.Run.id == rid)
        .first()
    )
    if not run:
        raise HTTPException(404)
    statuses = {
        node_id: status
        for node_id, status in db.query(models.NodeRun.node_id, models.NodeRun.status)
        .filter_by(run_id=rid)
        .all()
    }
    return _run_card(run, statuses)


@router.get(
    "/runs/{rid}/node-runs/{nrid}",
    response_model=schemas.NodeRunDetailOut,
    response_model_exclude_none=True,
)
def get_node_run(
    rid: str,
    nrid: str,
    fields: Annotated[list[str] | None, Query()] = None,
    db: Session = Depends(get_db),
):
    nr = (
        db.query(models.NodeRun)
        .filter_by(id=nrid, run_id=rid)
        .first()
    )
    if not nr:
        raise HTTPException(404)
    return _node_run_detail(nr, _parse_node_run_fields(fields))


@router.get("/workflows/{wid}/runs", response_model=list[schemas.RunSummaryOut])
def list_runs(wid: str, db: Session = Depends(get_db)):
    rows = (
        db.query(
            models.Run.id,
            models.Run.workflow_id,
            models.Run.kind,
            models.Run.status,
            models.Run.inputs,
            models.Run.error,
            models.Run.started_at,
            models.Run.ended_at,
            models.Run.total_cost,
        )
        .filter(models.Run.workflow_id == wid)
        .order_by(models.Run.started_at.desc())
        .all()
    )
    return [_run_summary(r) for r in rows]


def _run_row_exists(rid: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(models.Run, rid) is not None
    finally:
        db.close()


def _filtered_run_event(event: dict, *, node_id: str | None) -> dict | None:
    et = event.get("type")
    if et == "run_deleted":
        return event

    if node_id:
        if et in ("run_started", "run_finished"):
            return event if et == "run_started" else {**event, "outputs": {}}
        if event.get("node_id") != node_id:
            return None
        return event

    if et in ("mcp_status", "run_started"):
        return event
    if et == "node_started":
        return {**event, "inputs": {}}
    if et == "node_finished":
        return {
            **event,
            "inputs": {},
            "outputs": {},
            "logs": [],
            "llm_calls": [],
            "tool_calls": [],
        }
    if et == "run_finished":
        return {**event, "outputs": {}}
    if et == "tool_call_finished":
        result = event.get("result")
        if (
            isinstance(result, dict)
            and result.get("error_type") == "needs_auth"
            and result.get("server")
        ):
            filtered = {
                "type": "tool_call_finished",
                "node_id": event.get("node_id"),
                "tool": event.get("tool"),
                "args": {},
                "result": {
                    "error_type": "needs_auth",
                    "server": result.get("server"),
                },
                "via": event.get("via"),
            }
            for key in ("call_id", "tc_index", "round", "error"):
                if key in event:
                    filtered[key] = event[key]
            return filtered
    return None


@router.websocket("/runs/{rid}/events")
async def ws_run_events(
    websocket: WebSocket,
    rid: str,
    node_id: str | None = Query(None),
):
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
            filtered = _filtered_run_event(event, node_id=node_id)
            if filtered is not None:
                await websocket.send_json(filtered)
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
