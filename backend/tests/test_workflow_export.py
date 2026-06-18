"""Round-trip tests for project import/export."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models
from app.services import graph as graph_service
from app.api.workflows import to_workflow_export


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield Session
    engine.dispose()


def _seed_workflow(db) -> models.Workflow:
    w = models.Workflow(name="invoice flow")
    db.add(w)
    db.flush()

    a = models.Node(
        workflow_id=w.id,
        name="input",
        code="def run(inputs, ctx):\n    return {'text': inputs['text']}\n",
        inputs=[{"name": "text", "type_hint": "str", "required": True}],
        outputs=[{"name": "text", "type_hint": "str", "required": True}],
        position={"x": 10, "y": 20},
    )
    b = models.Node(
        workflow_id=w.id,
        name="output",
        code="def run(inputs, ctx):\n    return {'result': inputs['text']}\n",
        inputs=[{"name": "text", "type_hint": "str", "required": True}],
        outputs=[{"name": "result", "type_hint": "str", "required": True}],
        position={"x": 200, "y": 20},
    )
    db.add_all([a, b])
    db.flush()

    db.add(models.Edge(
        workflow_id=w.id,
        from_node_id=a.id,
        from_output="text",
        to_node_id=b.id,
        to_input="text",
    ))
    w.input_node_id = a.id
    w.output_node_id = b.id
    db.commit()
    db.refresh(w)
    return w


def test_export_bundle_contains_graph(db_factory):
    Session = db_factory
    with Session() as db:
        source = _seed_workflow(db)
        exported = to_workflow_export(source)

    assert exported.name == "invoice flow"
    assert len(exported.nodes) == 2
    assert len(exported.edges) == 1
    assert exported.input_node_id == exported.nodes[0].id
    assert exported.edges[0].from_node_id == exported.nodes[0].id


def test_import_regenerates_ids_and_preserves_graph(db_factory):
    Session = db_factory
    with Session() as db:
        source = _seed_workflow(db)
        exported = to_workflow_export(source)
        imported = graph_service.import_workflow_graph(
            db,
            exported.model_dump(),
            name="imported copy",
        )
        db.commit()
        db.refresh(imported)

        assert imported.id != source.id
        assert imported.name == "imported copy"
        assert len(imported.nodes) == 2
        assert len(imported.edges) == 1

        by_name = {n.name: n for n in imported.nodes}
        assert by_name["input"].code == source.nodes[0].code
        assert by_name["output"].position == source.nodes[1].position

        edge = imported.edges[0]
        assert edge.from_node_id == by_name["input"].id
        assert edge.to_node_id == by_name["output"].id
        assert imported.input_node_id == by_name["input"].id
        assert imported.output_node_id == by_name["output"].id
