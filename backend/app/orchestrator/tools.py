"""Orchestrator tool surface — graph mutators the LLM can call.

Each function takes (db, workflow_id, **args) and either returns a JSON-serialisable
dict (the tool result the LLM sees) or raises a ValueError on bad input. The
agent loop catches errors and returns them as `{"error": ...}` results so the
LLM can self-correct.
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app import models, schemas

_OK: dict[str, bool] = {"ok": True}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _get_workflow(db: DbSession, wid: str) -> models.Workflow:
    w = db.get(models.Workflow, wid)
    if not w:
        raise ValueError(f"workflow {wid} not found")
    return w


def _get_node(db: DbSession, wid: str, node_id: str) -> models.Node:
    n = db.get(models.Node, node_id)
    if not n or n.workflow_id != wid:
        raise ValueError(f"node {node_id} not found in workflow {wid}")
    return n


def _normalize_ports(ports: list[Any] | None, kind: str) -> list[dict]:
    """Coerce an LLM-supplied list of port dicts into IOPort shape."""
    out: list[dict] = []
    for p in (ports or []):
        if not isinstance(p, dict):
            raise ValueError(f"{kind} entry must be an object, got {type(p).__name__}")
        name = p.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"{kind} entry missing 'name'")
        out.append(
            {
                "name": name,
                "type_hint": p.get("type_hint", "any") or "any",
                "required": bool(p.get("required", kind == "input")),
            }
        )
    return out


def _next_position(db: DbSession, wid: str) -> dict:
    existing = db.query(models.Node).filter_by(workflow_id=wid).all()
    n = len(existing)
    # Lay out nodes left-to-right in a gentle wave so the orchestrator's
    # auto-built graphs aren't a pile.
    x = 60 + (n % 4) * 280
    y = 60 + (n // 4) * 200 + int(math.sin(n) * 24)
    return {"x": float(x), "y": float(y)}


def _node_full(n: models.Node) -> dict:
    """Full node payload — used by view_node_details. No truncation.
    ``id`` is omitted (the caller already passed ``node_id``). ``position``
    is deliberately omitted too: the LLM can't move nodes (no
    `set_position` tool — `add_node` auto-lays them out)."""
    return {
        "name": n.name,
        "description": n.description or "",
        "code": n.code or "",
        "inputs": n.inputs or [],
        "outputs": n.outputs or [],
    }


def _edge_summary(e: models.Edge) -> dict:
    return {
        "id": e.id,
        "from_node_id": e.from_node_id,
        "from_output": e.from_output,
        "to_node_id": e.to_node_id,
        "to_input": e.to_input,
    }


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------


def add_node(
    db: DbSession,
    wid: str,
    *,
    name: str,
    description: str = "",
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
) -> dict:
    """Create a new node in the workflow. The node is created with the
    default code stub — call ``configure_node`` to write its actual code.
    Returns the new node id."""
    _get_workflow(db, wid)
    n = models.Node(
        workflow_id=wid,
        name=name,
        description=description or "",
        code=schemas.DEFAULT_CODE,
        inputs=_normalize_ports(inputs, "input"),
        outputs=_normalize_ports(outputs, "output"),
        position=_next_position(db, wid),
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return {"node_id": n.id}


def remove_node(db: DbSession, wid: str, *, node_id: str) -> dict:
    n = _get_node(db, wid, node_id)
    from app.services.graph import cascade_delete_node
    cascade_delete_node(db, n)
    db.commit()
    return _OK


def rename_node(db: DbSession, wid: str, *, node_id: str, new_name: str) -> dict:
    if not new_name:
        raise ValueError("new_name must be non-empty")
    n = _get_node(db, wid, node_id)
    n.name = new_name
    db.commit()
    return _OK


def rename_project(db: DbSession, wid: str, *, new_name: str) -> dict:
    """Rename the current project (workflow)."""
    if not new_name.strip():
        raise ValueError("new_name must be non-empty")
    w = _get_workflow(db, wid)
    w.name = new_name.strip()
    db.commit()
    return _OK


def configure_node(
    db: DbSession,
    wid: str,
    *,
    node_id: str,
    description: str | None = None,
    code: str | None = None,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
) -> dict:
    """Patch a node's description, code, and/or ports.

    Omitted optional fields are left unchanged."""
    n = _get_node(db, wid, node_id)
    if description is not None:
        n.description = description
    if code is not None:
        n.code = code
    if inputs is not None:
        n.inputs = _normalize_ports(inputs, "input")
    if outputs is not None:
        n.outputs = _normalize_ports(outputs, "output")
    db.commit()
    db.refresh(n)
    return _OK


def add_edge(
    db: DbSession,
    wid: str,
    *,
    from_node_id: str,
    from_output: str,
    to_node_id: str,
    to_input: str,
) -> dict:
    """Connect one node's output to another node's input. Validates the ports
    exist on each side. Returns the new edge id."""
    src = _get_node(db, wid, from_node_id)
    dst = _get_node(db, wid, to_node_id)
    src_out_names = [p.get("name") for p in (src.outputs or [])]
    dst_in_names = [p.get("name") for p in (dst.inputs or [])]
    if from_output not in src_out_names:
        raise ValueError(
            f"node {src.name!r} has no output named {from_output!r} (available: {src_out_names})"
        )
    if to_input not in dst_in_names:
        raise ValueError(
            f"node {dst.name!r} has no input named {to_input!r} (available: {dst_in_names})"
        )
    e = models.Edge(
        workflow_id=wid,
        from_node_id=from_node_id,
        from_output=from_output,
        to_node_id=to_node_id,
        to_input=to_input,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"edge_id": e.id}


def remove_edge(db: DbSession, wid: str, *, edge_id: str) -> dict:
    e = db.get(models.Edge, edge_id)
    if not e or e.workflow_id != wid:
        raise ValueError(f"edge {edge_id} not found in workflow {wid}")
    db.delete(e)
    db.commit()
    return _OK


def clean_canvas(db: DbSession, wid: str) -> dict:
    """Wipe the workflow's graph: delete every node + edge and clear the
    input/output node pointers. The Workflow row itself stays (so the
    orchestrator session, runs, and run history are preserved).

    A session often spans multiple distinct workflows in sequence — e.g. a
    scoping workflow, then a solve workflow informed by what it found, then
    maybe a verify workflow. `clean_canvas` is the *transition* between
    stages: clear the canvas, build the next stage's graph, run it.
    """
    w = _get_workflow(db, wid)
    n_edges = db.query(models.Edge).filter_by(workflow_id=wid).delete(synchronize_session=False)
    n_nodes = db.query(models.Node).filter_by(workflow_id=wid).delete(synchronize_session=False)
    w.input_node_id = None
    w.output_node_id = None
    db.commit()
    return _OK


def set_input_node(db: DbSession, wid: str, *, node_id: str) -> dict:
    n = _get_node(db, wid, node_id)
    w = _get_workflow(db, wid)
    w.input_node_id = n.id
    db.commit()
    return _OK


def set_output_node(db: DbSession, wid: str, *, node_id: str) -> dict:
    n = _get_node(db, wid, node_id)
    w = _get_workflow(db, wid)
    w.output_node_id = n.id
    db.commit()
    return _OK


# ---------------------------------------------------------------------------
# read-only inspection tools
# ---------------------------------------------------------------------------


def view_graph(db: DbSession, wid: str) -> dict:
    """Return a structural snapshot of the workflow — node ids, names,
    descriptions, ports. Code is intentionally omitted; call
    `view_node_details` for the full body of a specific node."""
    w = _get_workflow(db, wid)
    nodes = []
    for n in w.nodes:
        nodes.append(
            {
                "id": n.id,
                "name": n.name,
                "description": n.description or "",
                "inputs": n.inputs or [],
                "outputs": n.outputs or [],
            }
        )
    edges = [_edge_summary(e) for e in w.edges]
    return {
        "name": w.name,
        "input_node_id": w.input_node_id,
        "output_node_id": w.output_node_id,
        "nodes": nodes,
        "edges": edges,
    }


def view_node_details(db: DbSession, wid: str, *, node_id: str) -> dict:
    """Return the full record for a node — including untruncated code and
    full config."""
    n = _get_node(db, wid, node_id)
    return _node_full(n)


def get_mcp_tool_schema(db: DbSession, wid: str, *, server: str, tool: str) -> dict:
    """Return the complete, untruncated JSON input schema for one MCP tool.

    The per-turn MCP advertisement caps each tool's schema to keep the prompt
    small, so a large tool (e.g. Notion's create_pages) shows up truncated.
    Call this to get the full schema before writing a *direct*
    ``ctx.tools.<server>.<tool>(...)`` call with nested/union arguments —
    otherwise the args you hand-write may fail the server's validation."""
    import os

    raw = os.getenv("MCP_SERVERS", "")
    if not raw.strip():
        return {"error": "no MCP servers configured in Settings"}
    try:
        from app.runner import mcp as mcp_mod
        from app.db import SessionLocal

        descriptors = mcp_mod.discover(raw, db_factory=SessionLocal)
    except Exception as e:  # pragma: no cover — defensive
        return {"error": f"could not load MCP tools: {type(e).__name__}: {e}"}
    if not descriptors:
        return {"error": "no MCP tools available (servers unreachable or none enabled)"}

    s = (server or "").strip()
    t = (tool or "").strip()
    match = next(
        (
            d
            for d in descriptors
            if (d.server_attr == s or d.server == s)
            and (d.tool_attr == t or d.tool == t or d.qualified == t)
        ),
        None,
    )
    if match is None:
        available = sorted(f"{d.server_attr}.{d.tool_attr}" for d in descriptors)
        return {"error": f"no MCP tool '{s}.{t}'; available: {available}"}
    return {
        "server": match.server_attr,
        "tool": match.tool_attr,
        "call": f"ctx.tools.{match.server_attr}.{match.tool_attr}(...)",
        "llm_name": match.qualified,
        "description": match.description or "",
        "input_schema": match.input_schema or {},
    }


# ---------------------------------------------------------------------------
# execution tools — `run_agent` blocks on one ephemeral node and returns its
# outputs inline; `run_workflow` starts the persisted graph in the background
# and emits a run id for the workspace UI.
# ---------------------------------------------------------------------------


def run_agent(
    db: DbSession,
    wid: str,
    *,
    name: str,
    description: str,
    code: str,
    _cancel_event: Any = None,
    _event_callback: Any = None,
) -> dict:
    """Execute node-shaped code inline and wait for its outputs.

    The supplied code uses the same ``run(inputs, ctx)`` contract as a normal
    node (with an empty inputs dict), but needs no declared ports because it has
    no edges or rerun form. It never touches the canvas or user-facing run
    history. A hidden completed NodeRun owns the inspectable trace and resumable
    LLM transcripts; the orchestrator tool call remains its visible record.
    """
    _get_workflow(db, wid)

    clean_name = (name or "").strip()
    clean_code = (code or "").strip()
    if not clean_name:
        return {"error": "name must be non-empty"}
    if not clean_code:
        return {"error": "code must be non-empty"}

    # Resolve the same default model a persisted node run would use.
    import os as _os
    default_model = (_os.getenv("DEFAULT_NODE_MODEL") or "").strip()
    if not default_model:
        setting = db.query(models.Setting).filter_by(key="default_node_model").first()
        default_model = (setting.value if setting and setting.value else "").strip()
    if not default_model:
        return {
            "error": (
                "No node model configured. Ask the user to set a default node model "
                "in Settings before running."
            )
        }

    # Reuse the normal isolated node runtime. Events are forwarded into the
    # orchestrator turn (not the workspace run channel) so the inline card can
    # render the same live trace as a normal node while this call blocks.
    from app.runner.runner import run_workflow_sync
    from app.runner.service import _split_call_transcripts

    node_id = "inline_agent"
    workflow = {
        "id": "inline_agent",
        "input_node_id": node_id,
        "output_node_id": node_id,
        "nodes": [
            {
                "id": node_id,
                "name": clean_name,
                "description": (description or "").strip(),
                "code": clean_code + "\n",
                # Port declarations only matter to a persisted graph. Empty
                # outputs preserve the complete dict returned by the code.
                "inputs": [],
                "outputs": [],
                "position": {"x": 0, "y": 0},
            }
        ],
        "edges": [],
    }
    started_at = datetime.utcnow()
    result = run_workflow_sync(
        workflow,
        {},
        default_model,
        cancel_event=_cancel_event,
        on_event=_event_callback,
    )
    ended_at = datetime.utcnow()
    node_run = next(iter(result.get("node_runs") or []), {})
    lean_calls, transcripts = _split_call_transcripts(node_run.get("llm_calls") or [])

    # Persist the completed execution under a private run kind. It is excluded
    # from every run list and never emits run_started, but gives continuation
    # chat the exact NodeRun + CallTranscript ownership it already understands.
    run_status = result.get("status") or "error"
    raw_node_status = node_run.get("status")
    node_status = raw_node_status if raw_node_status in {
        "pending", "running", "success", "error", "skipped"
    } else ("success" if run_status == "success" else "error")
    hidden_run = models.Run(
        workflow_id=wid,
        kind="inline_agent",
        status=run_status,
        inputs={},
        outputs=result.get("outputs") or {},
        error=result.get("error"),
        started_at=started_at,
        ended_at=ended_at,
        total_cost=float(result.get("total_cost") or 0.0),
        workflow_snapshot=workflow,
    )
    db.add(hidden_run)
    db.flush()
    persisted_node_run = models.NodeRun(
        run_id=hidden_run.id,
        node_id=node_id,
        status=node_status,
        inputs=node_run.get("inputs") or {},
        outputs=node_run.get("outputs") or result.get("outputs") or {},
        logs=node_run.get("logs") or [],
        llm_calls=lean_calls,
        tool_calls=node_run.get("tool_calls") or [],
        error=node_run.get("error") or result.get("error"),
        duration_ms=int(node_run.get("duration_ms") or 0),
        cost=float(node_run.get("cost") or result.get("total_cost") or 0.0),
    )
    db.add(persisted_node_run)
    db.flush()
    for call_id, messages in transcripts:
        db.add(models.CallTranscript(
            node_run_id=persisted_node_run.id,
            call_id=call_id,
            messages=messages,
        ))
    db.commit()

    display_node_run = {
        "id": persisted_node_run.id,
        "node_id": node_id,
        "status": persisted_node_run.status,
        "inputs": persisted_node_run.inputs or {},
        "outputs": persisted_node_run.outputs or {},
        "logs": persisted_node_run.logs or [],
        "llm_calls": persisted_node_run.llm_calls or [],
        "tool_calls": persisted_node_run.tool_calls or [],
        "error": persisted_node_run.error,
        "duration_ms": persisted_node_run.duration_ms or 0,
        "cost": persisted_node_run.cost or 0.0,
    }
    return {
        "status": result.get("status", "error"),
        "outputs": result.get("outputs") or {},
        "error": result.get("error"),
        "duration_ms": int(node_run.get("duration_ms") or 0),
        "total_cost": float(result.get("total_cost") or 0.0),
        # Stored/emitted for the chat UI; persistence._row_to_message removes
        # this field before the result is replayed to the orchestrator model.
        "_display": {
            "run_id": hidden_run.id,
            "node_run": display_node_run,
        },
    }


def run_workflow(
    db: DbSession,
    wid: str,
    *,
    inputs: dict | None = None,
) -> dict:
    """Kick off a workflow run with the given inputs in a background thread
    and return immediately with ``{run_id, status: "running"}``. The agent
    loop turns this into a ``run_started`` chat event so the run panel can
    attach to the WS, then passes this start result to the LLM without
    blocking on completion.
    """
    # Lazy imports to avoid a load-time cycle between the orchestrator package
    # and the api routers.
    from app.api.runs import _serialize_workflow
    from app.runner import service as run_service

    w = _get_workflow(db, wid)

    if not w.input_node_id:
        return {"error": "workflow has no input node — designate one with set_input_node first"}
    if not w.output_node_id:
        return {"error": "workflow has no output node — designate one with set_output_node first"}

    inputs = inputs or {}
    input_node = db.get(models.Node, w.input_node_id)
    if input_node is None:
        return {"error": f"input node {w.input_node_id} not found"}
    declared_names = {p.get("name") for p in (input_node.inputs or [])}
    required_names = {p.get("name") for p in (input_node.inputs or []) if p.get("required")}
    missing = sorted(required_names - set(inputs.keys()))
    if missing:
        return {"error": f"missing required inputs on {input_node.name!r}: {missing}"}
    extra = sorted(set(inputs.keys()) - declared_names)
    if extra:
        return {"error": f"unknown inputs for {input_node.name!r}: {extra} (declared: {sorted(declared_names)})"}

    # Resolve the node model, mirroring the API's lookup. No hardcoded default —
    # if the user hasn't configured one, tell them to set it in Settings rather
    # than guessing a model that may not match the connected provider.
    import os as _os
    default_model = _os.getenv("DEFAULT_NODE_MODEL", "")
    if not default_model:
        setting = db.query(models.Setting).filter_by(key="default_node_model").first()
        default_model = setting.value if setting and setting.value else ""
    if not default_model:
        return {"error": "No node model configured. Ask the user to set a default node model in Settings before running."}

    wf_data = _serialize_workflow(w)

    run = models.Run(
        workflow_id=wid,
        kind="orchestrator",
        status="running",
        inputs=inputs,
        workflow_snapshot=wf_data,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    # The agent loop will yield a `run_started` chat event with this run_id
    # so the frontend can attach while the run continues independently.
    run_service.start_run(run_id, wf_data, inputs, default_model)

    return {"run_id": run_id, "status": "running"}


def cancel_run(db: DbSession, wid: str, *, run_id: str) -> dict:
    """Cancel a workflow run in this workflow.

    Signals the live subprocess when one is tracked. If the DB row still reads
    running/pending but no in-memory state owns it, reconcile it to cancelled so
    it does not remain stuck.
    """
    from app.runner import service as run_service

    run = db.get(models.Run, run_id)
    if run is None or run.workflow_id != wid:
        return {"error": f"run {run_id} not found in workflow {wid}"}

    if run_service.cancel(run_id):
        return {"run_id": run_id, "cancelled": True}

    if run.status in ("running", "pending") and not run_service.has_state(run_id):
        run.status = "cancelled"
        run.error = run.error or "cancelled (run was no longer active)"
        run.ended_at = run.ended_at or datetime.utcnow()
        db.commit()
        run_service.discard(run_id)
        return {"run_id": run_id, "cancelled": True}

    return {"run_id": run_id, "cancelled": False, "reason": f"run is already {run.status}"}


def wait_for_run(
    db: DbSession,
    wid: str,
    run_id: str,
    *,
    cancel_event: Any = None,
    poll_interval: float = 0.1,
) -> dict:
    """Block until the given run finishes, then return the lean
    ``{run_id, status, total_cost}`` shape ``run_workflow`` exposes to the LLM.
    On a non-success status, ``error`` and ``node_errors`` (which node failed,
    with what message) are included too — that's the orchestrator's failure
    signal; per-node detail beyond the message goes through :func:`view_run`.

    Reads the terminal state from the in-memory event stream — *not* the DB.
    The runner's ``finished_event`` fires the instant ``run_finished`` is
    appended to the event log, but ``_execute_run`` persists to the DB
    *after* that, in a background thread. Querying the DB on this edge
    returns the stale ``status="running"`` row that ``run_workflow`` created
    at the top, which is exactly the race we hit before this fix. The
    materialised event stream is authoritative — that's what the run panel
    renders too.

    If ``cancel_event`` is provided and gets set, returns early with a
    cancellation result; the run keeps executing in the background.
    """
    from app.runner import events as ev_mod
    from app.runner.runner import materialize_run_result

    run = db.get(models.Run, run_id)
    if run is None or run.workflow_id != wid:
        return {"error": f"run {run_id} not found in workflow {wid}"}

    st = ev_mod.get_or_create(run_id)
    while not st.finished:
        if cancel_event is not None and cancel_event.is_set():
            return {
                "run_id": run_id,
                "status": "running",
                "error": "orchestrator turn cancelled while waiting; run is still in progress",
            }
        st.finished_event.wait(timeout=poll_interval)

    result = materialize_run_result(run_id)
    status = result.get("status") or "error"
    out = {
        "run_id": run_id,
        "status": status,
        "total_cost": float(result.get("total_cost") or 0.0),
    }
    if status != "success":
        # Name the failing node(s) using the snapshot the run executed against,
        # so a later rename doesn't mislabel the failure.
        snap_nodes = (run.workflow_snapshot or {}).get("nodes") or []
        node_names = {sn.get("id"): sn.get("name") for sn in snap_nodes}
        out["error"] = result.get("error")
        out["node_errors"] = [
            {
                "node_id": nr.get("node_id"),
                "node_name": node_names.get(nr.get("node_id")) or nr.get("node_id"),
                "error": nr.get("error") or "unknown error",
            }
            for nr in result.get("node_runs") or []
            if nr.get("status") == "error"
        ]
    return out


_NODE_FIELDS: tuple[str, ...] = ("inputs", "outputs", "logs")


_DEFAULT_RUN_LIST_LIMIT = 20
_MAX_RUN_LIST_LIMIT = 100


def list_runs(
    db: DbSession,
    wid: str,
    *,
    limit: int = _DEFAULT_RUN_LIST_LIMIT,
    kind: str | None = None,
) -> dict:
    """List historic runs for the workflow, most recent first. Returns a
    lean shape per run — ``{run_id, status, kind, started_at, ended_at,
    total_cost, error}`` — that's enough to identify a run; drill into
    contents with :func:`view_run`.

    ``kind`` on returned rows is one of:
    - ``"user"`` — the user clicked Run (or hit the REST API directly).
    - ``"orchestrator"`` — *you* started it via :func:`run_workflow`.

    Pass ``kind="user"`` or ``kind="orchestrator"`` to filter the list to
    one source.

    ``limit`` is clamped to ``[1, 100]`` (default 20). Older runs beyond the
    limit aren't returned; raise ``limit`` if the user asks about a run that
    isn't in the first page.
    """
    _get_workflow(db, wid)  # 404 the workflow rather than silently returning [].

    if not isinstance(limit, int) or limit < 1:
        limit = _DEFAULT_RUN_LIST_LIMIT
    limit = min(limit, _MAX_RUN_LIST_LIMIT)
    if kind is not None and kind not in {"user", "orchestrator"}:
        raise ValueError('kind must be "user" or "orchestrator"')

    query = db.query(models.Run).filter(
        models.Run.workflow_id == wid,
        models.Run.kind != "inline_agent",
    )
    if kind is not None:
        query = query.filter(models.Run.kind == kind)

    rows = query.order_by(models.Run.started_at.desc()).limit(limit).all()

    runs = [
        {
            "run_id": r.id,
            "status": r.status,
            "kind": r.kind,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "total_cost": float(r.total_cost or 0.0),
            "error": r.error,
        }
        for r in rows
    ]
    return {"runs": runs, "count": len(runs), "limit": limit, "kind": kind}


def view_run(
    db: DbSession,
    wid: str,
    *,
    run_id: str,
    node_id: str | None = None,
    fields: list[str] | None = None,
    ports: list[str] | None = None,
) -> dict:
    """Return one node's record within a run. There is no run-level dump —
    both ``node_id`` and ``fields`` are required, so every read names the
    node and the slice it's after.

    Always includes the lightweight metadata
    ``{node_name, status, error, duration_ms, cost}``; the
    heavy fields ``inputs``, ``outputs``, ``logs`` are gated by ``fields`` —
    a non-empty subset of ``["inputs", "outputs", "logs"]``. The workflow's
    result lives on the output node: ``node_id=<output node id>,
    fields=["outputs"]``.

    ``ports`` narrows further: the returned ``inputs``/``outputs`` dicts are
    filtered to just those port names, so a read never pays for ports it
    doesn't need. It's *required* whenever ``fields`` includes ``inputs`` or
    ``outputs`` (and rejected on a logs-only read, where there's nothing to
    filter). Errors if a named port exists on none of the selected dicts
    (typo guard).
    """
    if not node_id:
        return {
            "error": (
                "`node_id` is required — view_run reads one node's record. "
                "Get node ids from view_graph; use the output node's id to "
                "read the workflow's result."
            )
        }
    if not isinstance(fields, list) or not fields:
        return {
            "error": (
                f"`fields` is required — pass a non-empty subset of "
                f"{list(_NODE_FIELDS)}"
            )
        }
    bad = [f for f in fields if f not in _NODE_FIELDS]
    if bad:
        return {
            "error": (
                f"unknown field(s) {bad}; allowed: {list(_NODE_FIELDS)}"
            )
        }
    selected: tuple[str, ...] = tuple(dict.fromkeys(fields))  # dedupe, preserve order

    wants_dicts = any(f in ("inputs", "outputs") for f in selected)
    if wants_dicts:
        if not isinstance(ports, list) or not ports:
            return {
                "error": (
                    "`ports` is required when reading `inputs`/`outputs` — "
                    "name the port(s) you need (declared on the node; see "
                    "view_graph)"
                )
            }
    elif ports is not None:
        return {
            "error": (
                "`ports` filters the `inputs`/`outputs` dicts — it doesn't "
                "apply to a logs-only read"
            )
        }

    out = _materialise_node_run(db, wid, run_id, node_id, selected)
    # Bare error dicts (run/node not found) have no `status`; the per-node
    # record always does — and its `error` key is None on success, so the
    # key's presence can't distinguish the two shapes.
    if not wants_dicts or "status" not in out:
        return out

    available: set[str] = set()
    for f in ("inputs", "outputs"):
        if isinstance(out.get(f), dict):
            available |= set(out[f].keys())
    missing = [p for p in ports if p not in available]
    if missing:
        return {
            "error": (
                f"unknown port(s) {missing} on node {node_id} in run {run_id}; "
                f"available: {sorted(available)}"
            )
        }
    for f in ("inputs", "outputs"):
        if isinstance(out.get(f), dict):
            out[f] = {k: v for k, v in out[f].items() if k in ports}
    return out


def _materialise_node_run(
    db: DbSession,
    wid: str,
    run_id: str,
    node_id: str,
    fields: tuple[str, ...],
) -> dict:
    """Read a single NodeRun row for ``(run_id, node_id)`` into a result dict.
    Only the requested ``fields`` (subset of ``inputs``/``outputs``/``logs``)
    are populated; the lightweight metadata is always included.
    Returns ``{error: ...}`` if the run isn't in this workflow or the node
    didn't execute (e.g. upstream failed before reaching it)."""
    import time
    from app.runner import events as ev_mod

    db.expire_all()
    run = db.get(models.Run, run_id)
    if run is None or run.workflow_id != wid:
        return {"error": f"run {run_id} not found in workflow {wid}"}

    # Persist race: the runner appends `run_finished` (which sets
    # `finished_event`) *before* `_execute_run` materialises and commits the
    # final state to the DB. If the orchestrator calls `view_run` right
    # after `run_workflow` returns on a fast run, we can land here while
    # the in-memory stream says finished but the NodeRun rows aren't
    # committed yet. Briefly poll for the persist to catch up (bounded;
    # we'd rather return stale than hang).
    st = ev_mod.get(run_id)
    if st is not None and st.finished and run.status == "running":
        for _ in range(50):  # up to ~5s
            time.sleep(0.1)
            db.expire_all()
            run = db.get(models.Run, run_id)
            if run is None or run.status != "running":
                break

    if run is None:
        return {"error": f"run {run_id} produced no result row"}

    nr = next((x for x in (run.node_runs or []) if x.node_id == node_id), None)
    if nr is None:
        return {
            "error": (
                f"node {node_id} did not execute in run {run_id} "
                "(no NodeRun row — likely never reached, e.g. upstream failed)"
            )
        }

    # Resolve a friendly name from the workflow snapshot first (so it matches
    # what the run actually executed against), falling back to the live graph.
    node_name = node_id
    snap = run.workflow_snapshot or {}
    for sn in snap.get("nodes") or []:
        if sn.get("id") == node_id:
            node_name = sn.get("name") or node_id
            break
    else:
        live = db.get(models.Node, node_id)
        if live is not None and live.workflow_id == wid:
            node_name = live.name

    out: dict = {
        "node_name": node_name,
        "status": nr.status,
        "error": nr.error,
        "duration_ms": int(nr.duration_ms or 0),
        "cost": float(nr.cost or 0.0),
    }
    if "inputs" in fields:
        out["inputs"] = nr.inputs or {}
    if "outputs" in fields:
        out["outputs"] = nr.outputs or {}
    if "logs" in fields:
        out["logs"] = nr.logs or []
    return out


# ---------------------------------------------------------------------------
# registry + LLM tool schemas
# ---------------------------------------------------------------------------


REGISTRY = {
    "view_graph": view_graph,
    "view_node_details": view_node_details,
    "get_mcp_tool_schema": get_mcp_tool_schema,
    "add_node": add_node,
    "remove_node": remove_node,
    "rename_node": rename_node,
    "rename_project": rename_project,
    "configure_node": configure_node,
    "add_edge": add_edge,
    "remove_edge": remove_edge,
    "set_input_node": set_input_node,
    "set_output_node": set_output_node,
    "clean_canvas": clean_canvas,
    "run_agent": run_agent,
    "run_workflow": run_workflow,
    "cancel_run": cancel_run,
    "list_runs": list_runs,
    "view_run": view_run,
}


# Tools that don't mutate the workflow graph — exempt from the dispatcher's
# run-in-progress lock (which only blocks graph mutation). Includes the
# read-only inspection tools and `run_workflow` itself — `run_workflow` does
# its own active-run check internally with a clearer error message than the
# generic "cannot mutate the graph" guard.
NON_GRAPH_MUTATING_TOOLS: set[str] = {
    "view_graph",
    "view_node_details",
    "get_mcp_tool_schema",
    "list_runs",
    "view_run",
    "run_agent",
    "run_workflow",
    "cancel_run",
    "rename_project",
}


_PORT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "type_hint": {"type": "string", "description": "human-readable type label, e.g. 'list[path]', 'dict', 'string'"},
            "required": {"type": "boolean", "description": "input only — whether this input must be non-None for the node to run"},
        },
        "required": ["name"],
    },
}


