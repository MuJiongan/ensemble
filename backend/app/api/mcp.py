"""MCP server status + OAuth endpoints.

The MCP config lives in the browser (localStorage), so status probes take the
config in the request body rather than reading it from the env. ``POST
/api/mcp/status`` connects to each configured server and reports
connected / needs_auth / failed (+ tool count); the optional ``?server=`` form
probes a single server for the per-card test button.

OAuth login endpoints mirror ``app/api/auth.py`` (start/status/cancel/logout),
keyed by server name.
"""
from __future__ import annotations
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.db import SessionLocal, get_db
from app.auth import mcp_oauth
from app.auth import state as login_state
from app.auth.oauth import LoopbackCallbackServer, callback_html
from app import models
from app.runner import mcp as mcp_runner


router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class StatusRequest(BaseModel):
    mcp_servers: str = ""
    # When set, probe just this one server (per-card "test" button).
    server: Optional[str] = None


class StatusResponse(BaseModel):
    # server name -> {status, tool_count?, error?}
    servers: dict[str, dict]


@router.post("/status", response_model=StatusResponse)
def status(req: StatusRequest) -> StatusResponse:
    result = mcp_runner.probe(req.mcp_servers, db_factory=SessionLocal)
    if req.server is not None:
        result = {req.server: result.get(req.server, {"status": "failed", "error": "not configured"})}
    return StatusResponse(servers=result)


class ToolsRequest(BaseModel):
    mcp_servers: str = ""
    # When set, only return tools for this server.
    server: Optional[str] = None


class McpToolInfo(BaseModel):
    server: str
    server_attr: str
    tool: str
    tool_attr: str
    qualified: str
    description: str
    input_schema: dict


class ToolsResponse(BaseModel):
    tools: list[McpToolInfo]


@router.post("/tools", response_model=ToolsResponse)
def tools(req: ToolsRequest) -> ToolsResponse:
    """Discover every MCP tool the current config exposes — name, description,
    and full input schema. Powers the Settings "view tools" popout."""
    try:
        descriptors = mcp_runner.discover(req.mcp_servers, db_factory=SessionLocal)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"discover failed: {e}")
    out: list[McpToolInfo] = []
    for d in descriptors:
        if req.server is not None and d.server != req.server:
            continue
        out.append(
            McpToolInfo(
                server=d.server,
                server_attr=d.server_attr,
                tool=d.tool,
                tool_attr=d.tool_attr,
                qualified=d.qualified,
                description=d.description or "",
                input_schema=d.input_schema or {},
            )
        )
    return ToolsResponse(tools=out)


# --- OAuth login (per remote server) --------------------------------------


class LoginStartRequest(BaseModel):
    url: str
    oauth: Optional[dict] = None


class LoginStartResponse(BaseModel):
    authorize_url: str
    status: str  # 'started'
    callback_mode: str  # 'loopback' | 'public'


class LoginCallbackRequest(BaseModel):
    url: str


class LoginStatusResponse(BaseModel):
    """State of a server's login / stored credential.

    ``status``: ``signed_in`` (a credential row exists), ``pending`` (login in
    flight), ``error`` (last attempt failed), ``signed_out`` (nothing).
    """
    status: str
    error: Optional[str] = None
    callback_mode: Optional[str] = None


_CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _callback_param(request: Request, name: str) -> Optional[str]:
    # main.py stores and removes this sensitive query before routing so access
    # logs cannot capture the one-time code. Keep a query_params fallback for
    # router-only tests and alternate ASGI embedding.
    scrubbed = request.scope.get("mcp_oauth_callback_params")
    if isinstance(scrubbed, dict):
        value = scrubbed.get(name)
        if isinstance(value, list):
            return str(value[0]) if len(value) == 1 else None
        return str(value) if value is not None else None
    values = request.query_params.getlist(name)
    return values[0] if len(values) == 1 else None


