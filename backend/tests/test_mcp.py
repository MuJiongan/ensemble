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
