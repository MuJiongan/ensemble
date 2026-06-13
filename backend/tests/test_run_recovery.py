"""Recovery from orphaned 'running' runs.

A run's status lives in the DB while the live subprocess is tracked only in
in-memory event state. A crash/restart wipes that state but leaves the row
reading 'running' — a ghost that (without recovery) can't be cancelled, can't
be deleted, and blocks deleting its parent workflow. These tests cover the two
healing paths: the startup sweep and the cancel-endpoint reconciliation.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models
from app.runner import service as run_service
from app.api import runs as runs_api
from app.runner import events as ev_mod


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _seed_running_run(db) -> str:
    wf = models.Workflow(name="wf")
    db.add(wf)
    db.commit()
    db.refresh(wf)
    run = models.Run(workflow_id=wf.id, status="running", inputs={})
    db.add(run)
    db.commit()
    db.refresh(run)
    # No in-memory event state — the subprocess (and its tracking) is gone.
    ev_mod.discard(run.id)
    return run.id


def test_startup_reconcile_marks_inflight_runs_errored(db_factory, monkeypatch):
    db = db_factory()
    rid = _seed_running_run(db)

    monkeypatch.setattr(run_service, "SessionLocal", db_factory)
    healed = run_service.reconcile_interrupted_runs()
    assert healed == 1

    refreshed = db.get(models.Run, rid)
    db.refresh(refreshed)
    assert refreshed.status == "error"
    assert "interrupted" in (refreshed.error or "")
    assert refreshed.ended_at is not None


def test_cancel_reconciles_orphaned_running_run(db_factory):
    db = db_factory()
    rid = _seed_running_run(db)

    result = runs_api.cancel_run(rid, db=db)
    assert result == {"cancelled": True}

    run = db.get(models.Run, rid)
    assert run.status == "cancelled"
    assert run.ended_at is not None


def test_cancelled_run_can_then_be_deleted(db_factory):
    db = db_factory()
    rid = _seed_running_run(db)

    runs_api.cancel_run(rid, db=db)
    assert runs_api.delete_run(rid, db=db) == {"ok": True}
    assert db.get(models.Run, rid) is None


def test_delete_still_refuses_an_actually_running_run(db_factory):
    db = db_factory()
    rid = _seed_running_run(db)
    # Still 'running' and not cancelled: delete must refuse so the subprocess
    # can't outlive its row.
    with pytest.raises(Exception):
        runs_api.delete_run(rid, db=db)
