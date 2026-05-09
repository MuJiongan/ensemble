"""Shared graph mutation helpers used by both the REST API and the orchestrator
tool surface. Keep this layer narrow — only operations that are genuinely the
same between callers belong here. The two callers differ in how they validate
input (Pydantic vs raw dicts) and surface errors (HTTPException vs ValueError),
so most ops stay caller-specific.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models


def cascade_delete_node(db: Session, node: models.Node) -> None:
    """Remove a node, every edge that touches it, and clear the workflow's
    input/output pointers if they referenced it. Does NOT commit — the caller
    decides the transaction boundary."""
    nid = node.id
    db.query(models.Edge).filter(
        (models.Edge.from_node_id == nid) | (models.Edge.to_node_id == nid)
    ).delete(synchronize_session=False)
    w = db.get(models.Workflow, node.workflow_id)
    if w is not None:
        if w.input_node_id == nid:
            w.input_node_id = None
        if w.output_node_id == nid:
            w.output_node_id = None
    db.delete(node)
