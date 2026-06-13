"""Unit tests for MCP config parsing + OAuth token resolution. No network."""
from __future__ import annotations
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models
from app.runner import mcp as mcp_mod


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield Session
    engine.dispose()


# --- parse_config ----------------------------------------------------------


def test_parse_remote_defaults_to_oauth_capable():
    cfg = mcp_mod.parse_config('{"s": {"type": "remote", "url": "https://x/mcp"}}')
    assert cfg["s"].oauth is not None  # remote == oauth-capable by default
    assert cfg["s"].oauth.client_id == ""


def test_parse_remote_oauth_false_opts_out():
    cfg = mcp_mod.parse_config('{"s": {"type": "remote", "url": "https://x/mcp", "oauth": false}}')
    assert cfg["s"].oauth is None


def test_parse_remote_oauth_dict_carries_client():
    raw = '{"s": {"type": "remote", "url": "https://x/mcp", "oauth": {"clientId": "abc", "scope": "read"}}}'
    cfg = mcp_mod.parse_config(raw)
    assert cfg["s"].oauth.client_id == "abc"
    assert cfg["s"].oauth.scope == "read"


def test_parse_local_environment_and_timeout():
    raw = '{"s": {"type": "local", "command": ["echo"], "environment": {"K": "v"}, "timeout": 5000}}'
    cfg = mcp_mod.parse_config(raw)
    assert cfg["s"].environment == {"K": "v"}
    assert cfg["s"].timeout == 5000
    assert cfg["s"].call_timeout_s == 5.0


def test_parse_disabled_server_skipped():
    cfg = mcp_mod.parse_config('{"s": {"type": "local", "command": ["echo"], "enabled": false}}')
    assert cfg == {}


def test_parse_local_oauth_is_none():
    cfg = mcp_mod.parse_config('{"s": {"type": "local", "command": ["echo"]}}')
    assert cfg["s"].oauth is None


# --- tool_identifiers ------------------------------------------------------


def test_tool_identifiers_basic():
    assert mcp_mod.tool_identifiers("Brave", "search") == ("brave", "search", "brave_search")


def test_tool_identifiers_strips_redundant_server_prefix():
    # Notion prefixes its own tools; the dotted form shouldn't double it up.
    s, t, q = mcp_mod.tool_identifiers("Notion", "notion-create-pages")
    assert (s, t, q) == ("notion", "create_pages", "notion_create_pages")


def test_tool_identifiers_sanitizes_to_python_identifiers():
    s, t, q = mcp_mod.tool_identifiers("My Server!", "Do.A-Thing")
    assert s == "my_server"
    assert t == "do_a_thing"
    assert q == "my_server_do_a_thing"


def test_tool_identifiers_leading_digit_tool():
    s, t, _ = mcp_mod.tool_identifiers("svc", "3d-render")
    assert s == "svc"
    assert t == "_3d_render"


# --- error classification --------------------------------------------------


def test_classify_error_unauthorized():
    assert mcp_mod._classify_error(Exception("server returned 401 Unauthorized")) == "needs_auth"
    assert mcp_mod._classify_error(Exception("HTTP 403 forbidden")) == "needs_auth"


def test_classify_error_other():
    assert mcp_mod._classify_error(Exception("connection refused")) == "failed"


# --- resolve_oauth_config --------------------------------------------------


def _store_cred(db_factory, name, url, token, expires_at, refresh=None):
    db = db_factory()
    try:
        db.add(
            models.McpCredential(
                server_name=name,
                server_url=url,
                access_token=token,
                refresh_token=refresh,
                expires_at=expires_at,
            )
        )
        db.commit()
    finally:
        db.close()


def test_resolve_injects_bearer_for_valid_token(db_factory):
    raw = json.dumps({"s": {"type": "remote", "url": "https://x/mcp"}})
    _store_cred(db_factory, "s", "https://x/mcp", "tok123", datetime.utcnow() + timedelta(hours=1))
    out = json.loads(mcp_mod.resolve_oauth_config(raw, db_factory))
    assert out["s"]["headers"]["Authorization"] == "Bearer tok123"


def test_resolve_skips_server_without_credential(db_factory):
    raw = json.dumps({"s": {"type": "remote", "url": "https://x/mcp"}})
    out = mcp_mod.resolve_oauth_config(raw, db_factory)
    assert out == raw  # unchanged when no stored token


def test_resolve_skips_oauth_false(db_factory):
    raw = json.dumps({"s": {"type": "remote", "url": "https://x/mcp", "oauth": False}})
    _store_cred(db_factory, "s", "https://x/mcp", "tok", datetime.utcnow() + timedelta(hours=1))
    out = mcp_mod.resolve_oauth_config(raw, db_factory)
    assert out == raw  # opted out of oauth, no injection


