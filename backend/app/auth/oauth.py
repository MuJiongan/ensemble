"""Shared OAuth primitives used by the per-provider login flows.

Two pieces:

* :func:`generate_pkce` / :func:`generate_state` — RFC 7636 (PKCE S256) +
  CSRF nonce generation.
* :class:`LoopbackCallbackServer` — spin a one-shot HTTP server on a pinned
  port, await the ``GET /<path>?code=...&state=...`` callback that the
  provider's authorization endpoint will redirect the user's browser to,
  and return the parsed query.

OAuth client_ids that pin a specific ``redirect_uri`` (Codex CLI, Grok-CLI)
need a fixed host:port, so we let the caller specify both.
"""
from __future__ import annotations
import base64
import hashlib
import secrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse


class _ReusableHTTPServer(HTTPServer):
    """``HTTPServer`` variant that enables ``SO_REUSEADDR``.

    Without this, closing the server and immediately re-binding to the same
    port races against the kernel's TIME_WAIT window — the first OAuth attempt
    leaves the port briefly unbindable, so a quick retry crashes with
    ``OSError: address already in use``.
    """
    allow_reuse_address = True

    def server_bind(self):  # noqa: D401 (stdlib override)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def _b64url(buf: bytes) -> str:
    return base64.urlsafe_b64encode(buf).rstrip(b"=").decode("ascii")


_PKCE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


@dataclass(frozen=True)
class PkceCodes:
    verifier: str
    challenge: str  # S256 of verifier


def generate_pkce(length: int = 64) -> PkceCodes:
    verifier = "".join(secrets.choice(_PKCE_ALPHABET) for _ in range(length))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return PkceCodes(verifier=verifier, challenge=_b64url(digest))


def generate_state(num_bytes: int = 32) -> str:
    return _b64url(secrets.token_bytes(num_bytes))


_SUCCESS_HTML = (
    "<!doctype html>"
    "<html><head><title>emdash - signed in</title>"
    "<style>body{font-family:system-ui;display:flex;align-items:center;"
    "justify-content:center;height:100vh;margin:0;background:#fafaf9;color:#1a1714}"
    ".box{text-align:center;padding:2rem}h1{font-weight:400}p{color:#6b6b6b}</style>"
    "</head><body><div class=\"box\">"
    "<h1>signed in.</h1><p>you can close this window and return to emdash.</p>"
    "<script>setTimeout(function(){window.close()},1500)</script>"
    "</div></body></html>"
).encode("utf-8")


_ERROR_HTML_TEMPLATE = (
    "<!doctype html>"
    "<html><head><title>emdash - sign-in failed</title>"
    "<style>body{{font-family:system-ui;display:flex;align-items:center;"
    "justify-content:center;height:100vh;margin:0;background:#fafaf9;color:#1a1714}}"
    ".box{{text-align:center;padding:2rem}}h1{{font-weight:400;color:#b04030}}p{{color:#6b6b6b}}"
    ".err{{font-family:monospace;background:#fdf2f0;padding:1rem;border-radius:4px;margin-top:1rem}}</style>"
    "</head><body><div class=\"box\">"
    "<h1>sign-in failed.</h1><p>you can close this window and try again.</p>"
    "<div class=\"err\">{detail}</div>"
    "</div></body></html>"
)


@dataclass
class CallbackResult:
    code: Optional[str]
    state: Optional[str]
    error: Optional[str]


class _Handler(BaseHTTPRequestHandler):
    server_version = "emdash-oauth/1.0"
    # Suppress the default stderr access log so we don't spam the backend.
    def log_message(self, *args, **kwargs):  # type: ignore[override]
        return

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        server: "LoopbackCallbackServer" = self.server.callback_server  # type: ignore[attr-defined]
        if parsed.path != server.expected_path:
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        code = (qs.get("code") or [None])[0]
        state = (qs.get("state") or [None])[0]
        error = (qs.get("error_description") or qs.get("error") or [None])[0]
        if error:
            body = _ERROR_HTML_TEMPLATE.format(detail=error).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_SUCCESS_HTML)))
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML)
        server._deliver(CallbackResult(code=code, state=state, error=error))


class LoopbackCallbackServer:
    """One-shot loopback HTTP server that catches a single OAuth redirect.

    Usage::

        srv = LoopbackCallbackServer(host="127.0.0.1", port=1455,
                                     path="/auth/callback")
        srv.start()
        result = srv.wait(timeout=300)  # blocks until callback or timeout
        srv.stop()

    Both ``start()`` and ``stop()`` are idempotent. ``wait()`` is safe to call
    only once per server instance.
    """

    def __init__(self, host: str, port: int, path: str):
        self.host = host
        self.port = port
        self.expected_path = path
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._result: Optional[CallbackResult] = None
        self._event = threading.Event()
        self._lock = threading.Lock()

    @property
    def redirect_uri(self) -> str:
        # localhost vs 127.0.0.1 matters: OAuth client redirect_uris must
        # match exactly. Let the caller decide via the constructor's host.
        host = self.host
        return f"http://{host}:{self.port}{self.expected_path}"

    def _deliver(self, result: CallbackResult) -> None:
        with self._lock:
            if self._result is None:
                self._result = result
                self._event.set()

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _ReusableHTTPServer((self.host, self.port), _Handler)
        # Attach so the handler can reach back into us.
        self._server.callback_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"oauth-callback-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def wait(self, timeout: float) -> Optional[CallbackResult]:
        if self._event.wait(timeout):
            return self._result
        return None

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        # Unblock any worker still in ``wait()`` — they'll observe ``None``
        # and exit promptly instead of stalling for the full timeout.
        self._event.set()
        self._thread = None
