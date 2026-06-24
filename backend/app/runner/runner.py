"""Workflow runner.

`run_workflow_streaming` spawns a child Python process and pipes its JSON-line
events through `app.runner.events.append_event`, which fans them out to any
WebSocket subscribers and accumulates a backlog. `run_workflow_sync` is a
compat wrapper that runs streaming to completion and then materializes the
legacy result shape — used by tests and any other sync caller.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import defaultdict, deque
from typing import Any, Callable

from app.runner import events as ev_mod


def _resolve_mcp_servers() -> str:
    """Augment the MCP config with fresh OAuth bearer tokens for remote servers.

    The child subprocess has no DB access, so the API process resolves a fresh
    access token per OAuth server here and injects it as an Authorization
    header. Falls back to the raw config if resolution can't run.
    """
    raw = os.getenv("MCP_SERVERS", "")
    if not raw.strip():
        return raw
    try:
        from app.runner import mcp as mcp_mod
        from app.db import SessionLocal

        return mcp_mod.resolve_oauth_config(raw, SessionLocal)
    except Exception:
        return raw


def build_child_env() -> dict[str, str]:
    """Build the env a runner child inherits, from the current process env
    (which the request middleware has populated from the user's settings).

    Node calls use the NODE_* credentials (independent of the orchestrator's
    LLM_* creds), so a run uses the *node's* provider/model — not whatever the
    orchestrator chat is signed into. OAuth-backed providers (codex/xai) get a
    fresh access token resolved here and forwarded as the effective LLM_API_KEY;
    the subprocess never touches the credentials DB itself. Shared by the
    workflow runner and the continue-chat turn runner so both spawn with the
    same node credentials + MCP config.
    """
    env_for_child: dict[str, str] = {
        "LLM_API_KEY": os.getenv("NODE_API_KEY", ""),
        "LLM_BASE_URL": os.getenv("NODE_BASE_URL", ""),
        "PARALLEL_API_KEY": os.getenv("PARALLEL_API_KEY", ""),
        # Provider id + node reasoning variant so the child can apply the
        # catalog-computed reasoning options to each ``ctx.agent`` body.
        "LLM_PROVIDER_ID": (os.getenv("NODE_PROVIDER_ID") or "").strip(),
        "DEFAULT_NODE_VARIANT": os.getenv("DEFAULT_NODE_VARIANT", ""),
        # MCP server config (opencode-style JSON); the child connects to these
        # and registers their tools into its runtime registry. Remote OAuth
        # servers get a fresh bearer injected here (the child has no DB access).
        "MCP_SERVERS": _resolve_mcp_servers(),
    }
    pid = (os.getenv("NODE_PROVIDER_ID") or "").strip()
    if pid in ("codex", "xai"):
        from app.auth.resolve import resolve
        creds = resolve(pid)
        if creds is not None:
            env_for_child["LLM_API_KEY"] = creds.access_token
            env_for_child["LLM_PROVIDER_ID"] = pid
            if pid == "codex":
                env_for_child["LLM_ACCOUNT_ID"] = creds.account_id or ""
            elif pid == "xai":
                env_for_child["LLM_BASE_URL"] = "https://api.x.ai/v1"
    return env_for_child


def topo_sort(nodes: list[dict], edges: list[dict]) -> list[str]:
    indeg: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    node_ids = {n["id"] for n in nodes}
    for nid in node_ids:
        indeg[nid] = 0
    for e in edges:
        if e["from_node_id"] in node_ids and e["to_node_id"] in node_ids:
            adj[e["from_node_id"]].append(e["to_node_id"])
            indeg[e["to_node_id"]] += 1
    q = deque([nid for nid in node_ids if indeg[nid] == 0])
    out: list[str] = []
    while q:
        nid = q.popleft()
        out.append(nid)
        for m in adj[nid]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if len(out) != len(node_ids):
        raise ValueError("workflow has a cycle")
    return out


def drive_child_subprocess(
    run_id: str,
    module: str,
    payload: dict,
    terminal_event: Callable[[str, str | None], dict],
) -> dict | None:
    """Spawn ``python -m <module>``, pipe ``payload`` to its stdin, stream its
    JSON-line stdout events into ``run_id``'s pub/sub, and drain stderr
    concurrently (so a chatty child can't fill the stderr pipe and deadlock the
    stdout read).

    Shared by the workflow runner and the continue-chat turn runner — only the
    payload, the spawned module, and the terminal-event shape differ, the last
    supplied by ``terminal_event(status, error) -> dict``. On any spawn/stdin
    failure, or if the child exits without emitting a terminal ``run_finished``,
    a synthetic terminal is appended so subscribers always observe an end.

    Returns the child's ``run_finished`` event dict if it emitted one, else
    ``None``. The caller owns workdir lifecycle and post-processing.
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except Exception as e:
        ev_mod.append_event(run_id, terminal_event("error", f"failed to spawn {module}: {e}"))
        return None

    ev_mod.set_proc(run_id, proc)

    # Honor a cancel that arrived during the spawn window (before the proc
    # existed, so events.cancel recorded the flag but couldn't signal). Now that
    # we own the proc, terminate it and arm the same SIGKILL escalation. The
    # None-guard matters: a concurrent discard (or the turn GC) can drop the
    # state between set_proc and here.
    _st = ev_mod.get(run_id)
    if _st is not None and _st.cancelled:
        try:
            proc.terminate()
        except Exception:
            pass
        ev_mod.schedule_force_kill(proc)

    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload).encode())
        proc.stdin.close()
    except Exception as e:
        ev_mod.append_event(run_id, terminal_event("error", f"failed to write to {module} stdin: {e}"))
        try:
            proc.kill()
        except Exception:
            pass
        return None

    # Drain stderr concurrently: a child that writes a lot to stderr (MCP
    # connect spew, a long traceback) could otherwise fill the pipe, stall its
    # stdout, and wedge the stdout read below into a wait() deadlock.
    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        try:
            if proc.stderr is not None:
                for chunk in proc.stderr:
                    stderr_chunks.append(chunk)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    terminal: dict | None = None
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev_mod.append_event(run_id, event)
        if event.get("type") == "run_finished":
            terminal = event

    rc = proc.wait()
    stderr_thread.join(timeout=2.0)

    if terminal is None:
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")
        st = ev_mod.get(run_id)
        cancelled = bool(st and st.cancelled)
        if cancelled or rc < 0:
            detail = "cancelled by user" if cancelled else f"{module} killed (rc={rc})"
            ev_mod.append_event(run_id, terminal_event("cancelled", detail))
        else:
            ev_mod.append_event(
                run_id, terminal_event("error", f"{module} exited rc={rc}: {stderr_text[-1000:]}")
            )
    return terminal


