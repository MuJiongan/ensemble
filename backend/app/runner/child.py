"""Child subprocess entrypoint.

Reads workflow JSON from stdin, executes nodes as soon as all of their inputs
are ready (concurrent across independent branches), and emits structured
JSON-line events to stdout as work progresses. Any node error cancels the
whole run.

Event types (all on a single line, JSON):

  {"type": "mcp_status",   "servers": {"<name>": {"status": "...", ...}}}
  {"type": "run_started",  "node_count": N, "order": [...]}
  {"type": "node_started", "node_id": "...", "inputs": {...}}
  {"type": "log",          "node_id": "...", "msg": "..."}
  {"type": "llm_call_started",  "node_id": "...", "call_id": "...", "model": "...", "tools": [...], "label"?: "..."}
  {"type": "llm_round_started", "node_id": "...", "call_id": "...", "round": N}
  {"type": "llm_call_chunk",    "node_id": "...", "call_id": "...", "round": N,
                                "kind": "content"|"reasoning"|"tool_args",
                                "delta": "...", "tc_index"?: N, "tool"?: "..."}
  {"type": "llm_call_finished", "node_id": "...", "call_id": "...", "model": "...",
                                "content": "...", "usage": {...}, "cost": 0.0}
  {"type": "tool_call_started",  "node_id": "...", "tool": "...", "args": {...},
                                 "via": "direct"|"llm",
                                 "call_id"?: "...", "tc_index"?: N, "round"?: N}
  {"type": "tool_call_finished", "node_id": "...", "tool": "...", "args": {...},
                                 "result": ..., "via": ...,
                                 "call_id"?: "...", "tc_index"?: N, "round"?: N}
  {"type": "node_finished", "node_id": "...", "status": "...", "inputs": {...}, "outputs": {...},
                            "logs": [...], "llm_calls": [...], "tool_calls": [...],
                            "error": null|"...", "duration_ms": N, "cost": 0.0}
  {"type": "run_finished",  "status": "...", "outputs": {...}, "error": null|"...", "total_cost": 0.0}

A node may invoke ``ctx.call_llm`` from multiple threads concurrently; each
invocation gets its own ``call_id`` and the stdout write below is locked so
the per-line JSON frames don't get interleaved.
"""
from __future__ import annotations
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from pathlib import Path

# Subprocess plumbing (stdin payload, stdout event emit, SIGTERM, MCP connect)
# is shared with the continue-chat turn runner — see app.runner.subprocess_io.
from app.runner.subprocess_io import (
    _emit,
    _install_sigterm_handler,
    _load_mcp_tools,
    _read_payload,
)


@dataclass
class _Schedule:
    """Pre-computed DAG state used to drive node scheduling."""
    nodes_by_id: dict[str, dict]
    incoming: dict[str, list[dict]]
    successors: dict[str, set[str]]
    remaining: dict[str, int]
    order: list[str]


def _build_schedule(workflow: dict) -> _Schedule:
    """Topo-sort + index the workflow so the scheduler can dispatch nodes
    as soon as all of their inputs are satisfied."""
    from app.runner.runner import topo_sort

    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    nodes_by_id = {n["id"]: n for n in nodes}

    incoming: dict[str, list[dict]] = {}
    for e in edges:
        incoming.setdefault(e["to_node_id"], []).append(e)

    successors: dict[str, set[str]] = {nid: set() for nid in nodes_by_id}
    remaining: dict[str, int] = {nid: 0 for nid in nodes_by_id}
    seen_pairs: set[tuple[str, str]] = set()
    for e in edges:
        f, t = e["from_node_id"], e["to_node_id"]
        if f in nodes_by_id and t in nodes_by_id and (f, t) not in seen_pairs:
            seen_pairs.add((f, t))
            successors[f].add(t)
            remaining[t] += 1

    order = topo_sort(nodes, edges)
    return _Schedule(nodes_by_id, incoming, successors, remaining, order)


@dataclass
class _RunState:
    """Mutable shared state threaded through the per-node worker."""
    sched: _Schedule
    user_inputs: dict
    default_model: str
    workdir: Path
    input_node_id: str | None
    output_node_id: str | None
    node_outputs: dict[str, dict] = field(default_factory=dict)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    terminate_event: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    cancel_reason: str | None = None
    total_cost: float = 0.0


def _gather_node_inputs(state: _RunState, node_id: str, input_ports: list[dict]) -> dict:
    """Resolve a node's inputs from upstream outputs (or the user-provided
    inputs for the designated input node)."""
    if node_id == state.input_node_id:
        return dict(state.user_inputs)
    inputs = {p["name"]: None for p in input_ports}
    with state.state_lock:
        for e in state.sched.incoming.get(node_id, []):
            up = state.node_outputs.get(e["from_node_id"])
            inputs[e["to_input"]] = None if up is None else up.get(e["from_output"])
    return inputs


