from __future__ import annotations
from typing import Any
from datetime import datetime
from pydantic import BaseModel, Field


class IOPort(BaseModel):
    name: str
    type_hint: str = "any"
    required: bool = True


class Position(BaseModel):
    x: float = 0
    y: float = 0


DEFAULT_CODE = "def run(inputs, ctx):\n    return {}\n"


class NodeIn(BaseModel):
    name: str = "node"
    description: str = ""
    code: str = DEFAULT_CODE
    inputs: list[IOPort] = Field(default_factory=list)
    outputs: list[IOPort] = Field(default_factory=list)
    position: Position = Field(default_factory=Position)


class NodeOut(NodeIn):
    id: str
    workflow_id: str


class NodePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    code: str | None = None
    inputs: list[IOPort] | None = None
    outputs: list[IOPort] | None = None
    position: Position | None = None


class EdgeIn(BaseModel):
    from_node_id: str
    from_output: str
    to_node_id: str
    to_input: str


class EdgeOut(EdgeIn):
    id: str
    workflow_id: str


class WorkflowIn(BaseModel):
    name: str = "Untitled"


class WorkflowOut(BaseModel):
    id: str
    name: str
    input_node_id: str | None = None
    output_node_id: str | None = None


class WorkflowDetail(WorkflowOut):
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class WorkflowPatch(BaseModel):
    name: str | None = None
    input_node_id: str | None = None
    output_node_id: str | None = None


class WorkflowExportNode(BaseModel):
    """Portable node record — ``id`` is preserved for edge remapping on import."""

    id: str
    name: str = "node"
    description: str = ""
    code: str = DEFAULT_CODE
    inputs: list[IOPort] = Field(default_factory=list)
    outputs: list[IOPort] = Field(default_factory=list)


class WorkflowExportEdge(EdgeIn):
    """Portable edge record — ``id`` is ignored on import."""

    id: str | None = None


class WorkflowExport(BaseModel):
    """Portable project bundle written by export and accepted by import."""

    # Informational only — written by export, accepted but ignored on import.
    exported_at: str | None = None
    name: str = "untitled project"
    input_node_id: str | None = None
    output_node_id: str | None = None
    nodes: list[WorkflowExportNode] = Field(default_factory=list)
    edges: list[WorkflowExportEdge] = Field(default_factory=list)


class RunStartIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    kind: str = "user"


class NodeRunDetailOut(BaseModel):
    """Field-gated node-run detail.

    Lightweight metadata is always present. Heavy trace fields are included only
    when requested through ``fields=...`` on the node-run endpoint.
    """

    id: str
    node_id: str
    status: str
    error: str | None = None
    duration_ms: int
    cost: float
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    logs: list[Any] | None = None
    llm_calls: list[Any] | None = None
    tool_calls: list[Any] | None = None


class NodeRunSummaryOut(BaseModel):
    id: str
    node_id: str
    status: str
    error: str | None = None
    duration_ms: int
    cost: float
    log_count: int = 0
    llm_call_count: int = 0
    tool_call_count: int = 0


class RunModelStatOut(BaseModel):
    model: str
    calls: int
    promptTokens: int
    completionTokens: int
    cost: float


class RunOverviewOut(BaseModel):
    """Run detail without heavy per-node trace payloads.

    Carries enough to render the snapshot canvas and run-level panel. The
    snapshot is a code-free graph summary; run outputs and full snapshot/code
    load through focused endpoints. A node's inputs/outputs/logs/LLM/tool trace
    is fetched lazily through field-gated node-run requests.
    """

    id: str
    workflow_id: str
    kind: str
    status: str
    inputs: dict[str, Any]
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_cost: float
    workflow_snapshot: dict[str, Any] | None = None
    node_runs: list[NodeRunSummaryOut]
    model_stats: list[RunModelStatOut] = Field(default_factory=list)
    tool_call_count: int = 0


class RunOutputsOut(BaseModel):
    outputs: dict[str, Any] = Field(default_factory=dict)


