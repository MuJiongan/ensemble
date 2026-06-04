"""MCP (Model Context Protocol) client integration.

Connects to user-configured MCP servers, discovers their tools, and exposes
them to node code the same way the built-in tools (`shell`, `web_search`,
`web_fetch`) are exposed: as entries in the runner tool ``REGISTRY`` +
``TOOL_SCHEMAS``, callable via ``ctx.call_llm(tools=[...])`` or
``ctx.tools.<name>(...)``.

Config shape mirrors opencode's ``mcp`` block — a JSON object mapping a server
name to either a local (stdio child process) or remote (streamable HTTP)
connection::

    {
      "playwright": {"type": "local", "command": ["npx", "-y", "@playwright/mcp"]},
      "ctx7":       {"type": "remote", "url": "https://mcp.context7.com/mcp"}
    }

Discovered tools are namespaced ``<server>_<tool>`` so two servers can expose a
tool of the same name without colliding, and so the name stays within the
``^[a-zA-Z0-9_-]{1,64}$`` shape OpenAI-style tool calling requires.

The MCP SDK is async; node execution is synchronous and threaded. We bridge by
running every server's session on one background asyncio loop and funnelling
sync calls through ``run_coroutine_threadsafe``. A single long-lived task per
manager owns the connection lifecycle so the anyio cancel scopes that back the
transports are always entered and exited on the same task.
"""
from __future__ import annotations
import asyncio
import concurrent.futures
import json
import os
import re
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


# Connecting + listing tools at startup; generous so a cold `npx` download of a
# server package doesn't trip the timeout.
_CONNECT_TIMEOUT_S = 60.0
# Per tool-call ceiling. MCP tools can be slow (a browser action, a search), so
# this is well above opencode's 5s request default.
_CALL_TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass
class OAuthConfig:
    """OAuth client config for a remote server. All fields optional: with no
    client id we fall back to dynamic client registration (RFC 7591)."""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""


@dataclass
class ServerConfig:
    name: str
    type: str  # "local" | "remote"
    enabled: bool = True
    # local
    command: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    # remote
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # remote OAuth. None = no OAuth (static headers / public server); an
    # OAuthConfig (even empty) = OAuth-capable, matching opencode's
    # `oauth !== false` default for remote servers.
    oauth: OAuthConfig | None = None
    # both — ms, matching opencode's units
    timeout: int | None = None
    # Per-tool opt-outs (raw MCP tool names). Tools listed here are still
    # discovered so the Settings UI can render them with the toggle off, but
    # they're filtered out before reaching the orchestrator's advertisement
    # list and the runtime tool registry — invisible to nodes either way.
    disabled_tools: set[str] = field(default_factory=set)

    @property
    def call_timeout_s(self) -> float:
        if self.timeout and self.timeout > 0:
            return self.timeout / 1000.0
        return _CALL_TIMEOUT_S


