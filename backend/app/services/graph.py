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


def clone_live_workflow(
    db: Session,
    source: models.Workflow,
    *,
    name: str,
) -> models.Workflow:
    """Create a new workflow whose live graph is copied from ``source``.

    Runs, sessions, and messages are intentionally not copied; a fork starts as
    a clean branch with the same editable canvas.
    """
    target = models.Workflow(name=name)
    db.add(target)
    db.flush()

    node_id_map: dict[str, str] = {}
    for src in source.nodes:
        node = models.Node(
            workflow_id=target.id,
            name=src.name,
            description=src.description or "",
            code=src.code,
            inputs=src.inputs or [],
            outputs=src.outputs or [],
            config=src.config or {},
            position=src.position or {},
        )
        db.add(node)
        db.flush()
        node_id_map[src.id] = node.id

    for src in source.edges:
        from_id = node_id_map.get(src.from_node_id)
        to_id = node_id_map.get(src.to_node_id)
        if not from_id or not to_id:
            continue
        db.add(models.Edge(
            workflow_id=target.id,
            from_node_id=from_id,
            from_output=src.from_output,
            to_node_id=to_id,
            to_input=src.to_input,
        ))

    target.input_node_id = node_id_map.get(source.input_node_id or "")
    target.output_node_id = node_id_map.get(source.output_node_id or "")
    return target


def workflow_from_snapshot(
    db: Session,
    snapshot: dict,
    *,
    name: str,
) -> models.Workflow:
    """Materialise a run snapshot as a new editable workflow."""
    target = models.Workflow(name=name)
    db.add(target)
    db.flush()

    node_id_map: dict[str, str] = {}
    for src in snapshot.get("nodes") or []:
        old_id = src.get("id")
        if not old_id:
            continue
        node = models.Node(
            workflow_id=target.id,
            name=src.get("name") or "node",
            description=src.get("description") or "",
            code=src.get("code") or "def run(inputs, ctx):\n    return {}\n",
            inputs=src.get("inputs") or [],
            outputs=src.get("outputs") or [],
            config=src.get("config") or {},
            position=src.get("position") or {},
        )
        db.add(node)
        db.flush()
        node_id_map[old_id] = node.id

    for src in snapshot.get("edges") or []:
        from_id = node_id_map.get(src.get("from_node_id"))
        to_id = node_id_map.get(src.get("to_node_id"))
        if not from_id or not to_id:
            continue
        db.add(models.Edge(
            workflow_id=target.id,
            from_node_id=from_id,
            from_output=src.get("from_output") or "",
            to_node_id=to_id,
            to_input=src.get("to_input") or "",
        ))

    target.input_node_id = node_id_map.get(snapshot.get("input_node_id") or "")
    target.output_node_id = node_id_map.get(snapshot.get("output_node_id") or "")
    return target
