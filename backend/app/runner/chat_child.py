"""Continue-chat turn subprocess entrypoint.

Where `app.runner.child` executes a whole graph, this child runs exactly one
``ctx.agent`` invocation: it re-seeds a recorded conversation, lets the model
continue it (with the same tools — built-ins + MCP — reconnected just like a
run), and streams the result back. One turn of a continued agent conversation.

Reads a JSON payload from stdin::

    {"messages": [...], "tools": [...], "model": "...", "workdir": "...", "env": {...}}

Emits the same per-call event contract a run does — ``llm_call_started``,
``llm_round_started``, ``llm_call_chunk``, ``tool_call_started/finished``,
``llm_call_finished`` — but with no ``node_id`` (there is no node). The terminal
event reuses ``run_finished`` (so the generic events pub/sub treats it as the
end of the stream) and carries the grown conversation for persistence::

    {"type": "run_finished", "status": "success"|"error"|"cancelled",
     "messages": [...], "usage": {...}, "cost": 0.0, "error": null|"..."}
"""
from __future__ import annotations
import os
import threading
from pathlib import Path

from app.runner.subprocess_io import (
    _emit,
    _install_sigterm_handler,
    _load_mcp_tools,
    _read_payload,
)


def main() -> None:
    _install_sigterm_handler()
    payload = _read_payload()
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    model = payload.get("model") or payload.get("default_model") or ""
    workdir = Path(payload.get("workdir") or ".")
    workdir.mkdir(parents=True, exist_ok=True)

    # Tools (built-ins + MCP) only matter if the continuation can call them.
    # A tool-less continuation is a plain back-and-forth, so skip MCP connect — no
    # point paying that latency to register tools the model can't use.
    mcp_manager = _load_mcp_tools() if tools else None

    from app.runner.ctx import Ctx

    # on_event is the raw emitter — unlike a run, there's no node wrapper adding
    # a node_id, because a chat turn doesn't belong to any node.
    ctx = Ctx(workdir=workdir, default_model=model, on_event=_emit)

    _emit({"type": "run_started"})

    cancelled = False
    try:
        result = ctx.agent(model=model, prompt=messages, tools=tools)
    except KeyboardInterrupt:
        cancelled = True
        _emit({
            "type": "run_finished",
            "status": "cancelled",
            "error": "cancelled by user",
            # The conversation up to the interrupted turn isn't reliably
            # assembled, so don't claim a grown transcript — keep the seed.
            "messages": messages,
            "usage": {},
            "cost": 0.0,
        })
        # Best-effort MCP teardown before force-exit: spawned MCP servers are
        # child processes that would otherwise be orphaned. Bound it in a daemon
        # thread so a transport blocked on its own I/O can't wedge the cancel —
        # if shutdown doesn't return in 2s we force-exit anyway (a stream stuck
        # in httpx must not keep us alive — mirrors the run child's cancel path).
        if mcp_manager is not None:
            done = threading.Event()

            def _shutdown():
                try:
                    mcp_manager.shutdown()
                except Exception:
                    pass
                finally:
                    done.set()

            threading.Thread(target=_shutdown, daemon=True).start()
            done.wait(timeout=2.0)
        os._exit(0)
    except Exception as e:
        _emit({
            "type": "run_finished",
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "messages": messages,
            "usage": {},
            "cost": 0.0,
        })
        return
    finally:
        # Clean shutdown of MCP transports (skip on cancel — we force-exited).
        if mcp_manager is not None and not cancelled:
            try:
                mcp_manager.shutdown()
            except Exception:
                pass

    _emit({
        "type": "run_finished",
        "status": "success",
        "error": None,
        "messages": result.get("messages") or messages,
        "usage": result.get("usage") or {},
        "cost": float(result.get("cost") or 0.0),
    })


if __name__ == "__main__":
    main()