def parse_config(raw: str | dict | None) -> dict[str, ServerConfig]:
    """Parse the MCP settings blob into validated, enabled ServerConfigs.

    Tolerant by design: a malformed entry is skipped rather than failing the
    whole run, since this is user-supplied config and one bad server shouldn't
    block the others. Returns only servers that are well-formed and enabled.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        data = raw
    if not isinstance(data, dict):
        return {}
    # Allow either a bare map of servers or an `{"mcp": {...}}` wrapper (the
    # shape opencode's config file uses), so users can paste either.
    if "mcp" in data and isinstance(data["mcp"], dict):
        data = data["mcp"]

    out: dict[str, ServerConfig] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        typ = entry.get("type")
        if entry.get("enabled") is False:
            continue
        disabled_tools_raw = entry.get("disabled_tools") or []
        disabled_tools = {
            str(t) for t in disabled_tools_raw if isinstance(t, str)
        } if isinstance(disabled_tools_raw, list) else set()
        if typ == "local":
            command = entry.get("command")
            if not isinstance(command, list) or not command:
                continue
            out[name] = ServerConfig(
                name=name,
                type="local",
                command=[str(c) for c in command],
                environment={
                    str(k): str(v) for k, v in (entry.get("environment") or {}).items()
                },
                timeout=entry.get("timeout"),
                disabled_tools=disabled_tools,
            )
        elif typ == "remote":
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            out[name] = ServerConfig(
                name=name,
                type="remote",
                url=url,
                headers={
                    str(k): str(v) for k, v in (entry.get("headers") or {}).items()
                },
                oauth=_parse_oauth(entry.get("oauth")),
                timeout=entry.get("timeout"),
                disabled_tools=disabled_tools,
            )
    return out


def _parse_oauth(raw: Any) -> OAuthConfig | None:
    """Map an entry's ``oauth`` field to an OAuthConfig.

    Mirrors opencode's ``oauth !== false`` default: a remote server is
    OAuth-capable unless it explicitly opts out with ``"oauth": false``.
    A dict supplies a pre-registered client; absence still means capable
    (dynamic client registration on first login).
    """
    if raw is False:
        return None
    if isinstance(raw, dict):
        return OAuthConfig(
            client_id=str(raw.get("clientId") or raw.get("client_id") or ""),
            client_secret=str(raw.get("clientSecret") or raw.get("client_secret") or ""),
            scope=str(raw.get("scope") or ""),
        )
    return OAuthConfig()


# ---------------------------------------------------------------------------
# OAuth (remote servers) — API process only (needs DB + loopback callback)
# ---------------------------------------------------------------------------

# Pinned loopback callback for the MCP OAuth redirect. Pinned (not random) so it
# can be pre-registered as a redirect URI and so a second emdash instance fails
# loudly on bind rather than silently hijacking the callback. Mirrors the
# fixed-port approach in app/auth/codex.py.
MCP_OAUTH_PORT = int(os.getenv("MCP_OAUTH_PORT", "19876"))
MCP_OAUTH_CALLBACK_PATH = "/mcp/oauth/callback"
MCP_OAUTH_REDIRECT_URI = f"http://127.0.0.1:{MCP_OAUTH_PORT}{MCP_OAUTH_CALLBACK_PATH}"


def _oauth_imports():
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthToken,
    )

    return (
        OAuthClientProvider,
        TokenStorage,
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthToken,
    )


def _make_db_token_storage(server_name: str, server_url: str, db_factory: Callable):
    """Build a TokenStorage backed by the ``McpCredential`` table.

    Each method opens a short-lived session from ``db_factory`` so nothing is
    held across the async/loop boundary. ``set_client_info`` may run before any
    token exists (dynamic registration), so both setters upsert.
    """
    (_, TokenStorage, OAuthClientInformationFull, _, OAuthToken) = _oauth_imports()
    from app import models

    class DbTokenStorage(TokenStorage):
        async def get_tokens(self) -> Optional["OAuthToken"]:  # type: ignore[name-defined]
            db = db_factory()
            try:
                row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
                if not row or not row.access_token:
                    return None
                expires_in = None
                if row.expires_at:
                    expires_in = max(
                        0, int((row.expires_at - datetime.utcnow()).total_seconds())
                    )
                return OAuthToken(
                    access_token=row.access_token,
                    token_type=row.token_type or "Bearer",
                    expires_in=expires_in,
                    scope=row.scope,
                    refresh_token=row.refresh_token,
                )
            finally:
                db.close()

        async def set_tokens(self, tokens: "OAuthToken") -> None:  # type: ignore[name-defined]
            db = db_factory()
            try:
                row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
                if row is None:
                    row = models.McpCredential(server_name=server_name, server_url=server_url)
                    db.add(row)
                row.access_token = tokens.access_token
                row.token_type = tokens.token_type or "Bearer"
                row.refresh_token = tokens.refresh_token
                row.scope = tokens.scope
                row.expires_at = (
                    datetime.utcnow() + timedelta(seconds=int(tokens.expires_in))
                    if tokens.expires_in is not None
                    else None
                )
                db.commit()
            finally:
                db.close()

        async def get_client_info(self) -> Optional["OAuthClientInformationFull"]:  # type: ignore[name-defined]
            db = db_factory()
            try:
                row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
                if not row or not row.client_id:
                    return None
                # Coerce on read too, so even a row written before this fix
                # gets a usable auth method on the next connect.
                auth_method = row.token_endpoint_auth_method or (
                    "client_secret_post" if row.client_secret else None
                )
                return OAuthClientInformationFull(
                    client_id=row.client_id,
                    client_secret=row.client_secret,
                    client_id_issued_at=row.client_id_issued_at,
                    client_secret_expires_at=row.client_secret_expires_at,
                    token_endpoint_auth_method=auth_method,
                    redirect_uris=[MCP_OAUTH_REDIRECT_URI],
                )
            finally:
                db.close()

        async def set_client_info(self, client_info: "OAuthClientInformationFull") -> None:  # type: ignore[name-defined]
            # Some auth servers (Notion) return a client_secret on DCR but omit
            # token_endpoint_auth_method, which leaves the SDK sending no client
            # auth at the token endpoint and getting a 401. Coerce to
            # "client_secret_post" *on the in-memory object* so the SDK uses it
            # for the immediately-following token exchange — not just on
            # subsequent reads from the DB.
            if client_info.client_secret and not client_info.token_endpoint_auth_method:
                client_info.token_endpoint_auth_method = "client_secret_post"
            db = db_factory()
            try:
                row = db.query(models.McpCredential).filter_by(server_name=server_name).first()
                if row is None:
                    row = models.McpCredential(server_name=server_name, server_url=server_url)
                    db.add(row)
                row.client_id = client_info.client_id
                row.client_secret = client_info.client_secret
                row.client_id_issued_at = client_info.client_id_issued_at
                row.client_secret_expires_at = client_info.client_secret_expires_at
                row.token_endpoint_auth_method = client_info.token_endpoint_auth_method
                db.commit()
            finally:
                db.close()

    return DbTokenStorage()


def build_oauth_provider(
    cfg: ServerConfig,
    db_factory: Callable,
    redirect_handler=None,
    callback_handler=None,
):
    """Construct an ``OAuthClientProvider`` for a remote server.

    Interactive handlers are passed during login (to open the browser + await
    the loopback callback); omitted for silent refresh / connection probes,
    where the provider can still refresh an existing token but cannot start a
    fresh authorization.
    """
    (OAuthClientProvider, _, _, OAuthClientMetadata, _) = _oauth_imports()
    storage = _make_db_token_storage(cfg.name, cfg.url, db_factory)
    metadata = OAuthClientMetadata(
        redirect_uris=[MCP_OAUTH_REDIRECT_URI],
        client_name="emdash",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=(cfg.oauth.scope or None) if cfg.oauth else None,
    )
    kwargs: dict = {}
    if cfg.oauth and cfg.oauth.client_id:
        # Pre-registered client: seed the metadata so the SDK skips dynamic
        # registration. (clientId/secret are surfaced via stored client_info on
        # subsequent connects.)
        metadata.client_name = "emdash"
    return OAuthClientProvider(
        server_url=cfg.url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        **kwargs,
    )


_NON_IDENT_RE = re.compile(r"[^a-z0-9]+")


def _ident(s: str) -> str:
    """Lowercase, valid Python identifier piece (so it can be an attribute on
    ``ctx.tools``). Non-alphanumerics collapse to a single underscore."""
    out = _NON_IDENT_RE.sub("_", s.lower()).strip("_")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "tool"


def _classify_error(e: BaseException) -> str:
    """Map a connect-time exception to a probe status.

    ``needs_auth`` when the server answered with a 401/403 or the SDK raised an
    auth error (no usable credentials yet); ``failed`` otherwise. Best-effort:
    we inspect the exception type name and message rather than importing every
    transport-specific error class.
    """
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "unauthorized" in name or "auth" in name and "error" in name:
        return "needs_auth"
    if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
        return "needs_auth"
    return "failed"


def tool_identifiers(server: str, tool: str) -> tuple[str, str, str]:
    """Derive the clean names a server's tool is exposed under.

    Returns ``(server_attr, tool_attr, qualified)``:
      - ``server_attr`` / ``tool_attr`` drive the dotted direct-call form
        ``ctx.tools.<server_attr>.<tool_attr>(...)``.
      - ``qualified`` is the flat ``<server_attr>_<tool_attr>`` name used for
        the LLM function schema (function names can't contain dots) and as the
        registry key.

    Many servers prefix their tool names with their own name (Notion's
    ``notion-create-pages``); we strip that redundant prefix so the dotted form
    reads as ``notion.create_pages`` rather than ``notion.notion_create_pages``.
    """
    server_attr = _ident(server)
    tool_attr = _ident(tool)
    if tool_attr.startswith(server_attr + "_") and len(tool_attr) > len(server_attr) + 1:
        tool_attr = tool_attr[len(server_attr) + 1 :]
    qualified = f"{server_attr}_{tool_attr}"[:64]
    return server_attr, tool_attr, qualified


# ---------------------------------------------------------------------------
# tool descriptor
# ---------------------------------------------------------------------------


@dataclass
class ToolDescriptor:
    server: str
    tool: str  # raw name as the server knows it
    qualified: str  # flat LLM/registry name: "<server_attr>_<tool_attr>"
    server_attr: str  # dotted direct-call namespace: ctx.tools.<server_attr>
    tool_attr: str  # dotted direct-call method: .<tool_attr>(...)
    description: str
    input_schema: dict

    def schema(self) -> dict:
        """OpenAI-style function schema for this tool."""
        params = dict(self.input_schema or {})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        desc = self.description or f"{self.tool} (via MCP server '{self.server}')"
        return {
            "type": "function",
            "function": {
                "name": self.qualified,
                "description": f"[mcp:{self.server}] {desc}",
                "parameters": params,
            },
        }


def _serialize_result(res: Any) -> dict:
    """Flatten an MCP CallToolResult into a JSON-serialisable dict the LLM /
    node code can consume — text content joined into a string, structured
    content passed through, plus the error flag."""
    texts: list[str] = []
    for block in (getattr(res, "content", None) or []):
        if getattr(block, "type", None) == "text":
            texts.append(getattr(block, "text", "") or "")
        else:
            try:
                texts.append(json.dumps(block.model_dump(mode="json"), default=str))
            except Exception:
                texts.append(str(block))
    out: dict = {
        "content": "\n".join(texts),
        "isError": bool(getattr(res, "isError", False)),
    }
    structured = getattr(res, "structuredContent", None)
    if structured is not None:
        out["structured"] = structured
    return out


# ---------------------------------------------------------------------------
# async connection, driven from a background loop
# ---------------------------------------------------------------------------


class MCPManager:
    """Owns a background asyncio loop and a persistent session per server.

    ``start`` blocks until every configured server has been connected (or
    failed). After that, ``call`` is a synchronous proxy that runs the tool on
    the loop and blocks for the result. ``shutdown`` tears the loop down,
    closing transports (and terminating local stdio child processes) on the
    same task that opened them.
    """

    def __init__(self, db_factory: Callable | None = None) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, Any] = {}
        self._descriptors: dict[str, list[ToolDescriptor]] = {}
        self._status: dict[str, dict] = {}
        self._stop: asyncio.Event | None = None
        self._serve_future: concurrent.futures.Future | None = None
        self._configs: dict[str, ServerConfig] = {}
        # When set, remote OAuth servers connect through an OAuthClientProvider
        # (auto-refresh against the DB). None — e.g. in the runner subprocess —
        # falls back to whatever static headers the config carries.
        self._db_factory = db_factory

    # -- lifecycle ---------------------------------------------------------

    def start(self, configs: dict[str, ServerConfig]) -> dict[str, dict]:
        if not configs:
            return {}
        self._configs = configs
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-loop", daemon=True
        )
        self._thread.start()

        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._serve_future = asyncio.run_coroutine_threadsafe(
            self._serve(configs, ready), self._loop
        )
        try:
            ready.result(timeout=_CONNECT_TIMEOUT_S + 5)
        except Exception as e:  # connect phase blew up entirely
            self._status = {
                name: {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                for name in configs
            }
        return self._status

    async def _serve(
        self, configs: dict[str, ServerConfig], ready: concurrent.futures.Future
    ) -> None:
        self._stop = asyncio.Event()
        async with AsyncExitStack() as stack:
            for name, cfg in configs.items():
                try:
                    session, descriptors = await asyncio.wait_for(
                        self._connect_one(stack, cfg), timeout=_CONNECT_TIMEOUT_S
                    )
                    self._sessions[name] = session
                    self._descriptors[name] = descriptors
                    self._status[name] = {
                        "status": "connected",
                        "tool_count": len(descriptors),
                    }
                except Exception as e:
                    status = _classify_error(e)
                    self._status[name] = {
                        "status": status,
                        "error": (
                            "authentication required"
                            if status == "needs_auth"
                            else f"{type(e).__name__}: {e}"
                        ),
                    }
            if not ready.done():
                ready.set_result(True)
            await self._stop.wait()

    async def _connect_one(
        self, stack: AsyncExitStack, cfg: ServerConfig
    ) -> tuple[Any, list[ToolDescriptor]]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        if cfg.type == "local":
            params = StdioServerParameters(
                command=cfg.command[0],
                args=list(cfg.command[1:]),
                env=cfg.environment or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            # Attach the OAuth provider only when we already hold a usable
            # credential (so it can refresh + authenticate). With no stored
            # credential we connect plainly: an open or static-header server
            # succeeds, while an OAuth-required one answers 401 — which
            # classifies as needs_auth. Attaching a handler-less provider here
            # would instead make the SDK attempt a full interactive grant and
            # raise OAuthFlowError. Detection, not pre-judgement.
            auth = None
            if (
                cfg.oauth is not None
                and self._db_factory is not None
                and _has_usable_credential(cfg, self._db_factory)
            ):
                auth = build_oauth_provider(cfg, self._db_factory)
            transport = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=cfg.headers or None, auth=auth)
            )
            read, write = transport[0], transport[1]

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        descriptors = []
        for t in listed.tools:
            server_attr, tool_attr, qualified = tool_identifiers(cfg.name, t.name)
            descriptors.append(
                ToolDescriptor(
                    server=cfg.name,
                    tool=t.name,
                    qualified=qualified,
                    server_attr=server_attr,
                    tool_attr=tool_attr,
                    description=t.description or "",
                    input_schema=t.inputSchema or {},
                )
            )
        return session, descriptors

    def shutdown(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._stop is not None:
            loop.call_soon_threadsafe(self._stop.set)
        if self._serve_future is not None:
            try:
                self._serve_future.result(timeout=10)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            loop.close()
        except Exception:
            pass
        self._loop = None

    # -- introspection -----------------------------------------------------

    def descriptors(self) -> list[ToolDescriptor]:
        out: list[ToolDescriptor] = []
        for ds in self._descriptors.values():
            out.extend(ds)
        return out

    def status(self) -> dict[str, dict]:
        return dict(self._status)

    # -- calls -------------------------------------------------------------

    def call(self, server: str, tool: str, arguments: dict) -> dict:
        session = self._sessions.get(server)
        if session is None or self._loop is None:
            return {"content": "", "isError": True, "error": f"MCP server '{server}' not connected"}
        cfg = self._configs.get(server)
        timeout = cfg.call_timeout_s if cfg else _CALL_TIMEOUT_S
        coro = session.call_tool(
            tool, arguments or {}, read_timeout_seconds=timedelta(seconds=timeout)
        )
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            res = fut.result(timeout=timeout + 10)
        except Exception as e:
            return {"content": "", "isError": True, "error": f"{type(e).__name__}: {e}"}
        return _serialize_result(res)


def _make_proxy(manager: MCPManager, server: str, tool: str):
    """Build the sync callable registered into the tool REGISTRY. Accepts the
    tool's arguments as keywords (MCP tool inputs are an object schema)."""

    def proxy(**kwargs) -> dict:
        return manager.call(server, tool, kwargs)

    proxy.__name__ = tool_identifiers(server, tool)[2]
    return proxy


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def register_runtime_tools(
    raw_config: str | None,
    registry: dict,
    schemas: dict,
    namespaces: dict | None = None,
) -> MCPManager | None:
    """Connect to configured MCP servers and inject their tools into the given
    runtime ``registry`` (name -> callable) and ``schemas`` (name -> JSON
    schema). Returns the live manager (caller owns shutdown) or None when no
    servers are configured.

    When ``namespaces`` is given, it is populated as
    ``{server_attr: {tool_attr: qualified}}`` so node code can reach a tool by
    the dotted form ``ctx.tools.<server_attr>.<tool_attr>(...)`` in addition to
    the flat ``ctx.tools.<qualified>(...)``.

    Used by the runner subprocess at run start.
    """
    configs = parse_config(raw_config)
    if not configs:
        return None
    manager = MCPManager()
    manager.start(configs)
    for desc in manager.descriptors():
        cfg = configs.get(desc.server)
        if cfg and desc.tool in cfg.disabled_tools:
            continue
        proxy = _make_proxy(manager, desc.server, desc.tool)
        registry[desc.qualified] = proxy
        schemas[desc.qualified] = desc.schema()
        if namespaces is not None:
            namespaces.setdefault(desc.server_attr, {})[desc.tool_attr] = desc.qualified
    return manager


# Discovery cache for the orchestrator's advertise path — keyed on the raw
# config string so repeated turns with unchanged config don't reconnect.
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_CACHE: dict[str, list[ToolDescriptor]] = {}


def discover(raw_config: str | None, db_factory: Callable | None = None) -> list[ToolDescriptor]:
    """Connect, list tools, disconnect — returning tool descriptors for
    advertising to the orchestrator. Cached by config string so we don't spawn
    a fresh server connection on every orchestrator turn.

    ``db_factory`` enables OAuth refresh for remote servers (API process only).
    """
    key = (raw_config or "").strip()
    if not key:
        return []
    with _DISCOVERY_LOCK:
        if key in _DISCOVERY_CACHE:
            return _DISCOVERY_CACHE[key]
    configs = parse_config(key)
    if not configs:
        with _DISCOVERY_LOCK:
            _DISCOVERY_CACHE[key] = []
        return []
    manager = MCPManager(db_factory=db_factory)
    try:
        manager.start(configs)
        descriptors = manager.descriptors()
    finally:
        manager.shutdown()
    with _DISCOVERY_LOCK:
        _DISCOVERY_CACHE[key] = descriptors
    return descriptors


def resolve_oauth_config(raw_config: str | None, db_factory: Callable | None) -> str:
    """Inject a fresh ``Authorization: Bearer`` header into each remote OAuth
    server that has a stored credential, returning the augmented JSON string.

    Used at runner launch (the subprocess has no DB): the API process resolves a
    fresh token here and hands the child static bearer headers. Servers without
    a usable credential are left untouched (they'll surface as needs-auth in the
    UI; a run against them just won't authenticate).
    """
    raw = (raw_config or "").strip()
    if not raw or db_factory is None:
        return raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict):
        return raw
    servers = data["mcp"] if "mcp" in data and isinstance(data["mcp"], dict) else data
    if not isinstance(servers, dict):
        return raw

    configs = parse_config(raw)
    changed = False
    for name, cfg in configs.items():
        if cfg.type != "remote" or cfg.oauth is None:
            continue
        token = _ensure_fresh_token(cfg, db_factory)
        if not token:
            continue
        entry = servers.get(name)
        if not isinstance(entry, dict):
            continue
        headers = dict(entry.get("headers") or {})
        headers["Authorization"] = f"Bearer {token}"
        entry["headers"] = headers
        changed = True
    return json.dumps(data) if changed else raw


