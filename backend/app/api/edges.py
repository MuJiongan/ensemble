from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app import models, schemas
from app.api.workflows import to_edge_out

router = APIRouter(prefix="/api", tags=["edges"])


@router.post("/workflows/{wid}/edges", response_model=schemas.EdgeOut)
def create_edge(wid: str, body: schemas.EdgeIn, db: Session = Depends(get_db)):
    if not db.get(models.Workflow, wid):
        raise HTTPException(404)
    endpoint_count = (
        db.query(models.Node)
        .filter(
            models.Node.workflow_id == wid,
            models.Node.id.in_([body.from_node_id, body.to_node_id]),
        )
        .count()
    )
    if endpoint_count != len({body.from_node_id, body.to_node_id}):
        raise HTTPException(400, detail="edge endpoints must belong to workflow")
    e = models.Edge(
        workflow_id=wid,
        from_node_id=body.from_node_id,
        from_output=body.from_output,
        to_node_id=body.to_node_id,
        to_input=body.to_input,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return to_edge_out(e)


@router.delete("/workflows/{wid}/edges/{eid}")
def delete_edge(wid: str, eid: str, db: Session = Depends(get_db)):
    e = db.query(models.Edge).filter_by(id=eid, workflow_id=wid).first()
    if not e:
        raise HTTPException(404)
    db.delete(e)
    db.commit()
    return {"ok": True}
