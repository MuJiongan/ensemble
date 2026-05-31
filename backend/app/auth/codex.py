"""Codex / ChatGPT subscription OAuth — PKCE + authorization-code flow.

This is the same flow Codex CLI uses; we reuse its published client_id
(``app_EMoamEEZ73f0CkXaXp7hrann``) so the loopback redirect URI matches what
``auth.openai.com`` already has registered. The redirect URI is pinned to
``http://localhost:1455/auth/callback``.

After exchanging the authorization code we extract the user's ChatGPT account
id from the returned ``id_token`` JWT claims — every subsequent LLM request
hits ``https://chatgpt.com/backend-api/codex/responses`` with the access token
*and* ``ChatGPT-Account-Id: <account_id>``. Billing flows to the user's
ChatGPT Pro/Plus/Team subscription.

This module mirrors :mod:`app.auth.xai` — same surface, different endpoints.
"""
from __future__ import annotations
import base64
import json
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


PROVIDER = "codex"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
SCOPE = "openid profile email offline_access"

OAUTH_HOST = "localhost"
OAUTH_PORT = 1455
OAUTH_PATH = "/auth/callback"
REDIRECT_URI = f"http://{OAUTH_HOST}:{OAUTH_PORT}{OAUTH_PATH}"

CALLBACK_TIMEOUT_SECS = 5 * 60
REFRESH_SKEW_SECS = 120

# The endpoint that bills against the user's ChatGPT subscription.
CHAT_API_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

log = logging.getLogger(__name__)


def build_authorize_url(pkce: PkceCodes, state_value: str) -> str:
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state_value,
            "originator": "emdash",
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
        raise RuntimeError(f"Codex token exchange failed ({r.status_code}): {r.text[:300]}")
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
        raise RuntimeError(f"Codex token refresh failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def _decode_jwt_claims(token: str) -> dict:
    """Decode the payload of a JWT without verifying the signature — we trust
    OpenAI's token endpoint to have produced it. Returns ``{}`` on failure.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def _extract_account_id(tokens: dict) -> Optional[str]:
    """Mirror opencode's lookup chain. Account-id may live on the id_token or
    the access_token JWT, and it may be either a top-level claim, an
    organizations entry, or under the namespaced ``https://api.openai.com/auth``
    extension claim."""
    def _from(claims: dict) -> Optional[str]:
        if not claims:
            return None
        if claims.get("chatgpt_account_id"):
            return claims["chatgpt_account_id"]
        ext = claims.get("https://api.openai.com/auth") or {}
        if isinstance(ext, dict) and ext.get("chatgpt_account_id"):
            return ext["chatgpt_account_id"]
        orgs = claims.get("organizations") or []
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
            oid = orgs[0].get("id")
            if oid:
                return oid
        return None

    for key in ("id_token", "access_token"):
        token = tokens.get(key)
        if token:
            account_id = _from(_decode_jwt_claims(token))
            if account_id:
                return account_id
    return None


def _extract_email(tokens: dict) -> Optional[str]:
    """Pull the user's email out of the id_token, for the UI's 'signed in as'."""
    id_token = tokens.get("id_token")
    if not id_token:
        return None
    claims = _decode_jwt_claims(id_token)
    return claims.get("email") if isinstance(claims.get("email"), str) else None


def _persist(db: Session, tokens: dict, previous_account_id: Optional[str] = None) -> None:
    expires_in = int(tokens.get("expires_in") or 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    account_id = _extract_account_id(tokens) or previous_account_id
    email = _extract_email(tokens)
    cred = db.query(models.Credential).filter_by(provider=PROVIDER).first()
    if cred is None:
        cred = models.Credential(
            provider=PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or "",
            expires_at=expires_at,
            account_id=account_id,
            label=email,
        )
        db.add(cred)
    else:
        cred.access_token = tokens["access_token"]
        if tokens.get("refresh_token"):
            cred.refresh_token = tokens["refresh_token"]
        cred.expires_at = expires_at
        if account_id:
            cred.account_id = account_id
        if email:
            cred.label = email
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
        label = _extract_email(tokens) or "ChatGPT account"
        login_state.update_if_owner(PROVIDER, owner, status="complete", label=label)
    except Exception as e:
        login_state.update_if_owner(PROVIDER, owner, status="error", error=str(e))
    finally:
        server.stop()


def start_login(db_factory) -> tuple[str, str]:
    """Begin (or restart) a Codex OAuth login.

    A re-click on Sign in always starts fresh — we shut down any prior
    attempt's loopback server first so the pinned redirect port (1455) is
    available, and the user isn't trapped behind a stale 5-minute timeout.
    """
    login_state.reset(PROVIDER)
    pkce = generate_pkce()
    state_value = generate_state()
    server = LoopbackCallbackServer(OAUTH_HOST, OAUTH_PORT, OAUTH_PATH)
    server.start()
    url = build_authorize_url(pkce, state_value)
    my_state = login_state.LoginState(
        status="pending", started_at=time.time(), server=server
    )
    login_state.claim(PROVIDER, my_state)
    t = threading.Thread(
        target=_login_worker,
        args=(db_factory, pkce, state_value, server, my_state),
        name="codex-oauth-worker",
        daemon=True,
    )
    my_state.thread = t
    t.start()
    return url, "started"


def get_active_credential(db: Session) -> Optional[models.Credential]:
    cred = db.query(models.Credential).filter_by(provider=PROVIDER).first()
    if cred is None:
        return None
    if cred.expires_at - timedelta(seconds=REFRESH_SKEW_SECS) <= datetime.utcnow():
        try:
            tokens = _refresh(cred.refresh_token)
        except Exception as e:
            log.warning("codex token refresh failed: %s", e)
            return None
        _persist(db, tokens, previous_account_id=cred.account_id)
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