TOOL_SCHEMAS: dict[str, dict] = {
    "view_graph": {
        "type": "function",
        "function": {
            "name": "view_graph",
            "description": (
                "Return a structural snapshot of the workflow — node ids, names, descriptions, "
                "ports, model. Useful to confirm state mid-turn after "
                "a sequence of mutations, or to plan before editing. Does not include node code; "
                "use `view_node_details` for that."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "view_node_details": {
        "type": "function",
        "function": {
            "name": "view_node_details",
            "description": (
                "Return the full record for one node — including its complete code and full config "
                "(model). Call this before "
                "editing a node so you can make targeted patches instead of guessing at its "
                "current state."
            ),
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    },
    "get_mcp_tool_schema": {
        "type": "function",
        "function": {
            "name": "get_mcp_tool_schema",
            "description": (
                "Return the complete, untruncated JSON input schema for one MCP tool. "
                "The per-turn MCP tool list caps each tool's schema to save space, so a "
                "large tool shows up truncated. Call this BEFORE writing a direct "
                "`ctx.tools.<server>.<tool>(...)` call whose arguments are nested or use a "
                "union (anyOf/oneOf) — e.g. Notion's create_pages — so you can construct "
                "valid args instead of guessing. Pass the same `server`/`tool` names shown "
                "in the MCP tool list (the dotted `ctx.tools.<server>.<tool>` form). "
                "Returns {server, tool, call, llm_name, description, input_schema}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "server namespace, e.g. 'notion'"},
                    "tool": {"type": "string", "description": "tool name within the server, e.g. 'create_pages'"},
                },
                "required": ["server", "tool"],
            },
        },
    },
    "add_node": {
        "type": "function",
        "function": {
            "name": "add_node",
            "description": (
                "Create a new node with its structure — name, description, and ports. "
                "The node is created with a stub `def run(inputs, ctx): return {}` body; "
                "follow up with `configure_node` to write its actual Python code. "
                "Returns {node_id}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case node name"},
                    "description": {"type": "string", "description": "one-line italic description for the canvas"},
                    "inputs": _PORT_SCHEMA,
                    "outputs": _PORT_SCHEMA,
                },
                "required": ["name"],
            },
        },
    },
    "remove_node": {
        "type": "function",
        "function": {
            "name": "remove_node",
            "description": "Delete a node. Edges touching it are removed automatically.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    },
    "rename_node": {
        "type": "function",
        "function": {
            "name": "rename_node",
            "description": "Rename a node.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "new_name": {"type": "string"},
                },
                "required": ["node_id", "new_name"],
            },
        },
    },
    "rename_project": {
        "type": "function",
        "function": {
            "name": "rename_project",
            "description": "Rename the current project (workflow).",
            "parameters": {
                "type": "object",
                "properties": {
                    "new_name": {"type": "string"},
                },
                "required": ["new_name"],
            },
        },
    },
    "configure_node": {
        "type": "function",
        "function": {
            "name": "configure_node",
            "description": (
                "Patch a node's description, code, and/or ports. Omitted fields are left "
                "unchanged — omit `inputs`/`outputs` to preserve existing ports."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "description": {"type": "string"},
                    "code": {"type": "string"},
                    "inputs": {
                        **_PORT_SCHEMA,
                        "description": "Optional. Omit to leave existing input ports unchanged.",
                    },
                    "outputs": {
                        **_PORT_SCHEMA,
                        "description": "Optional. Omit to leave existing output ports unchanged.",
                    },
                },
                "required": ["node_id"],
            },
        },
    },
    "add_edge": {
        "type": "function",
        "function": {
            "name": "add_edge",
            "description": (
                "Connect one node's named output to another node's named input. Both ports must "
                "already exist. Returns {edge_id}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_node_id": {"type": "string"},
                    "from_output": {"type": "string"},
                    "to_node_id": {"type": "string"},
                    "to_input": {"type": "string"},
                },
                "required": ["from_node_id", "from_output", "to_node_id", "to_input"],
            },
        },
    },
    "remove_edge": {
        "type": "function",
        "function": {
            "name": "remove_edge",
            "description": "Delete an edge by id.",
            "parameters": {
                "type": "object",
                "properties": {"edge_id": {"type": "string"}},
                "required": ["edge_id"],
            },
        },
    },
    "set_input_node": {
        "type": "function",
        "function": {
            "name": "set_input_node",
            "description": "Designate this node as the entry point of the workflow.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    },
    "set_output_node": {
        "type": "function",
        "function": {
            "name": "set_output_node",
            "description": "Designate this node as the workflow's terminal node.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    },
    "clean_canvas": {
        "type": "function",
        "function": {
            "name": "clean_canvas",
            "description": (
                "Wipe the workflow's graph — delete every node + edge and clear the "
                "input/output node pointers. The session, runs, and run history are "
                "preserved. A session can host a *sequence of distinct workflows* on "
                "the way to one answer — e.g. a scoping workflow, then a solve workflow "
                "built on what it found, then a verify workflow. `clean_canvas` is the "
                "transition between stages. For incremental refinements within the "
                "current stage, patch in place instead."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "run_agent": {
        "type": "function",
        "function": {
            "name": "run_agent",
            "description": (
                "Execute one bounded agent inline without changing the workspace. Supply "
                "self-contained Python code implementing def run(inputs, ctx). The tool "
                "passes an empty inputs dict; the code contains the one-off task and logic. "
                "No input/output port declarations are needed, and the returned dict becomes "
                "`outputs`. The tool "
                "runs that code in the isolated node runtime, BLOCKS until it finishes, "
                "then returns its outputs directly. It creates no canvas node or visible "
                "run-history entry, so never call list_runs or view_run for its result. "
                "Its inline chat card exposes the same code, live run trace, logs, direct "
                "tool calls, and continuable LLM-call views as a normal node. Its code has the "
                "same ctx.agent and ctx.tools capabilities as any node. Use graph tools "
                "plus run_workflow when the work belongs in a "
                "reusable, inspectable, multi-stage, or parallel workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short snake_case label for the inline agent.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line description of what this node does.",
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Complete Python node code implementing def run(inputs, ctx). "
                            "Write it exactly as you would for configure_node, including "
                            "ctx.agent and/or direct ctx.tools calls as needed."
                        ),
                    },
                },
                "required": [
                    "name",
                    "description",
                    "code",
                ],
            },
        },
    },
    "run_workflow": {
        "type": "function",
        "function": {
            "name": "run_workflow",
            "description": (
                "Start a long background run with explicit inputs and return immediately: "
                "{run_id, status:\"running\"}. Do not poll; inspect later with `list_runs` "
                "/ `view_run` only after needed runs appear complete, the user asks, or a "
                "later step needs completed output. Call only when you can supply every "
                "required input from the conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inputs": {
                        "type": "object",
                        "description": (
                            "Map of input port name → value for the workflow's input node. "
                            "Keys must match the input node's declared input names. Every "
                            "port marked `required` must be present."
                        ),
                    },
                },
                "required": ["inputs"],
            },
        },
    },
    "cancel_run": {
        "type": "function",
        "function": {
            "name": "cancel_run",
            "description": (
                "Cancel a running or pending workflow run by id. Use when the user asks "
                "to stop a run, or when you started a run that is no longer useful. "
                "Returns {run_id, cancelled} or {run_id, cancelled:false, reason}. Only "
                "cancels runs in the current workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": (
                            "The run id to cancel. Use the id returned by `run_workflow` "
                            "or found through `list_runs`."
                        ),
                    },
                },
                "required": ["run_id"],
            },
        },
    },
    "list_runs": {
        "type": "function",
        "function": {
            "name": "list_runs",
            "description": (
                "List historic runs for this workflow, most recent first. Returns a lean "
                "shape per run — {run_id, status, kind, started_at, ended_at, total_cost, "
                "error} — enough to identify a run; drill into contents with `view_run`. "
                "`kind` is `\"user\"` (the user hit Run / the REST API) or `\"orchestrator\"` "
                "(you started it via `run_workflow`); pass `kind` to return only one source. "
                "Use when the user references a past run without giving you its id "
                "(\"the last failure\", \"yesterday's run\"), or when you need to find a "
                "specific run to inspect. Don't list runs "
                "preemptively — inspect run history only when the task calls for it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_RUN_LIST_LIMIT,
                        "description": (
                            f"How many runs to return (default {_DEFAULT_RUN_LIST_LIMIT}, "
                            f"max {_MAX_RUN_LIST_LIMIT}). Most recent first. Older runs are "
                            "truncated; raise this if a run you need is missing."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["user", "orchestrator"],
                        "description": (
                            "Optional source filter. Use `orchestrator` to list only runs "
                            "started by you with `run_workflow`, or `user` for manual/API runs."
                        ),
                    },
                },
            },
        },
    },
    "view_run": {
        "type": "function",
        "function": {
            "name": "view_run",
            "description": (
                "Read one node's record from a completed or failed run. Use only when you "
                "need run contents: summarizing completed orchestrator-started runs, failure "
                "diagnosis, research findings, stage handoff, or a user-requested inspection. "
                "Don't use it to poll or summarize a successful manually started run. Every "
                "call must name `node_id`, `fields`, and `ports` when reading inputs/outputs. "
                "The workflow result is on the output node."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "node_id": {
                        "type": "string",
                        "description": (
                            "The node to inspect — ids come from `view_graph` or "
                            "`node_errors`. Use the output node's id to read the "
                            "workflow's result."
                        ),
                    },
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["inputs", "outputs", "logs"],
                        },
                        "minItems": 1,
                        "uniqueItems": True,
                        "description": (
                            "Which heavy fields to return — the smallest subset of "
                            "[\"inputs\", \"outputs\", \"logs\"] that answers your "
                            "question (e.g. `[\"logs\"]` to read only the `ctx.log(...)` "
                            "lines without paying for big outputs)."
                        ),
                    },
                    "ports": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                        "description": (
                            "Which port names to return from the `inputs`/`outputs` "
                            "dicts — REQUIRED whenever `fields` includes `inputs` or "
                            "`outputs`; omit for a logs-only read. Port names are the "
                            "node's declared inputs/outputs (see `view_graph`). Name "
                            "just the port(s) you need."
                        ),
                    },
                },
                "required": ["run_id", "node_id", "fields"],
            },
        },
    },
}