def test_resolve_no_db_factory_is_passthrough():
    raw = json.dumps({"s": {"type": "remote", "url": "https://x/mcp"}})
    assert mcp_mod.resolve_oauth_config(raw, None) == raw


def test_resolve_expired_unrefreshable_token_not_injected(db_factory):
    raw = json.dumps({"s": {"type": "remote", "url": "https://x/mcp"}})
    _store_cred(db_factory, "s", "https://x/mcp", "stale", datetime.utcnow() - timedelta(hours=1))
    out = mcp_mod.resolve_oauth_config(raw, db_factory)
    assert out == raw  # expired + no refresh token => left alone


# --- _has_usable_credential (drives probe needs-auth detection) ------------


def _cfg(name="s", url="https://x/mcp"):
    return mcp_mod.parse_config(json.dumps({name: {"type": "remote", "url": url}}))[name]


def test_has_usable_credential_false_without_row(db_factory):
    # No stored credential => connect plainly => server 401 => needs_auth.
    assert mcp_mod._has_usable_credential(_cfg(), db_factory) is False


def test_has_usable_credential_true_for_valid_token(db_factory):
    _store_cred(db_factory, "s", "https://x/mcp", "tok", datetime.utcnow() + timedelta(hours=1))
    assert mcp_mod._has_usable_credential(_cfg(), db_factory) is True


def test_has_usable_credential_true_when_expired_but_refreshable(db_factory):
    _store_cred(
        db_factory, "s", "https://x/mcp", "stale",
        datetime.utcnow() - timedelta(hours=1), refresh="r",
    )
    assert mcp_mod._has_usable_credential(_cfg(), db_factory) is True


def test_has_usable_credential_false_when_expired_unrefreshable(db_factory):
    _store_cred(db_factory, "s", "https://x/mcp", "stale", datetime.utcnow() - timedelta(hours=1))
    assert mcp_mod._has_usable_credential(_cfg(), db_factory) is False


# --- pre-registered client + redirect override ----------------------------


def test_parse_remote_oauth_dict_carries_redirect_override():
    raw = (
        '{"s": {"type": "remote", "url": "https://x/mcp", '
        '"oauth": {"clientId": "abc", "clientSecret": "shh", '
        '"redirectUri": "http://127.0.0.1:9000/cb", "callbackPort": 9000}}}'
    )
    cfg = mcp_mod.parse_config(raw)
    assert cfg["s"].oauth.client_secret == "shh"
    assert cfg["s"].oauth.redirect_uri == "http://127.0.0.1:9000/cb"
    assert cfg["s"].oauth.callback_port == 9000


def test_effective_redirect_uri_precedence():
    # Explicit redirectUri wins; callback_port is a shorthand; nothing falls
    # back to the global default.
    assert (
        mcp_mod.effective_redirect_uri(mcp_mod.OAuthConfig(redirect_uri="https://x/cb"))
        == "https://x/cb"
    )
    assert (
        mcp_mod.effective_redirect_uri(mcp_mod.OAuthConfig(callback_port=9000))
        == f"http://127.0.0.1:9000{mcp_mod.MCP_OAUTH_CALLBACK_PATH}"
    )
    assert mcp_mod.effective_redirect_uri(mcp_mod.OAuthConfig()) == mcp_mod.MCP_OAUTH_REDIRECT_URI
    assert mcp_mod.effective_redirect_uri(None) == mcp_mod.MCP_OAUTH_REDIRECT_URI


def test_db_token_storage_get_client_info_prefers_config(db_factory):
    # Core DCR-skip behaviour: when the user has supplied a pre-registered
    # clientId, get_client_info returns it directly even though the DB has no
    # row yet. The SDK's DCR gate at oauth2.py:572 sees populated client_info
    # and skips registration — fixes the Slack "Registration failed: 404" path.
    import asyncio

    oauth = mcp_mod.OAuthConfig(client_id="cid", client_secret="sec")
    storage = mcp_mod._make_db_token_storage("s", "https://x/mcp", db_factory, oauth)
    info = asyncio.run(storage.get_client_info())
    assert info is not None
    assert info.client_id == "cid"
    assert info.client_secret == "sec"
    assert info.token_endpoint_auth_method == "client_secret_post"


def test_db_token_storage_get_client_info_public_client_when_no_secret(db_factory):
    import asyncio

    oauth = mcp_mod.OAuthConfig(client_id="cid")  # no secret => public client
    storage = mcp_mod._make_db_token_storage("s", "https://x/mcp", db_factory, oauth)
    info = asyncio.run(storage.get_client_info())
    assert info.client_secret is None
    assert info.token_endpoint_auth_method == "none"


