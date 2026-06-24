from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app import models, schemas
from app.services import graph as graph_service
from app.runner import service as run_service

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


def to_node_out(n: models.Node) -> schemas.NodeOut:
    return schemas.NodeOut(
        id=n.id,
        workflow_id=n.workflow_id,
        name=n.name,
        description=n.description or "",
        code=n.code or schemas.DEFAULT_CODE,
        inputs=[schemas.IOPort(**p) for p in (n.inputs or [])],
        outputs=[schemas.IOPort(**p) for p in (n.outputs or [])],
        position=schemas.Position(**(n.position or {})),
    )


def to_edge_out(e: models.Edge) -> schemas.EdgeOut:
    return schemas.EdgeOut(
        id=e.id,
        workflow_id=e.workflow_id,
        from_node_id=e.from_node_id,
        from_output=e.from_output,
        to_node_id=e.to_node_id,
        to_input=e.to_input,
    )


def to_workflow_export(w: models.Workflow) -> schemas.WorkflowExport:
    return schemas.WorkflowExport(
        exported_at=datetime.now(timezone.utc).isoformat(),
        name=w.name,
        input_node_id=w.input_node_id,
        output_node_id=w.output_node_id,
        nodes=[
            schemas.WorkflowExportNode(
                id=n.id,
                name=n.name,
                description=n.description or "",
                code=n.code or schemas.DEFAULT_CODE,
                inputs=[schemas.IOPort(**p) for p in (n.inputs or [])],
                outputs=[schemas.IOPort(**p) for p in (n.outputs or [])],
                position=schemas.Position(**(n.position or {})),
            )
            for n in w.nodes
        ],
        edges=[
            schemas.WorkflowExportEdge(
                id=e.id,
                from_node_id=e.from_node_id,
                from_output=e.from_output,
                to_node_id=e.to_node_id,
                to_input=e.to_input,
            )
            for e in w.edges
        ],
    )


@router.get("", response_model=list[schemas.WorkflowOut])
def list_workflows(db: Session = Depends(get_db)):
    rows = db.query(models.Workflow).order_by(models.Workflow.created_at.desc()).all()
    return [
        schemas.WorkflowOut(
            id=w.id, name=w.name, input_node_id=w.input_node_id, output_node_id=w.output_node_id
        )
        for w in rows
    ]


@router.post("", response_model=schemas.WorkflowOut)
def create_workflow(body: schemas.WorkflowIn, db: Session = Depends(get_db)):
    w = models.Workflow(name=body.name)
    db.add(w)
    db.commit()
    db.refresh(w)
    return schemas.WorkflowOut(id=w.id, name=w.name, input_node_id=None, output_node_id=None)


@router.post("/import", response_model=schemas.WorkflowOut)
def import_workflow(body: schemas.WorkflowExport, db: Session = Depends(get_db)):
    name = (body.name or "untitled project").strip() or "untitled project"
    imported = graph_service.import_workflow_graph(db, body.model_dump(), name=name)
    db.commit()
    db.refresh(imported)
    return schemas.WorkflowOut(
        id=imported.id,
        name=imported.name,
        input_node_id=imported.input_node_id,
        output_node_id=imported.output_node_id,
    )


@router.get("/{wid}/export", response_model=schemas.WorkflowExport)
def export_workflow(wid: str, db: Session = Depends(get_db)):
    w = db.get(models.Workflow, wid)
    if not w:
        raise HTTPException(404)
    return to_workflow_export(w)


@router.get("/{wid}", response_model=schemas.WorkflowDetail)
def get_workflow(wid: str, db: Session = Depends(get_db)):
    w = db.get(models.Workflow, wid)
    if not w:
        raise HTTPException(404)
    return schemas.WorkflowDetail(
        id=w.id,
        name=w.name,
        input_node_id=w.input_node_id,
        output_node_id=w.output_node_id,
        nodes=[to_node_out(n) for n in w.nodes],
        edges=[to_edge_out(e) for e in w.edges],
    )


@router.patch("/{wid}", response_model=schemas.WorkflowOut)
def patch_workflow(wid: str, body: schemas.WorkflowPatch, db: Session = Depends(get_db)):
    w = db.get(models.Workflow, wid)
    if not w:
        raise HTTPException(404)
    if body.name is not None:
        w.name = body.name
    if body.input_node_id is not None:
        w.input_node_id = body.input_node_id or None
    if body.output_node_id is not None:
        w.output_node_id = body.output_node_id or None
    db.commit()
    return schemas.WorkflowOut(
        id=w.id, name=w.name, input_node_id=w.input_node_id, output_node_id=w.output_node_id
    )


@router.delete("/{wid}")
def delete_workflow(wid: str, db: Session = Depends(get_db)):
    w = db.get(models.Workflow, wid)
    if not w:
        raise HTTPException(404)
    runs = list(w.runs)
    active = [
        r for r in runs
        if r.status in ("pending", "running") or run_service.is_active(r.id)
    ]
    if active:
        raise HTTPException(
            409,
            detail="cancel or wait for active runs before deleting this project",
        )
    run_ids = [r.id for r in runs]
    db.delete(w)
    db.commit()
    for rid in run_ids:
        run_service.discard(rid)
    return {"ok": True}