def run_workflow_streaming(
    run_id: str,
    workflow: dict,
    inputs: dict[str, Any],
    default_model: str = "",
) -> None:
    """Run a workflow in a child subprocess. Blocks until the child exits.

    Each JSON-line event the child writes to stdout is appended via
    `events.append_event(run_id, ...)`. If the child exits without emitting a
    `run_finished` event (e.g. it crashed or was killed), a synthetic one is
    appended so subscribers always observe a terminal event.
    """
    workdir = tempfile.mkdtemp(prefix="wfrun-")
    try:
        payload = {
            "workflow": workflow,
            "inputs": inputs,
            "default_model": default_model,
            "workdir": workdir,
            "env": build_child_env(),
        }

        def _terminal(status: str, error: str | None) -> dict:
            return {
                "type": "run_finished",
                "status": status,
                "error": error,
                "outputs": {},
                "total_cost": 0.0,
            }

        drive_child_subprocess(run_id, "app.runner.child", payload, _terminal)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_workflow_sync(
    workflow: dict,
    inputs: dict[str, Any],
    default_model: str = "",
) -> dict:
    """Compat wrapper. Runs a workflow to completion and materializes the
    legacy result shape: `{status, error, outputs, node_runs, total_cost}`."""
    run_id = "sync-" + uuid.uuid4().hex[:8]
    run_workflow_streaming(run_id, workflow, inputs, default_model)
    return materialize_run_result(run_id)


def materialize_run_result(run_id: str) -> dict:
    """Aggregate streamed events for a finished run into the legacy result dict."""
    st = ev_mod.get(run_id)
    if not st:
        return {
            "status": "error",
            "error": "no run state",
            "outputs": {},
            "node_runs": [],
            "total_cost": 0.0,
        }

    status = "error"
    error: str | None = None
    outputs: dict = {}
    total_cost = 0.0
    node_runs: list[dict] = []

    for ev in st.events:
        t = ev.get("type")
        if t == "node_finished":
            node_runs.append({k: v for k, v in ev.items() if k != "type"})
        elif t == "run_finished":
            status = ev.get("status", "error")
            error = ev.get("error")
            outputs = ev.get("outputs", {}) or {}
            total_cost = float(ev.get("total_cost", 0.0) or 0.0)

    return {
        "status": status,
        "error": error,
        "outputs": outputs,
        "node_runs": node_runs,
        "total_cost": total_cost,
    }