def _emit_node_skipped(node_id: str, inputs: dict, output_ports: list[dict]) -> dict:
    """Emit a node_finished(skipped) event and return the null-output dict."""
    null_out = {p["name"]: None for p in output_ports}
    _emit({
        "type": "node_finished",
        "node_id": node_id,
        "status": "skipped",
        "inputs": inputs,
        "outputs": null_out,
        "logs": [],
        "llm_calls": [],
        "tool_calls": [],
        "error": None,
        "duration_ms": 0,
        "cost": 0.0,
    })
    return null_out


def _execute_node(node: dict, ctx, inputs: dict) -> dict:
    """Exec the node's user code and return its (port-normalised) outputs.
    Raises if the code is malformed or returns the wrong shape."""
    ns: dict = {}
    exec(node.get("code") or "", ns, ns)
    run_fn = ns.get("run")
    if not callable(run_fn):
        raise RuntimeError("node code must define `run(inputs, ctx)` function")
    result = run_fn(inputs, ctx)
    if not isinstance(result, dict):
        raise RuntimeError(f"node returned {type(result).__name__}, expected dict")
    output_ports = node.get("outputs") or []
    if output_ports:
        return {p["name"]: result.get(p["name"]) for p in output_ports}
    return result


def _record_node_success(
    state: _RunState, node_id: str, inputs: dict, outputs: dict, ctx, start: float
) -> None:
    cost = sum(float(c.get("cost", 0.0) or 0.0) for c in ctx.llm_calls)
    with state.state_lock:
        state.node_outputs[node_id] = outputs
        state.total_cost += cost
    _emit({
        "type": "node_finished",
        "node_id": node_id,
        "status": "success",
        "inputs": inputs,
        "outputs": outputs,
        "logs": ctx.logs,
        "llm_calls": ctx.llm_calls,
        "tool_calls": ctx.tool_calls,
        "error": None,
        "duration_ms": int((time.time() - start) * 1000),
        "cost": cost,
    })


def _record_node_error(
    state: _RunState, node_id: str, inputs: dict, output_ports: list[dict],
    ctx, start: float, exc: BaseException,
) -> None:
    err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    null_out = {p["name"]: None for p in output_ports}
    cost = sum(float(c.get("cost", 0.0) or 0.0) for c in ctx.llm_calls)
    with state.state_lock:
        state.node_outputs[node_id] = null_out
        state.total_cost += cost
        if state.error is None:
            state.error = err
    state.terminate_event.set()
    _emit({
        "type": "node_finished",
        "node_id": node_id,
        "status": "error",
        "inputs": inputs,
        "outputs": null_out,
        "logs": ctx.logs,
        "llm_calls": ctx.llm_calls,
        "tool_calls": ctx.tool_calls,
        "error": err,
        "duration_ms": int((time.time() - start) * 1000),
        "cost": cost,
    })


def _run_node(state: _RunState, node_id: str) -> None:
    """Execute one node: gather inputs, optionally skip, exec user code,
    record outputs, and emit a node_finished event with the run trace."""
    if state.terminate_event.is_set():
        return

    from app.runner.ctx import Ctx

    node = state.sched.nodes_by_id[node_id]
    input_ports = node.get("inputs") or []
    output_ports = node.get("outputs") or []
    config = node.get("config") or {}
    node_model = config.get("model") or state.default_model

    inputs = _gather_node_inputs(state, node_id, input_ports)

    skip = any(
        p.get("required", True) and inputs.get(p["name"]) is None
        for p in input_ports
    )
    if skip:
        null_out = _emit_node_skipped(node_id, inputs, output_ports)
        with state.state_lock:
            state.node_outputs[node_id] = null_out
        return

    _emit({"type": "node_started", "node_id": node_id, "inputs": inputs})

    def _on_event(ev: dict, _nid: str = node_id) -> None:
        ev_payload = dict(ev)
        ev_payload.setdefault("node_id", _nid)
        _emit(ev_payload)

    ctx = Ctx(workdir=state.workdir, default_model=node_model, on_event=_on_event)
    start = time.time()
    try:
        outputs = _execute_node(node, ctx, inputs)
        _record_node_success(state, node_id, inputs, outputs, ctx, start)
    except Exception as e:
        _record_node_error(state, node_id, inputs, output_ports, ctx, start, e)


