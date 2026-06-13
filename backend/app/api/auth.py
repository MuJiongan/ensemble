"""OAuth endpoints for subscription-based LLM providers.

The frontend kicks off a login by POSTing to ``/api/auth/{provider}/start``,
opens the returned ``authorize_url`` in a popup, then polls
``/api/auth/{provider}/status`` until the loopback server captures the
callback and the backend exchanges the code for tokens. ``/logout`` clears
the stored credential.

Supported providers: ``codex`` (ChatGPT subscription), ``xai`` (xAI).
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.db import get_db, SessionLocal
from app.auth import codex, xai
from app.auth import state as login_state
from app import models


router = APIRouter(prefix="/api/auth", tags=["auth"])


_PROVIDERS = {
    "codex": codex,
    "xai": xai,
}


class StartResponse(BaseModel):
    authorize_url: str
    status: str  # 'started' | 'already_pending'


class StatusResponse(BaseModel):
    """Reports the state of either an in-flight login OR a persisted creds row.

    ``status`` is one of:
      * ``"signed_in"`` — a valid credential row exists (the user is signed in).
      * ``"pending"``   — login flow is in progress; the frontend should keep polling.
      * ``"error"``     — login failed; ``error`` carries the detail.
      * ``"signed_out"`` — nothing pending, nothing stored.
    """
    status: str
    label: Optional[str] = None
    error: Optional[str] = None


def _provider_or_404(provider: str):
    mod = _PROVIDERS.get(provider)
    if mod is None:
        raise HTTPException(status_code=404, detail=f"unknown auth provider '{provider}'")
    return mod


@router.post("/{provider}/start", response_model=StartResponse)
def start(provider: str) -> StartResponse:
    mod = _provider_or_404(provider)
    try:
        url, status = mod.start_login(SessionLocal)
    except OSError as e:
        # Pinned redirect ports (1455 for Codex, 56121 for xAI) are unbindable
        # — most likely another instance of emdash is running, or another
        # process is squatting on the port. Surface a useful message.
        raise HTTPException(
            status_code=409,
            detail=f"port for {provider} OAuth callback is in use ({e}); close other clients and try again",
        )
    return StartResponse(authorize_url=url, status=status)


@router.get("/{provider}/status", response_model=StatusResponse)
def status(provider: str, db: Session = Depends(get_db)) -> StatusResponse:
    mod = _provider_or_404(provider)
    cred = db.query(models.Credential).filter_by(provider=provider).first()
    if cred is not None:
        # Row existence isn't enough — the stored token may be expired with a
        # refresh that no longer works (revoked, signed out elsewhere).
        # get_active_credential refreshes when near expiry and returns None
        # when the credential is unusable, so the UI can prompt for re-login
        # instead of reporting a stale "signed in".
        active = mod.get_active_credential(db)
        if active is not None:
            return StatusResponse(status="signed_in", label=active.label)
        return StatusResponse(status="signed_out", error="session expired — sign in again")
    s = login_state.get(provider)
    if s is None:
        return StatusResponse(status="signed_out")
    if s.status == "complete":
        # Worker finished but row not yet read by us above — race-window
        # cleanup. Treat as signed_out and let the next poll find the row.
        return StatusResponse(status="signed_out")
    if s.status == "error":
        return StatusResponse(status="error", error=s.error)
    return StatusResponse(status="pending")


@router.post("/{provider}/logout", response_model=StatusResponse)
def logout(provider: str, db: Session = Depends(get_db)) -> StatusResponse:
    mod = _provider_or_404(provider)
    mod.logout(db)
    return StatusResponse(status="signed_out")


@router.post("/{provider}/cancel", response_model=StatusResponse)
def cancel(provider: str) -> StatusResponse:
    """Mark an in-flight login as cancelled. The loopback server keeps
    waiting for its timeout in the background, but the UI no longer polls."""
    _provider_or_404(provider)
    s = login_state.get(provider)
    if s and s.status == "pending":
        login_state.update(provider, status="error", error="cancelled")
    return StatusResponse(status="signed_out")