def test_db_token_storage_get_client_info_falls_back_to_db_without_config(db_factory):
    # No config-supplied clientId and no DB row => return None so the SDK
    # proceeds to DCR (the original behaviour for DCR-capable servers).
    import asyncio

    storage = mcp_mod._make_db_token_storage("s", "https://x/mcp", db_factory, mcp_mod.OAuthConfig())
    assert asyncio.run(storage.get_client_info()) is None


# --- DCR error classification ---------------------------------------------


class _FakeOAuthRegistrationError(Exception):
    pass


def test_classify_error_dcr_failure_is_not_needs_auth():
    # DCR failure is not retryable as a sign-in prompt; the user needs to
    # supply a pre-registered clientId. Make sure it's classified as failed
    # (so the UI shows the actionable error message) rather than needs_auth.
    e = _FakeOAuthRegistrationError("Registration failed: 404 <html>...</html>")
    e.__class__.__name__ = "OAuthRegistrationError"
    assert mcp_mod._classify_error(e) == "failed"


def test_format_connect_error_trims_dcr_html():
    e = _FakeOAuthRegistrationError("Registration failed: 404 " + "<html>" * 1000)
    e.__class__.__name__ = "OAuthRegistrationError"
    msg = mcp_mod._format_connect_error(e)
    assert "<html>" not in msg
    assert "clientId" in msg


def test_format_connect_error_passes_through_other_errors():
    msg = mcp_mod._format_connect_error(ValueError("bad config"))
    assert msg == "ValueError: bad config"


def test_format_connect_error_unwraps_exception_group():
    # anyio's TaskGroup (used by streamablehttp_client) wraps the real failure
    # in a BaseExceptionGroup whose str() is "unhandled errors in a TaskGroup".
    # Without unwrapping the user sees that wrapper and nothing about the
    # underlying cause.
    inner = ConnectionRefusedError("nope")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [inner])
    msg = mcp_mod._format_connect_error(group)
    assert "TaskGroup" not in msg
    assert "ConnectionRefusedError" in msg
    assert "nope" in msg


def test_format_connect_error_unwraps_dcr_inside_exception_group():
    # The actual Slack repro: OAuthRegistrationError raised inside an
    # ExceptionGroup. Unwrapping must happen *before* the registration check
    # or we'd still get the opaque wrapper message.
    inner = _FakeOAuthRegistrationError("Registration failed: 404 <html>...</html>")
    inner.__class__.__name__ = "OAuthRegistrationError"
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [inner])
    msg = mcp_mod._format_connect_error(group)
    assert "clientId" in msg
    assert "<html>" not in msg


def test_format_connect_error_trims_long_single_line():
    msg = mcp_mod._format_connect_error(RuntimeError("x" * 1000))
    assert len(msg) <= 500
    assert msg.endswith("…")


# --- auth-failure surfacing (re-login popup plumbing) ------------------------


def test_classify_error_oauth_flow_error_is_needs_auth():
    # The SDK raises this when stored tokens can't refresh and no interactive
    # redirect handler is attached (probe / call paths).
    class OAuthFlowError(Exception):
        pass

    err = OAuthFlowError("No redirect handler provided for authorization code grant")
    assert mcp_mod._classify_error(err) == "needs_auth"


@pytest.fixture()
def server_status():
    from app.runner import tools as tools_mod

    tools_mod.MCP_SERVER_STATUS.clear()
    yield tools_mod.MCP_SERVER_STATUS
    tools_mod.MCP_SERVER_STATUS.clear()


def test_mcp_unavailable_error_needs_auth_marker(server_status):
    from app.runner.tools import mcp_unavailable_error

    server_status["my_slack"] = {
        "server": "my-slack",
        "status": "needs_auth",
        "error": "authentication required",
    }
    # Both the flat tool name and the bare server attr resolve.
    for name in ("my_slack_send_message", "my_slack"):
        out = mcp_unavailable_error(name)
        assert out is not None
        assert out["error_type"] == "needs_auth"
        assert out["server"] == "my-slack"
        assert "authentication" in out["error"]


def test_mcp_unavailable_error_failed_server_has_no_auth_marker(server_status):
    from app.runner.tools import mcp_unavailable_error

    server_status["srv"] = {"server": "srv", "status": "failed", "error": "boom"}
    out = mcp_unavailable_error("srv_tool")
    assert out is not None
    assert "error_type" not in out
    assert "boom" in out["error"]