def llm_tool_specs() -> list[dict]:
    """Tool schemas in OpenRouter `tools` array shape."""
    return list(TOOL_SCHEMAS.values())


def _active_run_id(db: DbSession, wid: str) -> str | None:
    """Return the id of any in-flight run for this workflow, or None.

    Cross-references the DB with the live `events` registry so a stale
    `running` row from a crashed runner doesn't permanently block mutations.
    """
    from app.runner import events as ev_mod

    rows = (
        db.query(models.Run.id)
        .filter(
            models.Run.workflow_id == wid,
            models.Run.status.in_(["pending", "running"]),
        )
        .all()
    )
    for (rid,) in rows:
        st = ev_mod.get(rid)
        # If we have no in-memory state for it (process restart) OR the in-memory
        # state isn't finished yet, treat the run as active.
        if st is None or not st.finished:
            return rid
    return None


def execute(
    db: DbSession,
    wid: str,
    name: str,
    args: dict,
    *,
    cancel_event: Any = None,
    event_callback: Any = None,
) -> dict:
    """Dispatch a tool call. Returns either the tool's result dict or
    {"error": "..."} on failure — never raises, so the agent loop can keep
    going and let the LLM self-correct."""
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    # Refuse graph *mutations* while a workflow run is executing — the runner
    # snapshots the graph at start, so mid-run mutations won't take effect for
    # the current run anyway, and they can leave the orchestrator's mental
    # model out of sync with the run that the user is watching. Read-only
    # inspection tools are always allowed.
    if name not in NON_GRAPH_MUTATING_TOOLS:
        active = _active_run_id(db, wid)
        if active is not None:
            return {
                "error": (
                    f"cannot mutate the graph: run {active} is in progress. "
                    "wait for it to finish (or cancel it) before changing nodes/edges."
                )
            }
    try:
        call_args = dict(args or {})
        if name == "run_agent":
            call_args["_cancel_event"] = cancel_event
            call_args["_event_callback"] = event_callback
        return fn(db, wid, **call_args)
    except TypeError as e:
        # bad arguments — surface to LLM as an error result
        db.rollback()
        return {"error": f"bad arguments to {name}: {e}"}
    except ValueError as e:
        db.rollback()
        return {"error": str(e)}
    except Exception as e:  # pragma: no cover — defensive
        db.rollback()
        return {"error": f"{type(e).__name__}: {e}"}
