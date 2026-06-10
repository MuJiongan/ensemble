"""Message persistence + history → OpenRouter chat-shape conversion."""
from __future__ import annotations
import base64
import binascii
import json
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app import compaction, models

# A persisted compaction anchor is a system message tagged with this name. Its
# ``content`` holds the anchored summary; its ``tool_call_id`` column is reused
# to point at the first message of the verbatim tail (``None`` => no tail kept,
# the summary stands in for everything before it). See ``_history_messages``.
COMPACTION_MARKER = "__compaction__"

# A user message with attachments stores its ``content`` as a JSON OpenAI-style
# parts array ([{type: text}, {type: image_url}, {type: file}, ...]) and is
# tagged with this name (the ``name`` column is otherwise unused on user
# rows). Plain-text user rows stay raw strings, so old sessions replay as-is.
USER_PARTS_MARKER = "__parts__"


def _is_self_contained_reasoning_block(rd: Any) -> bool:
    """A reasoning block is portable across turns only if it carries its own
    content. Pure server-side pointers (e.g. OpenAI Responses ``rs_…`` ids
    with no inline text) reference items the provider only retains when
    ``store: true`` — we don't set that, so echoing the id back yields a 400
    on the next turn from any provider that tries to dereference it.
    """
    if not isinstance(rd, dict):
        return False
    return bool(rd.get("text") or rd.get("data") or rd.get("signature"))


def _persist_user(
    db: DbSession, sid: str, text: str, attachments: list[dict] | None = None
) -> models.Message:
    """Persist a user message. ``attachments`` items are ``{data_url, filename?}``;
    images become ``image_url`` parts, everything else (PDFs, text files)
    becomes an OpenAI-style ``file`` part."""
    if attachments:
        parts: list[dict] = [{"type": "text", "text": text}] if text else []
        for a in attachments:
            url = a.get("data_url") or ""
            if url.startswith("data:image/"):
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                default = "document.pdf" if url.startswith("data:application/pdf") else "file.txt"
                parts.append({
                    "type": "file",
                    "file": {"filename": a.get("filename") or default, "file_data": url},
                })
        m = models.Message(
            session_id=sid, role="user", content=json.dumps(parts), name=USER_PARTS_MARKER
        )
    else:
        m = models.Message(session_id=sid, role="user", content=text)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _raw_user_parts(r: models.Message) -> str | list:
    """A user row's content as stored: the decoded parts array for
    attachment-carrying rows (see ``USER_PARTS_MARKER``), the raw string
    otherwise."""
    if r.name != USER_PARTS_MARKER:
        return r.content or ""
    try:
        parts = json.loads(r.content or "[]")
    except json.JSONDecodeError:
        return r.content or ""
    return parts if isinstance(parts, list) else r.content or ""


def _decode_text_file_data(file_data: str) -> str | None:
    """Decode a ``data:text/...;base64,`` URL to its text, or ``None`` when
    the data URL isn't a text file."""
    head, _, b64 = file_data.partition(";base64,")
    if not head.startswith("data:text/"):
        return None
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def _user_content(r: models.Message) -> str | list:
    """A user row's content for model replay. Text-file attachments are
    inlined as plain text parts, so only images and PDFs survive as binary
    parts for the adapters."""
    content = _raw_user_parts(r)
    if isinstance(content, str):
        return content
    out: list = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "file":
            f = p.get("file") or {}
            text = _decode_text_file_data(f.get("file_data") or "")
            if text is not None:
                name = f.get("filename") or "file.txt"
                out.append({"type": "text", "text": f"<file: {name}>\n{text}"})
                continue
        out.append(p)
    return out


def user_bubble_fields(r: models.Message) -> tuple[str, list[str], list[dict]]:
    """Split a user row into (visible text, image data URLs, file tiles) for
    the chat panel. File tiles are ``{name, kind}`` with kind "pdf"/"txt"."""
    content = _raw_user_parts(r)
    if isinstance(content, str):
        return content, [], []
    text = "".join(
        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
    )
    images = [
        url
        for p in content
        if isinstance(p, dict) and p.get("type") == "image_url"
        if (url := (p.get("image_url") or {}).get("url"))
    ]
    files = []
    for p in content:
        if not (isinstance(p, dict) and p.get("type") == "file"):
            continue
        f = p.get("file") or {}
        is_pdf = (f.get("file_data") or "").startswith("data:application/pdf")
        files.append({
            "name": f.get("filename") or ("document.pdf" if is_pdf else "file.txt"),
            "kind": "pdf" if is_pdf else "txt",
        })
    return text, images, files


