"""Remote-browser handoff for OAuth loopback callbacks."""
from __future__ import annotations

import pytest

from app.auth.oauth import LoopbackCallbackServer


def test_callback_url_can_be_delivered_without_local_http_request():
    server = LoopbackCallbackServer("localhost", 1455, "/auth/callback")

    accepted = server.deliver_callback_url(
        "http://localhost:1455/auth/callback?code=abc123&state=csrf-token"
    )

    result = server.wait(timeout=0)
    assert accepted is True
    assert result is not None
    assert result.code == "abc123"
    assert result.state == "csrf-token"
    assert result.error is None


def test_callback_url_without_http_scheme_is_normalized():
    server = LoopbackCallbackServer("localhost", 1455, "/auth/callback")

    accepted = server.deliver_callback_url(
        "localhost:1455/auth/callback?code=abc123&scope=openid&state=csrf-token"
    )

    result = server.wait(timeout=0)
    assert accepted is True
    assert result is not None
    assert result.code == "abc123"
    assert result.state == "csrf-token"


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:1455/auth/callback?code=x",
        "http://127.0.0.1:1455/auth/callback?code=x",
        "http://localhost:9999/auth/callback?code=x",
        "http://localhost:1455/wrong?code=x",
        "http://localhost:1455/auth/callback",
    ],
)
def test_callback_url_must_match_the_active_loopback_listener(url: str):
    server = LoopbackCallbackServer("localhost", 1455, "/auth/callback")

    with pytest.raises(ValueError):
        server.deliver_callback_url(url)


def test_only_first_callback_is_accepted():
    server = LoopbackCallbackServer("127.0.0.1", 19876, "/mcp/oauth/callback")

    assert server.deliver_callback_url(
        "http://127.0.0.1:19876/mcp/oauth/callback?code=first&state=one"
    )
    assert not server.deliver_callback_url(
        "http://127.0.0.1:19876/mcp/oauth/callback?code=second&state=two"
    )
