"""Interactive OAuth login for remote MCP servers.

The MCP SDK owns discovery, DCR, PKCE, state validation, resource binding and
token exchange. This module owns the interactive callback transport:

* local/default flows bind an isolated OS-assigned loopback port;
* explicitly configured loopback redirects keep their fixed endpoint;
* deployments with ``PUBLIC_BASE_URL`` use one trusted HTTPS FastAPI callback
  that dispatches concurrent flows by the SDK-generated OAuth state.

Every attempt has one callback receiver and one state owner. Cancellation stops
that receiver immediately and rolls back any attempt-scoped credential writes,
so a late callback cannot complete or replace stored credentials.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from app import models
from app.auth import state as login_state
from app.auth.oauth import CallbackResult, CallbackWaiter, LoopbackCallbackServer
from app.runner.mcp import (
    MCP_OAUTH_CALLBACK_PATH,
    OAuthConfig,
    ServerConfig,
    _format_connect_error,
    build_oauth_provider,
    invalidate_discovery_cache,
)


CALLBACK_TIMEOUT_SECS = 5 * 60
# How long /start blocks waiting for the provider to produce an authorize URL.
AUTHORIZE_URL_TIMEOUT_SECS = 30
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
PUBLIC_CALLBACK_PATH = "/api/mcp/oauth/callback"
_CALLBACK_HOST = "127.0.0.1"


class CallbackEndpointBusy(RuntimeError):
    """A configured fixed callback could not be bound."""

    def __init__(self, endpoint: str, owner: Optional[str], cause: OSError):
        self.endpoint = endpoint
        self.owner = owner
        self.cause = cause
        owner_detail = f" by MCP server '{owner}'" if owner else ""
        super().__init__(
            f"configured MCP OAuth callback {endpoint} is already in use{owner_detail} "
            f"({cause}); cancel that sign-in or choose another callbackPort"
        )


class PublicCallbackError(RuntimeError):
    def __init__(self, detail: str, status_code: int = 400):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


@dataclass(frozen=True)
class _PublicFlow:
    key: str
    owner: login_state.LoginState
    receiver: CallbackWaiter


_PUBLIC_LOCK = threading.Lock()
_PUBLIC_FLOWS: dict[str, _PublicFlow] = {}


def state_key(server_name: str) -> str:
    return f"mcp:{server_name}"


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    normalized = host.lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def configured_public_callback_uri() -> Optional[str]:
    """Return the trusted backend callback derived only from PUBLIC_BASE_URL.

    Arbitrary Host / Forwarded headers are deliberately ignored. Non-loopback
    deployments must use HTTPS; plain HTTP remains available only for explicit
    loopback development bases.
    """
    raw = os.getenv(PUBLIC_BASE_URL_ENV, "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{PUBLIC_BASE_URL_ENV} has an invalid port") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            f"{PUBLIC_BASE_URL_ENV} must be an absolute external base URL "
            "without credentials, query, or fragment"
        )
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and _is_loopback_host(parsed.hostname)
    ):
        raise RuntimeError(
            f"{PUBLIC_BASE_URL_ENV} must use HTTPS (plain HTTP is allowed only for loopback development)"
        )
    return raw.rstrip("/") + PUBLIC_CALLBACK_PATH


def _loopback_params(redirect_uri: str) -> tuple[str, int, str] | None:
    """Resolve an exact HTTP loopback redirect into host, port and path."""
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname):
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "MCP OAuth redirectUri must not contain credentials, a query, or a fragment"
        )
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise RuntimeError("MCP OAuth redirectUri has an invalid port") from exc
    return parsed.hostname or _CALLBACK_HOST, port, parsed.path or MCP_OAUTH_CALLBACK_PATH


def _callback_for(oauth: OAuthConfig) -> tuple[str, CallbackWaiter, str]:
    """Create ``(mode, receiver, exact_redirect_uri)`` for one attempt."""
    explicit_uri = oauth.redirect_uri.strip()
    if explicit_uri:
        loopback = _loopback_params(explicit_uri)
        if loopback is not None:
            server = LoopbackCallbackServer(*loopback, redirect_uri=explicit_uri)
            try:
                server.start()
            except OSError as exc:
                owner_key = login_state.pending_callback_owner(server.redirect_uri)
                owner = owner_key.removeprefix("mcp:") if owner_key else None
                raise CallbackEndpointBusy(server.redirect_uri, owner, exc) from exc
            return "loopback", server, server.redirect_uri

        public_uri = configured_public_callback_uri()
        if public_uri is None:
            raise RuntimeError(
                f"non-loopback redirectUri requires {PUBLIC_BASE_URL_ENV}; "
                "configure the trusted external HTTPS base URL and allowlist its callback"
            )
        if explicit_uri != public_uri:
            raise RuntimeError(
                f"redirectUri must match the trusted backend callback {public_uri}; "
                "allowlist that exact URI with the MCP authorization server"
            )
        return "public", CallbackWaiter(), public_uri

    if oauth.callback_port:
        if not 1 <= oauth.callback_port <= 65535:
            raise RuntimeError("oauth.callbackPort must be between 1 and 65535")
        uri = f"http://{_CALLBACK_HOST}:{oauth.callback_port}{MCP_OAUTH_CALLBACK_PATH}"
        server = LoopbackCallbackServer(
            _CALLBACK_HOST, oauth.callback_port, MCP_OAUTH_CALLBACK_PATH
        )
        try:
            server.start()
        except OSError as exc:
            owner_key = login_state.pending_callback_owner(uri)
            owner = owner_key.removeprefix("mcp:") if owner_key else None
            raise CallbackEndpointBusy(uri, owner, exc) from exc
        return "loopback", server, server.redirect_uri

    public_uri = configured_public_callback_uri()
    if public_uri is not None:
        return "public", CallbackWaiter(), public_uri

    # RFC 8252 native-app default: bind port 0 first, then use the exact
    # OS-assigned port in metadata, DCR, authorization and token exchange.
    server = LoopbackCallbackServer(_CALLBACK_HOST, 0, MCP_OAUTH_CALLBACK_PATH)
    server.start()
    return "loopback", server, server.redirect_uri


def _authorize_state(authorize_url: str) -> str:
    values = parse_qs(urlparse(authorize_url).query).get("state") or []
    if len(values) != 1 or not values[0]:
        raise RuntimeError("MCP authorization URL did not contain exactly one OAuth state")
    return values[0]


def _register_public_flow(
    oauth_state: str,
    key: str,
    owner: login_state.LoginState,
    receiver: CallbackWaiter,
) -> None:
    with _PUBLIC_LOCK:
        if oauth_state in _PUBLIC_FLOWS:
            raise RuntimeError("duplicate MCP OAuth state")
        _PUBLIC_FLOWS[oauth_state] = _PublicFlow(key, owner, receiver)


def _unregister_public_flow(oauth_state: Optional[str], owner: login_state.LoginState) -> None:
    if not oauth_state:
        return
    with _PUBLIC_LOCK:
        flow = _PUBLIC_FLOWS.get(oauth_state)
        if flow is not None and flow.owner is owner:
            _PUBLIC_FLOWS.pop(oauth_state, None)


def deliver_public_callback(
    *, code: Optional[str], state: Optional[str], error: Optional[str]
) -> CallbackResult:
    """Deliver the shared HTTPS callback to exactly one pending MCP flow."""
    if not state:
        raise PublicCallbackError("missing OAuth state")
    if not code and not error:
        raise PublicCallbackError(
            "callback contains neither an authorization code nor an error"
        )

    # Pop before delivery: the callback is single-use even while token exchange
    # is still running, so a replay can never race the first request.
    with _PUBLIC_LOCK:
        flow = _PUBLIC_FLOWS.pop(state, None)
    if flow is None:
        raise PublicCallbackError("unknown, expired, or already-used OAuth state", 409)
    if time.time() - flow.owner.started_at > CALLBACK_TIMEOUT_SECS:
        flow.receiver.stop()
        raise PublicCallbackError("OAuth sign-in has expired", 410)
    result = CallbackResult(code=code, state=state, error=error)
    accepted = False

    def deliver() -> None:
        nonlocal accepted
        accepted = flow.receiver.deliver(result)

    if not login_state.run_if_pending_owner(flow.key, flow.owner, deliver):
        raise PublicCallbackError("OAuth sign-in is no longer pending", 409)
    if not accepted:
        raise PublicCallbackError("OAuth callback was already received", 409)
    return result


def _credential_snapshot(server_name: str, db_factory) -> Optional[dict]:
    db = db_factory()
    try:
        row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
        if row is None:
            return None
        return {
            column.name: getattr(row, column.name)
            for column in models.McpCredential.__table__.columns
        }
    finally:
        db.close()


def _restore_credential(server_name: str, snapshot: Optional[dict], db_factory) -> None:
    db = db_factory()
    try:
        row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
        if snapshot is None:
            if row is not None:
                db.delete(row)
        else:
            if row is None:
                row = models.McpCredential(
                    server_name=server_name,
                    server_url=str(snapshot.get("server_url") or ""),
                )
                db.add(row)
            for field, value in snapshot.items():
                setattr(row, field, value)
        db.commit()
    finally:
        db.close()


async def _run_login(
    cfg: ServerConfig,
    db_factory,
    receiver: CallbackWaiter,
    owner: login_state.LoginState,
    url_box: dict,
    flow_box: dict,
    key: str,
    callback_mode: str,
) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def redirect_handler(authorize_url: str) -> None:
        if not login_state.is_pending_owner(key, owner):
            raise RuntimeError("sign-in cancelled")
        if callback_mode == "public":
            oauth_state = _authorize_state(authorize_url)
            _register_public_flow(oauth_state, key, owner, receiver)
            flow_box["state"] = oauth_state
        url_box["url"] = authorize_url
        url_box["event"].set()

    async def callback_handler() -> tuple[str, Optional[str]]:
        result = await asyncio.to_thread(receiver.wait, CALLBACK_TIMEOUT_SECS)
        if result is None:
            if not login_state.is_pending_owner(key, owner):
                raise RuntimeError("sign-in cancelled")
            raise RuntimeError("sign-in timed out")
        if result.error:
            raise RuntimeError(result.error)
        if not result.code:
            raise RuntimeError("no authorization code in callback")
        return result.code, result.state

    provider = build_oauth_provider(
        cfg,
        db_factory,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        force_authorization=True,
        write_guard=lambda action: login_state.run_if_pending_owner(key, owner, action),
    )
    async with streamablehttp_client(cfg.url, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            # The first request 401s, which kicks the provider into the full
            # OAuth flow. On return, initialize confirms the authenticated MCP
            # session before the attempt can transition to complete.
            await session.initialize()


def _login_worker(
    cfg: ServerConfig,
    db_factory,
    receiver: CallbackWaiter,
    owner: login_state.LoginState,
    url_box: dict,
    flow_box: dict,
    key: str,
    callback_mode: str,
) -> None:
    try:
        asyncio.run(
            _run_login(
                cfg,
                db_factory,
                receiver,
                owner,
                url_box,
                flow_box,
                key,
                callback_mode,
            )
        )
        if login_state.update_if_pending_owner(
            key, owner, status="complete", label=cfg.name
        ):
            # Cached discovery may have run while signed out (zero tools).
            invalidate_discovery_cache()
    except Exception as exc:
        err = _format_connect_error(exc)
        if not url_box["event"].is_set():
            url_box["error"] = err
            url_box["event"].set()
        if login_state.update_if_pending_owner(key, owner, status="error", error=err):
            # DCR client information may have been written before a later
            # failure. Restore the pre-attempt row so a failed grant is atomic.
            if owner.on_cancel is not None:
                owner.on_cancel()
    finally:
        _unregister_public_flow(flow_box.get("state"), owner)
        receiver.stop()


def start_login(
    server_name: str, server_url: str, oauth_cfg: Optional[dict], db_factory
) -> tuple[str, str]:
    """Begin (or restart) an MCP server OAuth login."""
    key = state_key(server_name)
    login_state.reset(key)
    raw = oauth_cfg or {}
    port_raw = raw.get("callbackPort") or raw.get("callback_port") or 0
    try:
        callback_port = int(port_raw) if port_raw else 0
    except (TypeError, ValueError) as exc:
        raise RuntimeError("oauth.callbackPort must be an integer") from exc
    oauth = OAuthConfig(
        client_id=str(raw.get("clientId") or raw.get("client_id") or ""),
        client_secret=str(raw.get("clientSecret") or raw.get("client_secret") or ""),
        scope=str(raw.get("scope") or ""),
        redirect_uri=str(raw.get("redirectUri") or raw.get("redirect_uri") or ""),
        callback_port=callback_port,
    )

    callback_mode, receiver, redirect_uri = _callback_for(oauth)
    # The server must bind port 0 before this assignment. From here onward the
    # SDK metadata, DCR/client info, authorization request and exchange all see
    # this exact URI.
    oauth.redirect_uri = redirect_uri
    oauth.callback_port = 0
    cfg = ServerConfig(name=server_name, type="remote", url=server_url, oauth=oauth)

    try:
        snapshot = _credential_snapshot(server_name, db_factory)
    except Exception:
        receiver.stop()
        raise
    flow_box: dict = {"state": None}
    rollback_lock = threading.Lock()
    rollback_done = False

    def rollback() -> None:
        nonlocal rollback_done
        with rollback_lock:
            if rollback_done:
                return
            rollback_done = True
        _unregister_public_flow(flow_box.get("state"), my_state)
        _restore_credential(server_name, snapshot, db_factory)

    url_box: dict = {"url": None, "error": None, "event": threading.Event()}
    my_state = login_state.LoginState(
        status="pending",
        started_at=time.time(),
        server=receiver,
        callback_uri=redirect_uri,
        on_cancel=rollback,
    )
    if not login_state.claim(key, my_state):
        receiver.stop()
        raise RuntimeError(f"an MCP sign-in for '{server_name}' is already pending")

    thread = threading.Thread(
        target=_login_worker,
        args=(
            cfg,
            db_factory,
            receiver,
            my_state,
            url_box,
            flow_box,
            key,
            callback_mode,
        ),
        name=f"mcp-oauth-{server_name}",
        daemon=True,
    )
    my_state.thread = thread
    try:
        thread.start()
    except Exception:
        login_state.cancel(key, error="failed to start MCP OAuth worker")
        raise

    if not url_box["event"].wait(timeout=AUTHORIZE_URL_TIMEOUT_SECS):
        detail = "timed out obtaining authorize url from MCP server"
        login_state.cancel(key, error=detail)
        raise RuntimeError(detail)
    if not url_box["url"]:
        raise RuntimeError(url_box.get("error") or "authorize url unavailable")
    return url_box["url"], "started"


def callback_mode(server_name: str) -> Optional[str]:
    state = login_state.get(state_key(server_name))
    if state is None or not state.callback_uri:
        return None
    return "loopback" if isinstance(state.server, LoopbackCallbackServer) else "public"


def cancel(server_name: str) -> bool:
    return login_state.cancel(state_key(server_name), error="cancelled")


def logout(server_name: str, db: Session) -> bool:
    cancel(server_name)
    invalidate_discovery_cache()
    row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
    if row is None:
        login_state.clear(state_key(server_name))
        return False
    db.delete(row)
    db.commit()
    login_state.clear(state_key(server_name))
    return True
