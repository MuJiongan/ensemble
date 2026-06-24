"""Shared subprocess-side helpers for the runner children.

Both the workflow runner (`app.runner.child`) and the continue-chat turn runner
(`app.runner.chat_child`) spawn a child process that reads a JSON payload from
stdin, applies its env, optionally connects MCP servers, and streams JSON-line
events to stdout. The plumbing for all of that lives here so the two children
share one implementation instead of copying it.
"""
from __future__ import annotations
import json
import os
import signal
import sys
import threading


_emit_lock = threading.Lock()


def _emit(event: dict) -> None:
    """Write one JSON-line event to stdout. Locked so events emitted from
    concurrent threads (e.g. a node's parallel ``ctx.agent`` calls) don't
    interleave mid-line."""
    line = json.dumps(event, default=str) + "\n"
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _install_sigterm_handler() -> None:
    """Make SIGTERM raise KeyboardInterrupt so we can emit a clean cancelled event."""
    def _handler(signum, frame):
        raise KeyboardInterrupt("cancelled")
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


def _read_payload() -> dict:
    """Read the JSON payload from stdin and apply its env-vars in place."""
    payload = json.loads(sys.stdin.read())
    for k, v in (payload.get("env") or {}).items():
        if v:
            os.environ[k] = v
    return payload


def _load_mcp_tools():
    """Connect to configured MCP servers and register their tools into the
    runtime registry so node / chat code can use them via
    ``ctx.agent(tools=[...])`` or ``ctx.tools.<name>(...)``. Best-effort: a
    connection failure is logged to stderr (captured by the parent for
    diagnostics) but never fails the run. Returns the live manager so the
    caller can shut it down on exit."""
    raw = os.environ.get("MCP_SERVERS", "")
    if not raw.strip():
        return None
    try:
        from app.runner import mcp as mcp_mod
        from app.runner import tools as tools_mod

        manager = mcp_mod.register_runtime_tools(
            raw,
            tools_mod.REGISTRY,
            tools_mod.TOOL_SCHEMAS,
            tools_mod.MCP_NAMESPACES,
            tools_mod.MCP_SERVER_STATUS,
        )
        if manager is not None:
            for name, st in manager.status().items():
                print(f"[mcp] {name}: {st}", file=sys.stderr)
            # Surface per-server connection state to the run UI so it can
            # prompt for re-login when a configured server needs auth.
            _emit({"type": "mcp_status", "servers": manager.status()})
        return manager
    except Exception as e:
        print(f"[mcp] failed to load MCP tools: {type(e).__name__}: {e}", file=sys.stderr)
        return None
