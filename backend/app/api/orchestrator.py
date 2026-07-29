"""Orchestrator chat sessions — REST + SSE endpoints.

POST   /api/workflows/{wid}/sessions       create a session for a workflow
GET    /api/workflows/{wid}/sessions       list sessions on a workflow
GET    /api/workflows/{wid}/sessions/{sid}/messages  chat history rendered for the panel
DELETE /api/workflows/{wid}/sessions/{sid}/messages  clear chat history for the panel
POST   /api/workflows/{wid}/sessions/{sid}/messages  SSE stream of orchestrator events
                                                     for one user-message turn
"""
from __future__ import annotations
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app import images as images_mod, models, schemas
from app.orchestrator import agent
from app.orchestrator import turns as turn_service


router = APIRouter(prefix="/api", tags=["orchestrator"])


def _sse(turn_id: str):
    for event in turn_service.subscribe(turn_id):
        yield f"data: {json.dumps(event, default=str)}\n\n"


def _sse_response(turn_id: str) -> StreamingResponse:
    return StreamingResponse(
        _sse(turn_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering on dev
            "Connection": "keep-alive",
        },
    )


def _get_session(db: DbSession, wid: str, sid: str) -> models.Session:
    sess = (
        db.query(models.Session)
        .filter_by(id=sid, workflow_id=wid)
        .first()
    )
    if not sess:
        raise HTTPException(404, f"session {sid} not found")
    return sess


@router.post("/workflows/{wid}/sessions", response_model=schemas.SessionOut)
def create_session(wid: str, db: DbSession = Depends(get_db)) -> schemas.SessionOut:
    if not db.get(models.Workflow, wid):
        raise HTTPException(404, f"workflow {wid} not found")
    s = models.Session(workflow_id=wid)
    db.add(s)
    db.commit()
    db.refresh(s)
    return schemas.SessionOut(id=s.id, workflow_id=s.workflow_id)


@router.get("/workflows/{wid}/sessions", response_model=list[schemas.SessionOut])
def list_sessions(wid: str, db: DbSession = Depends(get_db)) -> list[schemas.SessionOut]:
    if not db.get(models.Workflow, wid):
        raise HTTPException(404, f"workflow {wid} not found")
    rows = (
        db.query(models.Session)
        .filter_by(workflow_id=wid)
        .order_by(models.Session.created_at.asc())
        .all()
    )
    return [schemas.SessionOut(id=r.id, workflow_id=r.workflow_id) for r in rows]


@router.get("/workflows/{wid}/sessions/{sid}/messages", response_model=schemas.SessionMessagesOut)
def get_messages(
    wid: str,
    sid: str,
    db: DbSession = Depends(get_db),
) -> schemas.SessionMessagesOut:
    sess = _get_session(db, wid, sid)
    bubbles = agent.render_history(db, sid)
    active_turn_id = turn_service.active_turn_id(sid)
    active_runs = (
        db.query(models.Run)
        .filter(
            models.Run.workflow_id == sess.workflow_id,
            models.Run.kind == "orchestrator",
            models.Run.status.in_(("running", "pending")),
        )
        .order_by(models.Run.started_at.desc())
        .all()
    )
    return schemas.SessionMessagesOut(
        messages=bubbles,  # type: ignore[arg-type]
        active_turn=active_turn_id is not None or agent._is_turn_active(sid),
        active_turn_id=active_turn_id,
        active_runs=[
            schemas.ActiveRunOut(id=r.id, workflow_id=r.workflow_id, status=r.status)
            for r in active_runs
        ],
    )


@router.delete("/workflows/{wid}/sessions/{sid}/messages")
def clear_messages(wid: str, sid: str, db: DbSession = Depends(get_db)) -> dict:
    _get_session(db, wid, sid)

    # Inline-agent traces belong to the chat tool cards that reference them,
    # not to workspace run history. Collect those private run ids before the
    # messages disappear, then delete the complete trace graph as part of the
    # same clear-context transaction. CallChat uses string references instead
    # of a foreign key, so remove continuations explicitly before the Run ->
    # NodeRun -> CallTranscript cascades fire.
    inline_run_ids: set[str] = set()
    tool_rows = (
        db.query(models.Message)
        .filter_by(session_id=sid, role="tool", name="run_agent")
        .all()
    )
    for row in tool_rows:
        try:
            payload = json.loads(row.content or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        display = payload.get("_display") if isinstance(payload, dict) else None
        run_id = display.get("run_id") if isinstance(display, dict) else None
        if isinstance(run_id, str) and run_id:
            inline_run_ids.add(run_id)

    private_runs = []
    if inline_run_ids:
        private_runs = (
            db.query(models.Run)
            .filter(
                models.Run.id.in_(inline_run_ids),
                models.Run.workflow_id == wid,
                models.Run.kind == "inline_agent",
            )
            .all()
        )
        node_run_ids = [nr.id for run in private_runs for nr in run.node_runs]
        if node_run_ids:
            db.query(models.CallChat).filter(
                models.CallChat.node_run_id.in_(node_run_ids)
            ).delete(synchronize_session=False)
        for run in private_runs:
            db.delete(run)

    db.query(models.Message).filter_by(session_id=sid).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted_inline_agents": len(private_runs)}


@router.post("/workflows/{wid}/sessions/{sid}/cancel")
def cancel_session_turn(wid: str, sid: str, db: DbSession = Depends(get_db)) -> dict:
    """Signal the in-flight orchestrator turn for this session to stop.

    Idempotent: returns ``{cancelled: false}`` if no turn is currently running.
    The running ``run_turn`` generator detects the signal at its next checkpoint
    (between LLM rounds, mid-SSE-stream, or between tool calls) and exits.
    """
    _get_session(db, wid, sid)
    ok = turn_service.cancel(sid)
    return {"cancelled": ok}


@router.post("/workflows/{wid}/sessions/{sid}/messages")
def post_message(
    wid: str,
    sid: str,
    body: schemas.UserMessageIn,
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Stream orchestrator events as Server-Sent Events.

    Each event is a single line `data: {json}\\n\\n`. Event kinds match
    ``app.orchestrator.agent.run_turn``: user_message, assistant_text,
    tool_call_start, tool_call_end, error, done.
    """
    # Validate + downscale attachments before the stream opens so a bad one
    # fails the whole request as a 422 instead of erroring mid-SSE.
    try:
        attachments = [
            {"data_url": images_mod.normalize_attachment(a.data_url), "filename": a.filename}
            for a in body.attachments
        ]
    except images_mod.ImageError as e:
        raise HTTPException(422, str(e))

    _get_session(db, wid, sid)
    turn_id = turn_service.start_turn(
        sid,
        body.text,
        attachments=attachments,
        auto_user=body.auto,
    )
    return _sse_response(turn_id)


@router.get("/workflows/{wid}/sessions/{sid}/turns/{turn_id}/events")
def stream_turn_events(
    wid: str,
    sid: str,
    turn_id: str,
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Replay and tail one in-flight orchestrator turn as Server-Sent Events."""
    _get_session(db, wid, sid)
    if not turn_service.turn_belongs_to_session(turn_id, sid):
        raise HTTPException(404, f"turn {turn_id} not found")
    return _sse_response(turn_id)