def _emit_synthetic_node_error(state: _RunState, node_id: str, exc: BaseException) -> None:
    """A node thread escaped its own try/except — emit a synthetic
    node_finished so the frontend dot doesn't stay stuck on running."""
    err_msg = f"{type(exc).__name__}: {exc}"
    if state.error is None:
        state.error = err_msg
    _emit({
        "type": "node_finished",
        "node_id": node_id,
        "status": "error",
        "inputs": {},
        "outputs": {},
        "logs": [],
        "llm_calls": [],
        "tool_calls": [],
        "error": err_msg,
        "duration_ms": 0,
        "cost": 0.0,
    })


def _drive_scheduler(state: _RunState, pool: ThreadPoolExecutor) -> None:
    """Submit nodes whose dependencies are satisfied; advance as each
    finishes. Bails on KeyboardInterrupt (caller marks the run cancelled)."""
    in_flight: dict = {}
    for nid in state.sched.order:
        if state.sched.remaining[nid] == 0:
            in_flight[pool.submit(_run_node, state, nid)] = nid

    while in_flight:
        done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
        for fut in done:
            finished_id = in_flight.pop(fut)
            exc = fut.exception()
            if exc is not None:
                # Most failures emit `node_finished` from inside `_run_node`'s
                # own try/except. Exceptions outside that block (Ctx build,
                # event emit) escape here.
                _emit_synthetic_node_error(state, finished_id, exc)
                state.terminate_event.set()
                continue
            if state.terminate_event.is_set():
                continue
            for s in state.sched.successors[finished_id]:
                state.sched.remaining[s] -= 1
                if state.sched.remaining[s] == 0:
                    in_flight[pool.submit(_run_node, state, s)] = s


def _emit_run_finished(state: _RunState) -> None:
    """Emit the terminal run_finished event, choosing status from accumulated
    state. Force-exits on cancel so any node thread blocked in an LLM stream
    can't keep the subprocess alive."""
    final_outputs = (
        state.node_outputs.get(state.output_node_id, {}) if state.output_node_id else {}
    )

    if state.cancel_reason is not None:
        _emit({
            "type": "run_finished",
            "status": "cancelled",
            "error": state.cancel_reason,
            "outputs": final_outputs,
            "total_cost": state.total_cost,
        })
        # Force-exit — _exit skips finalizers but stdout was just flushed
        # by _emit so the parent has the cancelled event in hand.
        os._exit(0)

    if state.error is not None:
        _emit({
            "type": "run_finished",
            "status": "error",
            "error": state.error,
            "outputs": final_outputs,
            "total_cost": state.total_cost,
        })
        return

    _emit({
        "type": "run_finished",
        "status": "success",
        "error": None,
        "outputs": final_outputs,
        "total_cost": state.total_cost,
    })


def main() -> None:
    _install_sigterm_handler()
    payload = _read_payload()
    workflow = payload["workflow"]
    workdir = Path(payload["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)

    mcp_manager = _load_mcp_tools()

    try:
        sched = _build_schedule(workflow)
    except Exception as e:
        _emit({
            "type": "run_finished",
            "status": "error",
            "error": str(e),
            "outputs": {},
            "total_cost": 0.0,
        })
        return

    state = _RunState(
        sched=sched,
        user_inputs=payload.get("inputs") or {},
        default_model=payload.get("default_model") or "",
        workdir=workdir,
        input_node_id=workflow.get("input_node_id"),
        output_node_id=workflow.get("output_node_id"),
    )

    _emit({"type": "run_started", "node_count": len(sched.order), "order": sched.order})

    pool = ThreadPoolExecutor(max_workers=max(1, len(sched.order)))
    try:
        _drive_scheduler(state, pool)
    except KeyboardInterrupt:
        state.cancel_reason = "cancelled by user"
        state.terminate_event.set()

    # On cancel, don't wait for in-flight node threads — they may be blocked
    # in `ctx.call_llm` (httpx with no timeout, deliberately, so streams don't
    # truncate). Python signals are delivered to the main thread only, so a
    # node thread won't see SIGTERM. Waiting here means the user has to click
    # cancel a second time; skip the wait and forcibly exit below.
    cancelled = state.cancel_reason is not None
    pool.shutdown(wait=not cancelled, cancel_futures=cancelled)

    # On a clean finish, close MCP transports so local stdio servers get a
    # graceful shutdown. On cancel we skip it — `_emit_run_finished` force-exits
    # the process, which tears down the child servers anyway, and we don't want
    # to block the cancel on a slow server.
    if mcp_manager is not None and not cancelled:
        mcp_manager.shutdown()

    _emit_run_finished(state)


if __name__ == "__main__":
    main()
