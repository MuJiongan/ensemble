"""Continue-chat (call_llm continuation) API.

Each finished ``ctx.call_llm`` in a run trace can be continued as a chat: we
seed a ``CallChat`` from the call's recorded conversation and let the user keep
talking to the same model, with the same tools reconnected, persisted per call.
One continuation per call — it's a single ongoing thread, not a branch.

Endpoints:
  POST /api/node-runs/{nrid}/llm-calls/{call_id}/chat  — create-or-get continuation
  POST /api/call-chats/{id}/turns                      — send a turn → {turn_id}
  WS   /api/call-chats/turns/{turn_id}/events          — stream turn events
  POST /api/call-chats/turns/{turn_id}/cancel          — stop a streaming turn
"""
from __future__ import annotations
import os
import sys
import traceback
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app import models, schemas
from app.runner import chat as chat_service
from app.runner.runner import build_child_env

router = APIRouter(prefix="/api", tags=["call-chats"])


def _chat_to_out(chat: models.CallChat) -> schemas.CallChatOut:
    return schemas.CallChatOut(
        id=chat.id,
        workflow_id=chat.workflow_id,
        node_run_id=chat.node_run_id,
        call_id=chat.call_id,
        label=chat.label or "",
        model=chat.model or "",
        provider_id=chat.provider_id or "",
        variant=chat.variant or "",
        tools=chat.tools or [],
        messages=chat.messages or [],
    )


def _find_call(llm_calls, call_id: str):
    """Return (call_record, 1-based index) for `call_id`, or (None, 0)."""
    for i, c in enumerate(llm_calls or []):
        if isinstance(c, dict) and c.get("call_id") == call_id:
            return c, i + 1
    return None, 0


def _node_name(db: Session, node_run: models.NodeRun) -> str:
    """Resolve the node's display name from its run's frozen snapshot (stable
    even if the live node was later renamed or deleted)."""
    run = db.get(models.Run, node_run.run_id)
    if run and run.workflow_snapshot:
        for n in run.workflow_snapshot.get("nodes", []) or []:
            if n.get("id") == node_run.node_id:
                return n.get("name") or node_run.node_id
    return node_run.node_id


@router.post(
    "/node-runs/{nrid}/llm-calls/{call_id}/chat",
    response_model=schemas.CallChatOut,
)
def open_call_chat(nrid: str, call_id: str, db: Session = Depends(get_db)):
    """Create (or return the existing) continuation for one call_llm call."""
    existing = (
        db.query(models.CallChat)
        .filter_by(node_run_id=nrid, call_id=call_id)
        .first()
    )
    if existing is not None:
        return _chat_to_out(existing)

    nr = db.get(models.NodeRun, nrid)
    if nr is None:
        raise HTTPException(404, detail="node run not found")
    call, idx = _find_call(nr.llm_calls, call_id)
    if call is None:
        raise HTTPException(404, detail="llm call not found")
    seed = call.get("messages")
    if not seed:
        # Either the call predates message persistence or its transcript
        # exceeded the persistence budget — it isn't continuable.
        raise HTTPException(404, detail="this call has no saved conversation to continue")

    run = db.get(models.Run, nr.run_id)
    workflow_id = run.workflow_id if run else ""
    call_label = (call.get("label") or "").strip()
    label = (
        f"{_node_name(db, nr)} · {call_label}"
        if call_label
        else f"{_node_name(db, nr)} · call {idx}"
    )

    chat = models.CallChat(
        workflow_id=workflow_id,
        node_run_id=nrid,
        call_id=call_id,
        label=label,
        model=call.get("model") or "",
        provider_id=call.get("provider_id") or "",
        variant=call.get("variant") or "",
        tools=call.get("tools") or [],
        messages=seed,
    )
    db.add(chat)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent open (e.g. a double-click) already inserted this
        # (node_run_id, call_id). Roll back and return the existing row rather
        # than 500ing on the uniqueness constraint.
        db.rollback()
        existing = (
            db.query(models.CallChat)
            .filter_by(node_run_id=nrid, call_id=call_id)
            .first()
        )
        if existing is not None:
            return _chat_to_out(existing)
        raise
    db.refresh(chat)
    return _chat_to_out(chat)