def _has_usable_credential(cfg: ServerConfig, db_factory: Callable) -> bool:
    """True when we hold a stored token we can actually use to connect: a
    still-valid access token, or any access token plus a refresh token. Drives
    the probe's needs-auth short-circuit so we never start an interactive grant
    for a server the user hasn't logged into yet."""
    from app import models

    db = db_factory()
    try:
        row = db.query(models.McpCredential).filter_by(server_name=cfg.name).first()
        if not row or not row.access_token:
            return False
        if row.refresh_token:
            return True
        return row.expires_at is None or row.expires_at > datetime.utcnow() + timedelta(seconds=30)
    finally:
        db.close()


def _ensure_fresh_token(cfg: ServerConfig, db_factory: Callable) -> str:
    """Return a valid access token for a remote OAuth server, refreshing if the
    stored one has expired. Empty string when there's no usable credential."""
    from app import models

    db = db_factory()
    try:
        row = db.query(models.McpCredential).filter_by(server_name=cfg.name).first()
        if not row or not row.access_token:
            return ""
        fresh = row.expires_at is None or row.expires_at > datetime.utcnow() + timedelta(seconds=30)
        if fresh:
            return row.access_token
        has_refresh = bool(row.refresh_token)
    finally:
        db.close()

    if not has_refresh:
        # Expired and unrefreshable — hand back nothing; the server will 401.
        return ""

    # Trigger a refresh by connecting through the provider, which persists the
    # rotated token to the DB as a side effect; then re-read it.
    manager = MCPManager(db_factory=db_factory)
    try:
        manager.start({cfg.name: cfg})
    finally:
        manager.shutdown()
    db = db_factory()
    try:
        row = db.query(models.McpCredential).filter_by(server_name=cfg.name).first()
        return row.access_token if row and row.access_token else ""
    finally:
        db.close()


