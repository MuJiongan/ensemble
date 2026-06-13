"""Interactive OAuth login for remote MCP servers.

Mirrors :mod:`app.auth.codex`'s start/status/cancel/logout shape, but the
actual OAuth dance is driven by the MCP SDK's ``OAuthClientProvider`` rather
than hand-rolled requests: it handles discovery, dynamic client registration
(RFC 7591), PKCE, the code exchange, and token persistence (via our
``DbTokenStorage``). We only supply the two interactive hooks the provider
needs — a ``redirect_handler`` that surfaces the authorize URL to the frontend,
and a ``callback_handler`` that awaits the loopback redirect.

State is keyed ``mcp:<server_name>`` in the shared :mod:`app.auth.state`
tracker, so one server's login can't collide with another's (or with the LLM
provider logins).
"""
from __future__ import annotations
import asyncio
import threading
import time
from typing import Optional

from sqlalchemy.orm import Session

from app import models
from app.auth import state as login_state
from app.auth.oauth import LoopbackCallbackServer
from app.runner.mcp import (
    MCP_OAUTH_CALLBACK_PATH,
    MCP_OAUTH_PORT,
    OAuthConfig,
    ServerConfig,
    _format_connect_error,
    build_oauth_provider,
    effective_redirect_uri,
    invalidate_discovery_cache,
)


CALLBACK_TIMEOUT_SECS = 5 * 60
# How long /start blocks waiting for the provider to produce an authorize URL.
AUTHORIZE_URL_TIMEOUT_SECS = 30
_CALLBACK_HOST = "127.0.0.1"


def _loopback_params(oauth_cfg: OAuthConfig) -> tuple[str, int, str] | None:
    """Resolve loopback (host, port, path) from the OAuth config, or None when
    the configured redirect URI is non-loopback (HTTPS tunnel etc.) and we
    can't catch the callback ourselves."""
    from urllib.parse import urlparse

    parsed = urlparse(effective_redirect_uri(oauth_cfg))
    host = parsed.hostname or _CALLBACK_HOST
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    port = parsed.port or MCP_OAUTH_PORT
    path = parsed.path or MCP_OAUTH_CALLBACK_PATH
    return host, port, path


def state_key(server_name: str) -> str:
    return f"mcp:{server_name}"


async def _run_login(
    cfg: ServerConfig,
    db_factory,
    server: LoopbackCallbackServer,
    url_box: dict,
) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def redirect_handler(authorize_url: str) -> None:
        url_box["url"] = authorize_url
        url_box["event"].set()

    async def callback_handler() -> tuple[str, Optional[str]]:
        result = await asyncio.to_thread(server.wait, CALLBACK_TIMEOUT_SECS)
        if result is None:
            raise RuntimeError("sign-in timed out")
        if result.error:
            raise RuntimeError(result.error)
        if not result.code:
            raise RuntimeError("no authorization code in callback")
        return result.code, result.state

    provider = build_oauth_provider(
        cfg, db_factory, redirect_handler=redirect_handler, callback_handler=callback_handler
    )
    async with streamablehttp_client(cfg.url, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            # The first request 401s, which kicks the provider into the full
            # OAuth flow (browser redirect → callback → token exchange). On
            # return we're authenticated; initialize confirms the session.
            await session.initialize()


def _login_worker(
    cfg: ServerConfig,
    db_factory,
    server: LoopbackCallbackServer,
    owner: login_state.LoginState,
    url_box: dict,
    key: str,
) -> None:
    try:
        asyncio.run(_run_login(cfg, db_factory, server, url_box))
        # Cached discovery may have run while this server was signed out
        # (zero tools) — drop it so the orchestrator re-advertises next turn.
        invalidate_discovery_cache()
        login_state.update_if_owner(key, owner, status="complete", label=cfg.name)
    except Exception as e:
        # If we failed before producing the authorize URL, unblock /start.
        # _format_connect_error trims the HTML blob the SDK includes when DCR
        # 404s (Slack et al.) down to an actionable one-liner.
        err = _format_connect_error(e)
        if not url_box["event"].is_set():
            url_box["error"] = err
            url_box["event"].set()
        login_state.update_if_owner(key, owner, status="error", error=err)
    finally:
        server.stop()


def start_login(
    server_name: str, server_url: str, oauth_cfg: Optional[dict], db_factory
) -> tuple[str, str]:
    """Begin (or restart) an MCP server OAuth login.

    Blocks briefly until the SDK provider produces the authorize URL, then
    returns it for the frontend to open in a popup. Raises on bind failure
    (pinned callback port in use) or if the URL can't be produced.
    """
    key = state_key(server_name)
    login_state.reset(key)
    raw = oauth_cfg or {}
    port_raw = raw.get("callbackPort") or raw.get("callback_port") or 0
    try:
        callback_port = int(port_raw) if port_raw else 0
    except (TypeError, ValueError):
        callback_port = 0
    oauth = OAuthConfig(
        client_id=str(raw.get("clientId") or raw.get("client_id") or ""),
        client_secret=str(raw.get("clientSecret") or raw.get("client_secret") or ""),
        scope=str(raw.get("scope") or ""),
        redirect_uri=str(raw.get("redirectUri") or raw.get("redirect_uri") or ""),
        callback_port=callback_port,
    )
    cfg = ServerConfig(name=server_name, type="remote", url=server_url, oauth=oauth)
    loopback = _loopback_params(oauth)
    if loopback is None:
        # Non-loopback redirect (e.g. an HTTPS tunnel): we have no way to catch
        # the callback from here. Tell the user up-front rather than spinning
        # up a doomed flow.
        raise RuntimeError(
            "redirectUri must be a loopback URL (http://127.0.0.1:... or http://localhost:...) "
            "for emdash to catch the OAuth callback"
        )
    server = LoopbackCallbackServer(*loopback)
    server.start()
    url_box: dict = {"url": None, "error": None, "event": threading.Event()}
    my_state = login_state.LoginState(status="pending", started_at=time.time(), server=server)
    login_state.claim(key, my_state)
    t = threading.Thread(
        target=_login_worker,
        args=(cfg, db_factory, server, my_state, url_box, key),
        name=f"mcp-oauth-{server_name}",
        daemon=True,
    )
    my_state.thread = t
    t.start()

    if not url_box["event"].wait(timeout=AUTHORIZE_URL_TIMEOUT_SECS):
        login_state.update_if_owner(key, my_state, status="error", error="failed to obtain authorize url")
        server.stop()
        raise RuntimeError("timed out obtaining authorize url from MCP server")
    if not url_box["url"]:
        raise RuntimeError(url_box.get("error") or "authorize url unavailable")
    return url_box["url"], "started"


def logout(server_name: str, db: Session) -> bool:
    invalidate_discovery_cache()
    row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
    if row is None:
        login_state.clear(state_key(server_name))
        return False
    db.delete(row)
    db.commit()
    login_state.clear(state_key(server_name))
    return True
