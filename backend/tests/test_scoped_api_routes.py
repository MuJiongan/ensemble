from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.api import call_chats as call_chats_api
from app.api import catalog as catalog_api
from app.api import edges as edges_api
from app.api import nodes as nodes_api
from app.api import orchestrator as orchestrator_api
from app.db import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _workflow(db, name: str) -> models.Workflow:
    wf = models.Workflow(name=name)
    db.add(wf)
    db.flush()
    return wf


def _node(db, workflow_id: str, name: str) -> models.Node:
    node = models.Node(workflow_id=workflow_id, name=name)
    db.add(node)
    db.flush()
    return node


def test_node_mutations_are_workflow_scoped(db):
    wf1 = _workflow(db, "one")
    wf2 = _workflow(db, "two")
    node = _node(db, wf1.id, "loader")
    db.commit()

    out = nodes_api.patch_node(
        wf1.id,
        node.id,
        schemas.NodePatch(name="renamed"),
        db=db,
    )
    assert out.name == "renamed"

    with pytest.raises(HTTPException) as exc:
        nodes_api.patch_node(wf2.id, node.id, schemas.NodePatch(name="wrong"), db=db)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        nodes_api.delete_node(wf2.id, node.id, db=db)
    assert exc.value.status_code == 404


def test_edge_mutations_are_workflow_scoped_and_validate_endpoints(db):
    wf1 = _workflow(db, "one")
    wf2 = _workflow(db, "two")
    a = _node(db, wf1.id, "a")
    b = _node(db, wf1.id, "b")
    outsider = _node(db, wf2.id, "outsider")
    db.commit()

    edge = edges_api.create_edge(
        wf1.id,
        schemas.EdgeIn(
            from_node_id=a.id,
            from_output="out",
            to_node_id=b.id,
            to_input="in",
        ),
        db=db,
    )
    assert edge.workflow_id == wf1.id

    with pytest.raises(HTTPException) as exc:
        edges_api.create_edge(
            wf1.id,
            schemas.EdgeIn(
                from_node_id=a.id,
                from_output="out",
                to_node_id=outsider.id,
                to_input="in",
            ),
            db=db,
        )
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        edges_api.delete_edge(wf2.id, edge.id, db=db)
    assert exc.value.status_code == 404

    assert edges_api.delete_edge(wf1.id, edge.id, db=db) == {"ok": True}


def test_session_messages_are_workflow_scoped(db):
    wf1 = _workflow(db, "one")
    wf2 = _workflow(db, "two")
    session = models.Session(workflow_id=wf1.id)
    db.add(session)
    db.commit()
    db.refresh(session)

    assert orchestrator_api.get_messages(wf1.id, session.id, db=db).messages == []

    with pytest.raises(HTTPException) as exc:
        orchestrator_api.get_messages(wf2.id, session.id, db=db)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        orchestrator_api.clear_messages(wf2.id, session.id, db=db)
    assert exc.value.status_code == 404


def test_clean_scoped_routes_are_registered_without_old_aliases():
    paths = {
        getattr(route, "path", "")
        for router in (
            nodes_api.router,
            edges_api.router,
            orchestrator_api.router,
            call_chats_api.router,
            catalog_api.router,
        )
        for route in router.routes
    }
    assert "/api/workflows/{wid}/nodes/{nid}" in paths
    assert "/api/workflows/{wid}/edges/{eid}" in paths
    assert "/api/workflows/{wid}/sessions/{sid}/messages" in paths
    assert "/api/runs/{rid}/node-runs/{nrid}/llm-calls/{call_id}/chat" in paths
    assert "/api/runs/{rid}/node-runs/{nrid}/llm-calls/{call_id}/turns" in paths
    assert "/api/catalog/providers/{provider_id}/models/{model_id:path}/variants" in paths

    assert "/api/nodes/{nid}" not in paths
    assert "/api/edges/{eid}" not in paths
    assert "/api/sessions/{sid}/messages" not in paths
    assert "/api/node-runs/{nrid}/llm-calls/{call_id}/chat" not in paths
    assert "/api/catalog/models/{provider_id}/{model_id:path}/variants" not in paths