def test_mcp_unavailable_error_connected_or_unknown_is_none(server_status):
    from app.runner.tools import mcp_unavailable_error

    server_status["ok"] = {"server": "ok", "status": "connected", "tool_count": 3}
    assert mcp_unavailable_error("ok_tool") is None
    assert mcp_unavailable_error("unrelated_tool") is None


def test_manager_call_unconnected_needs_auth_server_carries_marker():
    mgr = mcp_mod.MCPManager()
    mgr._status["s"] = {"status": "needs_auth", "error": "authentication required"}
    out = mgr.call("s", "tool", {})
    assert out["isError"] is True
    assert out["error_type"] == "needs_auth"
    assert out["server"] == "s"


def test_manager_call_unconnected_failed_server_has_no_marker():
    mgr = mcp_mod.MCPManager()
    mgr._status["s"] = {"status": "failed", "error": "boom"}
    out = mgr.call("s", "tool", {})
    assert out["isError"] is True
    assert "error_type" not in out


def test_login_status_expired_unrefreshable_reports_signed_out(db_factory):
    from app.api.mcp import login_status

    _store_cred(db_factory, "s", "https://x/mcp", "stale", datetime.utcnow() - timedelta(hours=1))
    db = db_factory()
    try:
        assert login_status("s", db=db).status == "signed_out"
    finally:
        db.close()


def test_login_status_expired_but_refreshable_reports_signed_in(db_factory):
    from app.api.mcp import login_status

    _store_cred(
        db_factory,
        "s",
        "https://x/mcp",
        "stale",
        datetime.utcnow() - timedelta(hours=1),
        refresh="r1",
    )
    db = db_factory()
    try:
        assert login_status("s", db=db).status == "signed_in"
    finally:
        db.close()


def test_login_status_fresh_token_reports_signed_in(db_factory):
    from app.api.mcp import login_status

    _store_cred(db_factory, "s", "https://x/mcp", "tok", datetime.utcnow() + timedelta(hours=1))
    db = db_factory()
    try:
        assert login_status("s", db=db).status == "signed_in"
    finally:
        db.close()


# --- fail-fast connect against an unauthenticated server ---------------------


def test_probe_unauthenticated_server_fails_fast_as_needs_auth():
    """A server answering 401 must classify as needs_auth within seconds.

    Regression: the connect used to enter the SDK transport's cancel scopes in
    a detached asyncio.wait_for task, so the 401 couldn't cancel anything —
    every probe/discover/run-start hung for the full 60s connect timeout and
    came back 'failed: TimeoutError' instead of needs_auth.
    """
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self):
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        raw = json.dumps(
            {"s": {"type": "remote", "url": f"http://127.0.0.1:{srv.server_address[1]}/mcp"}}
        )
        start = time.time()
        out = mcp_mod.probe(raw)
        elapsed = time.time() - start
        assert out["s"]["status"] == "needs_auth", out
        assert elapsed < 10, f"probe took {elapsed:.1f}s — connect is blocking again"
    finally:
        srv.shutdown()


# --- fast-fail on a server known to be down (no read-timeout hang) -----------


def test_call_fast_fails_when_status_needs_auth():
    """A server marked needs_auth (mid-run 401, or transport teardown) makes
    every later call return immediately with the auth marker — never waiting
    out the read timeout."""
    mgr = mcp_mod.MCPManager()
    mgr._status["s"] = {"status": "needs_auth", "error": "authentication required"}
    # A live session is present, but the guard short-circuits before using it.
    mgr._sessions["s"] = object()
    out = mgr.call("s", "tool", {})
    assert out["isError"] is True
    assert out["error_type"] == "needs_auth"
    assert "authenticated" in out["error"]
    assert out["server"] == "s"


def test_call_fast_fails_when_status_failed():
    mgr = mcp_mod.MCPManager()
    mgr._status["s"] = {"status": "failed", "error": "no response within 120s — it may be disconnected"}
    mgr._sessions["s"] = object()
    out = mgr.call("s", "tool", {})
    assert out["isError"] is True
    assert "error_type" not in out
    assert "unavailable" in out["error"]
    assert "disconnected" in out["error"]


def test_call_connected_status_does_not_short_circuit():
    """A healthy server isn't fast-failed — the guard returns None so the call
    proceeds (and here errors only because there's no real loop/session)."""
    mgr = mcp_mod.MCPManager()
    mgr._status["s"] = {"status": "connected", "tool_count": 3}
    assert mgr._down_result("s") is None
    # No session/loop wired up, so the call falls through to "not connected"
    # rather than the down-guard.
    out = mgr.call("s", "tool", {})
    assert out["error"] == "MCP server 's' not connected"