def probe(raw_config: str | None, db_factory: Callable | None = None) -> dict[str, dict]:
    """Connect to each configured server, returning per-server status for the
    Settings UI.

    Result maps server name to one of:
      - ``{"status": "connected", "tool_count": N}``
      - ``{"status": "needs_auth", "error": "..."}`` (401/403 / auth error)
      - ``{"status": "failed", "error": "..."}``

    Unlike ``discover`` this is uncached: it's driven by an explicit user action
    (Settings open / test button) and should reflect the live server state. It
    also reports disabled servers, which ``parse_config`` filters out, so the UI
    can show them as such.
    """
    if not raw_config or not raw_config.strip():
        return {}
    configs = parse_config(raw_config)
    out: dict[str, dict] = {}
    if configs:
        manager = MCPManager(db_factory=db_factory)
        try:
            out.update(manager.start(configs))
        finally:
            manager.shutdown()
    # Surface explicitly-disabled servers (dropped by parse_config) so the card
    # can render a "disabled" badge rather than vanishing from status.
    try:
        data = json.loads(raw_config)
        if isinstance(data, dict):
            if "mcp" in data and isinstance(data["mcp"], dict):
                data = data["mcp"]
            for name, entry in data.items():
                if isinstance(entry, dict) and entry.get("enabled") is False:
                    out.setdefault(name, {"status": "disabled"})
    except json.JSONDecodeError:
        pass
    return out
