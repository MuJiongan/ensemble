"""xAI (Grok) subscription OAuth — PKCE + authorization-code flow.

Reuses the published Grok-CLI client_id (``b1a00492-...``) because xAI rejects
loopback OAuth from non-allowlisted clients. The redirect URI is pinned to
``http://127.0.0.1:56121/callback`` for the same reason. On success the user
gets a bearer token that authenticates standard chat-completions requests to
``https://api.x.ai/v1/chat/completions`` — no new request-shape needed.

This module exposes:

* :func:`start_login` — generate PKCE + state, open the loopback server,
  spawn a worker that completes the exchange when the callback arrives.
* :func:`get_active_credential` — refresh if needed and return a valid
  access token (or ``None``).
* :func:`logout` — clear the stored credential.
"""
from __future__ import annotations
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app import models
from app.auth.oauth import (
    LoopbackCallbackServer,
    PkceCodes,
    generate_pkce,
    generate_state,
)
from app.auth import state as login_state


PROVIDER = "xai"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

OAUTH_HOST = "127.0.0.1"
OAUTH_PORT = 56121
OAUTH_PATH = "/callback"
REDIRECT_URI = f"http://{OAUTH_HOST}:{OAUTH_PORT}{OAUTH_PATH}"

CALLBACK_TIMEOUT_SECS = 5 * 60
REFRESH_SKEW_SECS = 120

log = logging.getLogger(__name__)


def build_authorize_url(pkce: PkceCodes, state: str, nonce: str) -> str:
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
            # `plan=generic` opts into xAI's generic OAuth plan tier — without
            # it, accounts.x.ai rejects loopback OAuth from non-allowlisted
            # clients. `referrer=emdash` is best-effort attribution.
            "plan": "generic",
            "referrer": "emdash",
        }
    )
    return f"{AUTHORIZE_URL}?{params}"


def _exchange_code(code: str, pkce: PkceCodes) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": pkce.verifier,
            },
            headers={"Accept": "application/json"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"xAI token exchange failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def _refresh(refresh_token: str) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            headers={"Accept": "application/json"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"xAI token refresh failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def _persist(db: Session, tokens: dict) -> None:
    """Write/update the credential row for this provider."""
    expires_in = int(tokens.get("expires_in") or 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    cred = db.query(models.Credential).filter_by(provider=PROVIDER).first()
    if cred is None:
        cred = models.Credential(
            provider=PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or "",
            expires_at=expires_at,
        )
        db.add(cred)
    else:
        cred.access_token = tokens["access_token"]
        # Some providers rotate the refresh_token only on auth, not on refresh
        # — keep the existing one if the response omits it.
        if tokens.get("refresh_token"):
            cred.refresh_token = tokens["refresh_token"]
        cred.expires_at = expires_at
    db.commit()


def _login_worker(
    db_factory,
    pkce: PkceCodes,
    state_value: str,
    server: LoopbackCallbackServer,
    owner: login_state.LoginState,
) -> None:
    """``owner`` is this worker's own state row — we only write back to it if
    we're still the owner of the provider slot, so a superseded prior attempt
    can't clobber a fresh one's pending state."""
    try:
        result = server.wait(timeout=CALLBACK_TIMEOUT_SECS)
        if result is None:
            login_state.update_if_owner(
                PROVIDER, owner, status="error", error="sign-in timed out"
            )
            return
        if result.error:
            login_state.update_if_owner(
                PROVIDER, owner, status="error", error=result.error
            )
            return
        if not result.code or result.state != state_value:
            login_state.update_if_owner(
                PROVIDER, owner, status="error", error="invalid callback state"
            )
            return
        tokens = _exchange_code(result.code, pkce)
        db = db_factory()
        try:
            _persist(db, tokens)
        finally:
            db.close()
        login_state.update_if_owner(
            PROVIDER, owner, status="complete", label="xAI account"
        )
    except Exception as e:
        login_state.update_if_owner(PROVIDER, owner, status="error", error=str(e))
    finally:
        server.stop()


def start_login(db_factory) -> tuple[str, str]:
    """Begin (or restart) an xAI OAuth login.

    A re-click on Sign in always starts fresh — we shut down any prior
    attempt's loopback server first so the pinned redirect port (56121) is
    available, and the user isn't trapped behind a stale 5-minute timeout.
    """
    login_state.reset(PROVIDER)
    pkce = generate_pkce()
    state_value = generate_state()
    nonce = generate_state()
    server = LoopbackCallbackServer(OAUTH_HOST, OAUTH_PORT, OAUTH_PATH)
    server.start()
    url = build_authorize_url(pkce, state_value, nonce)
    my_state = login_state.LoginState(
        status="pending", started_at=time.time(), server=server
    )
    login_state.claim(PROVIDER, my_state)
    t = threading.Thread(
        target=_login_worker,
        args=(db_factory, pkce, state_value, server, my_state),
        name="xai-oauth-worker",
        daemon=True,
    )
    my_state.thread = t
    t.start()
    return url, "started"


def get_active_credential(db: Session) -> Optional[models.Credential]:
    """Return a valid credential, refreshing if it's near expiry. ``None`` if
    no credential is stored (i.e. the user hasn't signed in)."""
    cred = db.query(models.Credential).filter_by(provider=PROVIDER).first()
    if cred is None:
        return None
    if cred.expires_at - timedelta(seconds=REFRESH_SKEW_SECS) <= datetime.utcnow():
        try:
            tokens = _refresh(cred.refresh_token)
        except Exception as e:
            log.warning("xai token refresh failed: %s", e)
            return None
        _persist(db, tokens)
        db.refresh(cred)
    return cred


def logout(db: Session) -> bool:
    cred = db.query(models.Credential).filter_by(provider=PROVIDER).first()
    if cred is None:
        return False
    db.delete(cred)
    db.commit()
    login_state.clear(PROVIDER)
    return True