class RunSnapshotOut(BaseModel):
    workflow_snapshot: dict[str, Any] | None = None


class SnapshotNodeCodeOut(BaseModel):
    node_id: str
    code: str


class RunSummaryOut(BaseModel):
    """Lean run-list row: no graph snapshot and no node-run payloads."""

    id: str
    workflow_id: str
    kind: str
    status: str
    inputs: dict[str, Any]
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_cost: float


class RunCardNodeOut(BaseModel):
    id: str
    name: str
    status: str


class RunCardOut(BaseModel):
    """Small payload for a chat run card; excludes snapshot code and traces."""

    id: str
    workflow_id: str
    status: str
    error: str | None = None
    total_cost: float
    node_count: int
    nodes: list[RunCardNodeOut] = Field(default_factory=list)
    input_node_name: str | None = None
    output_node_name: str | None = None


# --- orchestrator session schemas -----------------------------------------


class SessionOut(BaseModel):
    id: str
    workflow_id: str


class ChatToolCall(BaseModel):
    """A tool call card as rendered in the chat panel."""
    tool: str
    args: str  # human-readable summary, e.g. 'name="transcribe"'
    status: str  # "ok" | "err"
    result: Any | None = None


class ChatBlockP(BaseModel):
    t: str = "p"
    text: str


class ChatBlockTool(BaseModel):
    t: str = "tool"
    tool: str
    args: str
    # Full parsed argument dict, so the panel can render the raw input
    # parameters when a tool card is expanded — `args` is only a lossy summary.
    args_full: dict[str, Any] | None = None
    status: str
    result: Any | None = None


class ChatMessageOut(BaseModel):
    """One rendered chat bubble. Either user (with text) or assistant (mixed
    content)."""
    role: str  # "user" | "assistant"
    text: str | None = None
    content: list[dict[str, Any]] | None = None  # for assistant: list of ChatBlockP / ChatBlockTool
    images: list[str] | None = None  # for user: attached images as data URLs
    files: list[dict[str, Any]] | None = None  # for user: non-image tiles [{name, kind}]
    # Provider-reported USD cost for the assistant round that produced this
    # bubble. Currently only OpenRouter reports cost; omitted otherwise.
    cost: float | None = None


class ActiveRunOut(BaseModel):
    id: str
    workflow_id: str
    status: str


class SessionMessagesOut(BaseModel):
    messages: list[ChatMessageOut]
    # True when the backend still has an in-flight orchestrator turn for this
    # session. Used by the frontend to restore "working" status after a reload.
    active_turn: bool = False
    # Reconnectable id for the in-flight turn's replayable event stream.
    active_turn_id: str | None = None
    # Active orchestrator-started workflow runs for this session's workflow.
    # Lets a reloaded pending run_workflow card recover the run id that was
    # originally delivered as a transient SSE event.
    active_runs: list[ActiveRunOut] = Field(default_factory=list)


class AttachmentIn(BaseModel):
    """One chat attachment (image, PDF, or text file) as a base64 data URL."""
    data_url: str
    filename: str | None = None


class UserMessageIn(BaseModel):
    text: str
    attachments: list[AttachmentIn] = []
    auto: bool = False


# --- continue-chat (agent continuation) schemas ------------------------


class CallChatOut(BaseModel):
    """A call's continuation with its full transcript (OpenAI-shape messages)."""
    id: str
    workflow_id: str
    run_id: str
    node_run_id: str
    call_id: str
    label: str
    model: str
    # Provider + reasoning variant the source call ran with — the frontend sends
    # these as the node selection so the continuation keeps using the same model.
    provider_id: str = ""
    variant: str = ""
    tools: list[Any] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class CallChatTurnIn(BaseModel):
    text: str
    # The continuation's currently-selected model (from the chat model
    # switcher). Empty → keep the stored model. Provider + variant ride in via
    # the X-Node-* headers; this carries the model name to match them.
    model: str = ""


class CallChatTurnOut(BaseModel):
    turn_id: str