@router.post("/call-chats/{cid}/turns", response_model=schemas.CallChatTurnOut)
def send_call_chat_turn(
    cid: str, body: schemas.CallChatTurnIn, db: Session = Depends(get_db)
):
    chat = db.get(models.CallChat, cid)
    if chat is None:
        raise HTTPException(404)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, detail="empty message")

    # Resolve the model + provider/variant for this turn as a MATCHED pair, so
    # the turn never sends one provider's request with another's model id.
    #   - provider/variant come from the X-Node-* headers (applied to process
    #     env, the same source build_child_env reads).
    #   - model: the switcher's choice (body.model) when a provider is pinned;
    #     else the current node-default model (DEFAULT_NODE_MODEL env — matches
    #     the default provider the unpinned headers carry); else the stored
    #     model as a last resort.
    # The frontend only sends body.model when it pins a provider, so an old
    # call with no recorded provider falls back to the current node default for
    # BOTH provider and model instead of pairing a stale model with a new one.
    provider_id = (os.getenv("NODE_PROVIDER_ID") or "").strip()
    variant = os.getenv("DEFAULT_NODE_VARIANT") or ""
    model = (
        (body.model or "").strip()
        or (os.getenv("DEFAULT_NODE_MODEL") or "").strip()
        or chat.model
        or ""
    )

    # Persist the selection — all three together, never a partial mismatch — so
    # reopening the continuation keeps the same model + provider.
    chat.model = model
    chat.provider_id = provider_id
    chat.variant = variant

    # Append the user turn, then sanitize the transcript before it is persisted
    # or sent to the child: fold consecutive user turns into one (a prior
    # failed/cancelled turn leaves a dangling user message, and two user turns
    # in a row hard-400 strict providers) and normalize the head to system*-then-
    # user (a seed can start with a tool/assistant message a strict provider
    # rejects). Persist immediately so a mid-turn reload shows the user's
    # message; reassign (not .append) so SQLAlchemy sees the JSON column dirty.
    messages = list(chat.messages or [])
    messages.append({"role": "user", "content": text})
    messages = chat_service.prepare_transcript(messages)
    chat.messages = messages
    db.commit()

    # Snapshot the child env now, while the request's settings headers are
    # applied to process env — the turn runs on a background thread later.
    child_env = build_child_env()
    turn_id = "turn-" + uuid.uuid4().hex[:12]
    chat_service.start_chat_turn(
        turn_id=turn_id,
        chat_id=chat.id,
        messages=messages,
        tools=list(chat.tools or []),
        model=model,
        child_env=child_env,
    )
    return schemas.CallChatTurnOut(turn_id=turn_id)


@router.post("/call-chats/turns/{turn_id}/cancel")
def cancel_call_chat_turn(turn_id: str):
    return {"cancelled": chat_service.cancel(turn_id)}


@router.websocket("/call-chats/turns/{turn_id}/events")
async def ws_call_chat_turn_events(websocket: WebSocket, turn_id: str):
    """Stream a chat turn's events (backlog + live tail) until it finishes."""
    await websocket.accept()
    try:
        if not chat_service.has_state(turn_id):
            # No event state: the turn already finished and its state was swept
            # (the persisted CallChat is the record — the frontend loads it on
            # reopen), or the turn_id is unknown. Either way this isn't a
            # failure, so finalize the client cleanly instead of stamping a
            # spurious "[turn failed] turn not found" over a turn that may well
            # have succeeded. (A turn finishing before its WS attaches is a tiny
            # race — state lingers briefly past completion so it's near-
            # impossible, but if it happens the persisted transcript is truth.)
            await websocket.send_json({
                "type": "run_finished", "status": "success",
                "error": None, "messages": [], "usage": {}, "cost": 0.0,
            })
            return
        async for event in chat_service.subscribe(turn_id):
            await websocket.send_json(event)
    except WebSocketDisconnect:
        return
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        try:
            await websocket.send_json({"type": "error", "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
        return
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
