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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.db import SessionLocal, get_db
from app.auth import mcp_oauth
from app.auth import state as login_state
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


class LoginStatusResponse(BaseModel):
    """State of a server's login / stored credential.

    ``status``: ``signed_in`` (a credential row exists), ``pending`` (login in
    flight), ``error`` (last attempt failed), ``signed_out`` (nothing).
    """
    status: str
    error: Optional[str] = None


@router.post("/{server}/login/start", response_model=LoginStartResponse)
def login_start(server: str, req: LoginStartRequest) -> LoginStartResponse:
    try:
        url, status_str = mcp_oauth.start_login(server, req.url, req.oauth, SessionLocal)
    except OSError as e:
        raise HTTPException(
            status_code=409,
            detail=f"MCP OAuth callback port {mcp_runner.MCP_OAUTH_PORT} is in use ({e}); close other clients and try again",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not start MCP login: {e}")
    return LoginStartResponse(authorize_url=url, status=status_str)


@router.get("/{server}/login/status", response_model=LoginStatusResponse)
def login_status(server: str, db: Session = Depends(get_db)) -> LoginStatusResponse:
    row = db.query(models.McpCredential).filter_by(server_name=server).first()
    if row is not None and row.access_token:
        # An expired token with no refresh token is unusable — report it as
        # signed-out so the UI prompts for re-login instead of "authorized".
        expired = row.expires_at is not None and row.expires_at <= datetime.utcnow()
        if not expired or row.refresh_token:
            return LoginStatusResponse(status="signed_in")
        return LoginStatusResponse(status="signed_out")
    s = login_state.get(mcp_oauth.state_key(server))
    if s is None:
        return LoginStatusResponse(status="signed_out")
    if s.status == "complete":
        return LoginStatusResponse(status="signed_out")
    if s.status == "error":
        return LoginStatusResponse(status="error", error=s.error)
    return LoginStatusResponse(status="pending")


@router.post("/{server}/login/cancel", response_model=LoginStatusResponse)
def login_cancel(server: str) -> LoginStatusResponse:
    s = login_state.get(mcp_oauth.state_key(server))
    if s and s.status == "pending":
        login_state.update(mcp_oauth.state_key(server), status="error", error="cancelled")
    return LoginStatusResponse(status="signed_out")


@router.post("/{server}/logout", response_model=LoginStatusResponse)
def logout(server: str, db: Session = Depends(get_db)) -> LoginStatusResponse:
    mcp_oauth.logout(server, db)
    return LoginStatusResponse(status="signed_out")
