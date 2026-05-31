"""Resolve OAuth-backed credentials at LLM-call time.

The LLM callers check ``$LLM_PROVIDER_ID`` per request:

  * ``"codex"`` — return the active ChatGPT/Codex OAuth credential (refreshed
    if needed). The caller routes the request through the Codex Responses
    API.
  * ``"xai"``   — return the active xAI OAuth credential. The caller uses it
    as a bearer against the standard chat-completions endpoint.
  * anything else — return ``None``; caller falls back to the API-key path.

This module is also used by the runner subprocess spawner so the child
process gets a fresh OAuth bearer in its env at spawn time (refreshing
once before the worker starts, rather than threading DB access into the
subprocess).
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.db import SessionLocal


@dataclass(frozen=True)
class ActiveCreds:
    provider: str           # 'codex' | 'xai'
    access_token: str
    account_id: Optional[str]  # only set for codex


def current_provider_id() -> str:
    """Provider id forwarded from the frontend (``X-Llm-Provider-Id``) or
    empty if the user is on an api-key preset."""
    return (os.getenv("LLM_PROVIDER_ID") or "").strip()


def resolve(provider_id: str, db: Optional[Session] = None) -> Optional[ActiveCreds]:
    """Return refreshed credentials for an OAuth provider, or ``None`` if the
    user hasn't signed in (or refresh failed)."""
    if provider_id not in ("codex", "xai"):
        return None
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        if provider_id == "codex":
            from app.auth import codex
            cred = codex.get_active_credential(db)
        else:
            from app.auth import xai
            cred = xai.get_active_credential(db)
        if cred is None:
            return None
        return ActiveCreds(
            provider=provider_id,
            access_token=cred.access_token,
            account_id=cred.account_id,
        )
    finally:
        if owns_session:
            db.close()
