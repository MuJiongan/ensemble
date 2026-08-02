"""Concurrency, cancellation, and public-callback coverage for MCP OAuth."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.responses import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.api import mcp as mcp_api
from app.auth import mcp_oauth
from app.auth import state as login_state
from app.auth.oauth import CallbackWaiter, LoopbackCallbackServer
from app.db import Base
from app.runner import mcp as mcp_runner


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_oauth_state(monkeypatch):
    monkeypatch.delenv(mcp_oauth.PUBLIC_BASE_URL_ENV, raising=False)
    yield
    for name in ("a", "b", "late", "public-a", "public-b", "expired"):
        mcp_oauth.cancel(name)
        login_state.clear(mcp_oauth.state_key(name))
    with mcp_oauth._PUBLIC_LOCK:
        mcp_oauth._PUBLIC_FLOWS.clear()


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


async def _waiting_login(
    cfg,
    db_factory,
    receiver,
    owner,
    url_box,
    flow_box,
    key,
    callback_mode,
):
    """Network-free worker used to exercise start/cancel listener ownership."""
    url_box["url"] = (
        "https://auth.example/authorize?state=test&redirect_uri="
        + quote(cfg.oauth.redirect_uri, safe="")
    )
    url_box["event"].set()
    result = await asyncio.to_thread(receiver.wait, 5)
    if result is None:
        raise RuntimeError("sign-in cancelled")


def test_default_flows_bind_distinct_ephemeral_ports(monkeypatch, db_factory):
    monkeypatch.setattr(mcp_oauth, "_run_login", _waiting_login)

    mcp_oauth.start_login("a", "https://a.example/mcp", None, db_factory)
    mcp_oauth.start_login("b", "https://b.example/mcp", None, db_factory)

    a = login_state.get(mcp_oauth.state_key("a"))
    b = login_state.get(mcp_oauth.state_key("b"))
    assert a is not None and b is not None
    assert isinstance(a.server, LoopbackCallbackServer)
    assert isinstance(b.server, LoopbackCallbackServer)
    assert a.server.port != 0
    assert b.server.port != 0
    assert a.server.port != b.server.port
    assert a.callback_uri == a.server.redirect_uri
    assert b.callback_uri == b.server.redirect_uri


def test_cancel_releases_fixed_callback_for_another_server(monkeypatch, db_factory):
    monkeypatch.setattr(mcp_oauth, "_run_login", _waiting_login)
    port = _free_port()

    mcp_oauth.start_login(
        "a", "https://a.example/mcp", {"callbackPort": port}, db_factory
    )
    a = login_state.get(mcp_oauth.state_key("a"))
    assert a is not None
    assert mcp_oauth.cancel("a") is True
    assert a.thread is not None
    a.thread.join(timeout=2)
    assert not a.thread.is_alive()

    mcp_oauth.start_login(
        "b", "https://b.example/mcp", {"callbackPort": port}, db_factory
    )
    b = login_state.get(mcp_oauth.state_key("b"))
    assert b is not None
    assert b.callback_uri == f"http://127.0.0.1:{port}/mcp/oauth/callback"


def test_fixed_callback_collision_names_active_owner(monkeypatch, db_factory):
    monkeypatch.setattr(mcp_oauth, "_run_login", _waiting_login)
    port = _free_port()
    mcp_oauth.start_login(
        "a", "https://a.example/mcp", {"callbackPort": port}, db_factory
    )

    with pytest.raises(mcp_oauth.CallbackEndpointBusy) as exc:
        mcp_oauth.start_login(
            "b", "https://b.example/mcp", {"callbackPort": port}, db_factory
        )
    assert exc.value.owner == "a"
    assert str(port) in str(exc.value)


def test_cancelled_attempt_cannot_persist_late_tokens(db_factory):
    key = mcp_oauth.state_key("late")
    owner = login_state.LoginState(status="pending", started_at=time.time())
    assert login_state.claim(key, owner)
    storage = mcp_runner._make_db_token_storage(
        "late",
        "https://late.example/mcp",
        db_factory,
        mcp_runner.OAuthConfig(),
        force_authorization=True,
        write_guard=lambda action: login_state.run_if_pending_owner(key, owner, action),
    )
    OAuthToken = mcp_runner._oauth_imports()[-1]

    assert login_state.cancel(key)
    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="must-not-land", token_type="Bearer")
            )
        )

    db = db_factory()
    try:
        assert db.query(models.McpCredential).filter_by(server_name="late").first() is None
    finally:
        db.close()


def test_pending_reauth_takes_precedence_over_old_credential(db_factory):
    db = db_factory()
    try:
        db.add(
            models.McpCredential(
                server_name="a",
                server_url="https://a.example/mcp",
                access_token="old-but-unexpired",
            )
        )
        db.commit()
        owner = login_state.LoginState(
            status="pending",
            started_at=time.time(),
            server=CallbackWaiter(),
            callback_uri="https://ensemble.example/api/mcp/oauth/callback",
        )
        assert login_state.claim(mcp_oauth.state_key("a"), owner)

        result = mcp_api.login_status("a", db=db)
        assert result.status == "pending"
        assert result.callback_mode == "public"
    finally:
        db.close()


def _claim_public(name: str, oauth_state: str, *, age: float = 0) -> CallbackWaiter:
    key = mcp_oauth.state_key(name)
    receiver = CallbackWaiter()
    owner = login_state.LoginState(
        status="pending",
        started_at=time.time() - age,
        server=receiver,
        callback_uri="https://ensemble.example/api/mcp/oauth/callback",
    )
    assert login_state.claim(key, owner)
    mcp_oauth._register_public_flow(oauth_state, key, owner, receiver)
    return receiver


def test_public_callback_route_dispatches_concurrent_flows_and_rejects_replay():
    a = _claim_public("public-a", "state-a")
    b = _claim_public("public-b", "state-b")
    app = FastAPI()
    app.include_router(mcp_api.router)

    with TestClient(app) as client:
        response = client.get(
            "/api/mcp/oauth/callback", params={"code": "code-b", "state": "state-b"}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "code-b" not in response.text

        replay = client.get(
            "/api/mcp/oauth/callback", params={"code": "again", "state": "state-b"}
        )
        assert replay.status_code == 409

        response = client.get(
            "/api/mcp/oauth/callback", params={"code": "code-a", "state": "state-a"}
        )
        assert response.status_code == 200

    assert b.wait(0).code == "code-b"
    assert a.wait(0).code == "code-a"


def test_public_callback_rejects_missing_mismatched_and_expired_state():
    _claim_public(
        "expired", "old-state", age=mcp_oauth.CALLBACK_TIMEOUT_SECS + 1
    )

    with pytest.raises(mcp_oauth.PublicCallbackError, match="missing"):
        mcp_oauth.deliver_public_callback(code="x", state=None, error=None)
    with pytest.raises(mcp_oauth.PublicCallbackError, match="unknown"):
        mcp_oauth.deliver_public_callback(
            code="x", state="another-server-state", error=None
        )
    with pytest.raises(mcp_oauth.PublicCallbackError) as expired:
        mcp_oauth.deliver_public_callback(code="x", state="old-state", error=None)
    assert expired.value.status_code == 410


def test_public_base_url_is_trusted_and_https_only(monkeypatch):
    monkeypatch.setenv(mcp_oauth.PUBLIC_BASE_URL_ENV, "https://ensemble.example:8443")
    mode, receiver, uri = mcp_oauth._callback_for(mcp_runner.OAuthConfig())
    try:
        assert mode == "public"
        assert uri == "https://ensemble.example:8443/api/mcp/oauth/callback"
    finally:
        receiver.stop()

    monkeypatch.setenv(mcp_oauth.PUBLIC_BASE_URL_ENV, "http://ensemble.example")
    with pytest.raises(RuntimeError, match="HTTPS"):
        mcp_oauth.configured_public_callback_uri()


def test_public_callback_query_is_scrubbed_before_routing():
    from app.main import apply_settings_headers

    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": mcp_oauth.PUBLIC_CALLBACK_PATH,
        "raw_path": mcp_oauth.PUBLIC_CALLBACK_PATH.encode(),
        "query_string": b"code=secret-code&state=one-time-state",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("ensemble.example", 443),
    }
    request = Request(scope)

    async def call_next(inner: Request):
        assert inner.scope["query_string"] == b""
        assert inner.scope["mcp_oauth_callback_params"] == {
            "code": ["secret-code"],
            "state": ["one-time-state"],
        }
        return Response()

    asyncio.run(apply_settings_headers(request, call_next))


def test_interactive_dcr_does_not_reuse_client_for_previous_redirect(db_factory):
    db = db_factory()
    try:
        db.add(
            models.McpCredential(
                server_name="a",
                server_url="https://a.example/mcp",
                client_id="registered-for-old-port",
            )
        )
        db.commit()
    finally:
        db.close()

    storage = mcp_runner._make_db_token_storage(
        "a",
        "https://a.example/mcp",
        db_factory,
        mcp_runner.OAuthConfig(
            redirect_uri="http://127.0.0.1:54321/mcp/oauth/callback"
        ),
        force_authorization=True,
    )
    assert asyncio.run(storage.get_client_info()) is None


def test_interactive_dcr_reuses_client_when_exact_redirect_still_matches(db_factory):
    redirect_uri = "https://ensemble.example/api/mcp/oauth/callback"
    db = db_factory()
    try:
        db.add(
            models.McpCredential(
                server_name="a",
                server_url="https://a.example/mcp",
                client_id="compatible-client",
                redirect_uri=redirect_uri,
            )
        )
        db.commit()
    finally:
        db.close()

    storage = mcp_runner._make_db_token_storage(
        "a",
        "https://a.example/mcp",
        db_factory,
        mcp_runner.OAuthConfig(redirect_uri=redirect_uri),
        force_authorization=True,
    )
    info = asyncio.run(storage.get_client_info())
    assert info is not None
    assert info.client_id == "compatible-client"
    assert [str(uri) for uri in info.redirect_uris] == [redirect_uri]
