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
import html
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


class _ReusableHTTPServerV6(_ReusableHTTPServer):
    address_family = socket.AF_INET6


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
    "<script>history.replaceState({},document.title,location.pathname);"
    "setTimeout(function(){window.close()},1500)</script>"
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
    "<script>history.replaceState({{}},document.title,location.pathname)</script>"
    "</div></body></html>"
)


@dataclass
class CallbackResult:
    code: Optional[str]
    state: Optional[str]
    error: Optional[str]


def callback_html(error: Optional[str] = None) -> bytes:
    """Return the self-contained page shown after an OAuth callback.

    The page has no external resources (so a callback URL cannot leak through
    a Referer header) and immediately removes the one-time query string from
    browser history. Provider-supplied errors are escaped before rendering.
    """
    if error:
        return _ERROR_HTML_TEMPLATE.format(detail=html.escape(error)).encode("utf-8")
    return _SUCCESS_HTML


class CallbackWaiter:
    """Thread-safe, one-shot callback delivery channel.

    Loopback callbacks and backend-owned HTTPS callbacks share the same wait /
    cancel semantics. The latter do not need to bind a socket; the FastAPI
    route delivers directly into this object.
    """

    def __init__(self) -> None:
        self._result: Optional[CallbackResult] = None
        self._event = threading.Event()
        self._lock = threading.Lock()

    def deliver(self, result: CallbackResult) -> bool:
        with self._lock:
            if self._result is None:
                self._result = result
                self._event.set()
                return True
            return False

    def wait(self, timeout: float) -> Optional[CallbackResult]:
        if self._event.wait(timeout):
            return self._result
        return None

    def stop(self) -> None:
        # Unblock a callback handler immediately. With no result, ``wait``
        # returns None and the owning OAuth worker exits without exchanging a
        # code or writing credentials.
        self._event.set()


def _single_query_value(query: dict[str, list[str]], name: str) -> Optional[str]:
    values = query.get(name) or []
    return values[0] if len(values) == 1 else None


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
        code = _single_query_value(qs, "code")
        state = _single_query_value(qs, "state")
        error = _single_query_value(qs, "error_description") or _single_query_value(
            qs, "error"
        )
        if error:
            body = callback_html(error)
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
        server.deliver(CallbackResult(code=code, state=state, error=error))


class LoopbackCallbackServer(CallbackWaiter):
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

    def __init__(
        self, host: str, port: int, path: str, *, redirect_uri: Optional[str] = None
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.expected_path = path
        self._redirect_uri = redirect_uri
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def redirect_uri(self) -> str:
        # localhost vs 127.0.0.1 matters: OAuth client redirect_uris must
        # match exactly. Let the caller decide via the constructor's host.
        if self._redirect_uri:
            return self._redirect_uri
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}{self.expected_path}"

    def deliver_callback_url(self, callback_url: str) -> bool:
        """Deliver a callback copied from a browser on another device.

        OAuth clients used by Codex, xAI, and many MCP servers pin a loopback
        redirect URI. On a phone, tablet, or other remote device that redirect
        targets the remote device's localhost, not this Mac. The user can copy
        the failed redirect URL back to the app; this method validates that it
        targets this exact callback listener and feeds it into the same
        one-shot result path as a local browser.
        """
        normalized_url = callback_url.strip()
        # Mobile browsers often omit the obvious scheme when copying what they
        # display in the address bar (``localhost:1455/...``). OAuth loopback
        # callbacks are HTTP, so make that shorthand unambiguous before parsing.
        if "://" not in normalized_url:
            normalized_url = f"http://{normalized_url.lstrip('/')}"
        parsed = urlparse(normalized_url)
        expected_host = self.host.lower().strip("[]")
        actual_host = (parsed.hostname or "").lower().strip("[]")
        try:
            actual_port = parsed.port or 80
        except ValueError as exc:
            raise ValueError("callback URL has an invalid port") from exc
        if (
            parsed.scheme != "http"
            or actual_host != expected_host
            or actual_port != self.port
            or parsed.path != self.expected_path
        ):
            raise ValueError(f"expected a callback URL beginning with {self.redirect_uri}")

        qs = parse_qs(parsed.query)
        code = _single_query_value(qs, "code")
        state = _single_query_value(qs, "state")
        error = _single_query_value(qs, "error_description") or _single_query_value(
            qs, "error"
        )
        if not code and not error:
            raise ValueError("callback URL does not contain an authorization code or error")
        return self.deliver(CallbackResult(code=code, state=state, error=error))

    def start(self) -> None:
        if self._server is not None:
            return
        server_cls = _ReusableHTTPServerV6 if ":" in self.host else _ReusableHTTPServer
        self._server = server_cls((self.host, self.port), _Handler)
        # Port 0 asks the OS for an isolated ephemeral listener. Capture the
        # selected port before constructing OAuth metadata or returning an
        # authorization URL so every stage uses the exact same redirect URI.
        self.port = int(self._server.server_address[1])
        # Attach so the handler can reach back into us.
        self._server.callback_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"oauth-callback-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        # Unblock any worker still in ``wait()`` — they'll observe ``None``
        # and exit promptly instead of stalling for the full timeout.
        super().stop()
        self._thread = None