@router.get("/oauth/callback", response_class=HTMLResponse)
def public_oauth_callback(request: Request) -> HTMLResponse:
    """Receive a backend-owned HTTPS OAuth callback for any MCP server.

    OAuth ``state`` is an unguessable, one-time dispatch key. It is registered
    only after the SDK creates the authorization request and remains tied to
    one pending attempt; the SDK independently performs its constant-time state
    validation before exchanging the code.
    """
    code = _callback_param(request, "code")
    state = _callback_param(request, "state")
    error = _callback_param(request, "error_description") or _callback_param(
        request, "error"
    )
    try:
        result = mcp_oauth.deliver_public_callback(code=code, state=state, error=error)
    except mcp_oauth.PublicCallbackError as exc:
        return HTMLResponse(
            callback_html(exc.detail),
            status_code=exc.status_code,
            headers=_CALLBACK_HEADERS,
        )
    return HTMLResponse(
        callback_html(result.error),
        status_code=200,
        headers=_CALLBACK_HEADERS,
    )


@router.post("/{server}/login/start", response_model=LoginStartResponse)
def login_start(server: str, req: LoginStartRequest) -> LoginStartResponse:
    try:
        url, status_str = mcp_oauth.start_login(server, req.url, req.oauth, SessionLocal)
    except mcp_oauth.CallbackEndpointBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except OSError as e:
        raise HTTPException(
            status_code=409,
            detail=f"could not bind an MCP OAuth callback ({e})",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not start MCP login: {e}")
    return LoginStartResponse(
        authorize_url=url,
        status=status_str,
        callback_mode=mcp_oauth.callback_mode(server) or "loopback",
    )


@router.get("/{server}/login/status", response_model=LoginStatusResponse)
def login_status(server: str, db: Session = Depends(get_db)) -> LoginStatusResponse:
    # An explicit re-authentication attempt takes precedence over an older
    # credential row. Otherwise a still-unexpired but revoked token would make
    # the first poll report signed_in and close the browser before the fresh
    # grant finishes.
    state = login_state.get(mcp_oauth.state_key(server))
    if state is not None and state.status == "pending":
        return LoginStatusResponse(
            status="pending", callback_mode=mcp_oauth.callback_mode(server)
        )
    if state is not None and state.status == "error":
        return LoginStatusResponse(
            status="error",
            error=state.error,
            callback_mode=mcp_oauth.callback_mode(server),
        )

    row = db.query(models.McpCredential).filter_by(server_name=server).first()
    if row is not None and row.access_token:
        # An expired token with no refresh token is unusable — report it as
        # signed-out so the UI prompts for re-login instead of "authorized".
        expired = row.expires_at is not None and row.expires_at <= datetime.utcnow()
        if not expired or row.refresh_token:
            return LoginStatusResponse(status="signed_in")
        return LoginStatusResponse(status="signed_out")
    return LoginStatusResponse(status="signed_out")


@router.post("/{server}/login/callback", response_model=LoginStatusResponse)
def login_callback(server: str, req: LoginCallbackRequest) -> LoginStatusResponse:
    """Relay a loopback OAuth callback copied from a remote browser."""
    s = login_state.get(mcp_oauth.state_key(server))
    if s is None or s.status != "pending" or s.server is None:
        raise HTTPException(status_code=409, detail="no MCP sign-in is waiting for a callback")
    if not isinstance(s.server, LoopbackCallbackServer):
        raise HTTPException(
            status_code=409,
            detail="this sign-in uses the automatic public HTTPS callback",
        )
    accepted = False

    def deliver() -> None:
        nonlocal accepted
        accepted = s.server.deliver_callback_url(req.url)

    try:
        active = login_state.run_if_pending_owner(
            mcp_oauth.state_key(server), s, deliver
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not active:
        raise HTTPException(
            status_code=409, detail="this sign-in is no longer pending"
        )
    if not accepted:
        raise HTTPException(status_code=409, detail="this sign-in already received a callback")
    return LoginStatusResponse(status="pending")


@router.post("/{server}/login/cancel", response_model=LoginStatusResponse)
def login_cancel(server: str) -> LoginStatusResponse:
    mcp_oauth.cancel(server)
    return LoginStatusResponse(status="signed_out")


@router.post("/{server}/logout", response_model=LoginStatusResponse)
def logout(server: str, db: Session = Depends(get_db)) -> LoginStatusResponse:
    mcp_oauth.logout(server, db)
    return LoginStatusResponse(status="signed_out")
