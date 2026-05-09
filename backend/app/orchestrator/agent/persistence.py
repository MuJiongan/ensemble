"""Message persistence + history → OpenRouter chat-shape conversion."""
from __future__ import annotations
import json
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app import models


def _persist_user(db: DbSession, sid: str, text: str) -> models.Message:
    m = models.Message(session_id=sid, role="user", content=text)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _persist_assistant(
    db: DbSession,
    sid: str,
    content: str,
    tool_calls: list[dict] | None,
    reasoning_details: list[dict] | None = None,
) -> models.Message:
    m = models.Message(
        session_id=sid,
        role="assistant",
        content=content or "",
        tool_calls=tool_calls or [],
        reasoning_details=reasoning_details or [],
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _persist_tool_result(
    db: DbSession,
    sid: str,
    tool_call_id: str,
    name: str,
    result: Any,
) -> models.Message:
    m = models.Message(
        session_id=sid,
        role="tool",
        content=json.dumps(result, default=str),
        tool_call_id=tool_call_id,
        name=name,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _history_messages(db: DbSession, sid: str) -> list[dict]:
    """Replay persisted messages back in OpenRouter chat shape."""
    rows = (
        db.query(models.Message)
        .filter_by(session_id=sid)
        .order_by(models.Message.ts.asc(), models.Message.id.asc())
        .all()
    )
    out: list[dict] = []
    for r in rows:
        if r.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": r.tool_call_id or "",
                    "name": r.name or "",
                    "content": r.content or "",
                }
            )
        elif r.role == "assistant":
            msg: dict = {"role": "assistant", "content": r.content or ""}
            if r.tool_calls:
                msg["tool_calls"] = r.tool_calls
            # Anthropic / OpenRouter require the original reasoning blocks to
            # be echoed back unmodified before any tool result message — they
            # enforce ordering of the assistant's content blocks across turns.
            if r.reasoning_details:
                msg["reasoning_details"] = r.reasoning_details
            out.append(msg)
        elif r.role == "user":
            out.append({"role": "user", "content": r.content or ""})
        elif r.role == "system":
            out.append({"role": "system", "content": r.content or ""})
    return out