def _persist_assistant(
    db: DbSession,
    sid: str,
    content: str,
    tool_calls: list[dict] | None,
    reasoning_details: list[dict] | None = None,
    cost: float = 0.0,
) -> models.Message:
    m = models.Message(
        session_id=sid,
        role="assistant",
        content=content or "",
        tool_calls=tool_calls or [],
        reasoning_details=reasoning_details or [],
        cost=cost or 0.0,
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


def _persist_compaction(
    db: DbSession, sid: str, summary: str, tail_start_id: str | None
) -> models.Message:
    """Persist a compaction anchor. Future turns replay this summary in place
    of everything before ``tail_start_id`` (see ``_history_messages``)."""
    m = models.Message(
        session_id=sid,
        role="system",
        name=COMPACTION_MARKER,
        content=summary or "",
        tool_call_id=tail_start_id,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _is_compaction_marker(r: models.Message) -> bool:
    return r.role == "system" and r.name == COMPACTION_MARKER


def _row_to_message(r: models.Message) -> dict | None:
    """Convert one persisted row to OpenAI-compatible chat shape (or ``None``
    for rows that never go to the model, e.g. compaction markers)."""
    if r.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": r.tool_call_id or "",
            "name": r.name or "",
            "content": r.content or "",
        }
    if r.role == "assistant":
        msg: dict = {"role": "assistant", "content": r.content or ""}
        if r.tool_calls:
            msg["tool_calls"] = r.tool_calls
        # Anthropic / OpenRouter require the original reasoning blocks to
        # be echoed back before any tool result message — they enforce
        # ordering of the assistant's content blocks across turns. Filter
        # to blocks that carry their own content; opaque server-side
        # pointers can't be replayed (see _is_self_contained_reasoning_block).
        if r.reasoning_details:
            rds = [
                rd
                for rd in r.reasoning_details
                if _is_self_contained_reasoning_block(rd)
            ]
            if rds:
                msg["reasoning_details"] = rds
        return msg
    if r.role == "user":
        return {"role": "user", "content": _user_content(r)}
    if r.role == "system":
        return {"role": "system", "content": r.content or ""}
    return None


def _ordered_rows(db: DbSession, sid: str) -> list[models.Message]:
    return (
        db.query(models.Message)
        .filter_by(session_id=sid)
        .order_by(models.Message.ts.asc(), models.Message.id.asc())
        .all()
    )


def _active_rows(rows: list[models.Message]) -> tuple[list[models.Message], str | None]:
    """The rows the model should currently see, after honouring the latest
    compaction anchor, plus that anchor's summary text (``None`` if no
    compaction has happened).

    The anchor row is chronologically the *newest* (it's written at compaction
    time) but semantically sits *before* the verbatim tail it preserved, so we
    locate the tail by the anchor's ``tail_start_id`` rather than by position.
    Everything before that boundary collapses into the summary; marker rows
    themselves are dropped from the replay.
    """
    marker = None
    marker_idx = -1
    for i, r in enumerate(rows):
        if _is_compaction_marker(r):
            marker, marker_idx = r, i
    if marker is None:
        return rows, None

    start = len(rows)
    if marker.tool_call_id:
        for i, r in enumerate(rows):
            if r.id == marker.tool_call_id:
                start = i
                break
    else:
        start = marker_idx + 1
    active = [r for r in rows[start:] if not _is_compaction_marker(r)]
    return active, marker.content or None


def _history_messages(db: DbSession, sid: str) -> list[dict]:
    """Replay persisted messages back in OpenAI-compatible chat shape, with the
    latest compaction anchor (if any) standing in for the summarized prefix."""
    rows = _ordered_rows(db, sid)
    active, summary = _active_rows(rows)
    out: list[dict] = []
    if summary:
        out.append(compaction.summary_message(summary))
    for r in active:
        msg = _row_to_message(r)
        if msg is not None:
            out.append(msg)
    return out
