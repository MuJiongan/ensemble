from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


class IOPort(BaseModel):
    name: str
    type_hint: str = "any"
    required: bool = True


class NodeConfig(BaseModel):
    model: str = ""


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
    config: NodeConfig = Field(default_factory=NodeConfig)
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
    config: NodeConfig | None = None
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


class WorkflowExportNode(NodeIn):
    """Portable node record — ``id`` is preserved for edge remapping on import."""

    id: str


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


class NodeRunOut(BaseModel):
    id: str
    node_id: str
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    logs: list[Any]
    llm_calls: list[Any]
    tool_calls: list[Any]
    error: str | None = None
    duration_ms: int
    cost: float


class RunOut(BaseModel):
    id: str
    workflow_id: str
    kind: str
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    error: str | None = None
    total_cost: float
    # Frozen graph the runner actually executed (nodes + code + edges + in/out
    # node ids). `None` for legacy rows created before snapshotting landed.
    workflow_snapshot: dict[str, Any] | None = None
    node_runs: list[NodeRunOut]


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


class SessionMessagesOut(BaseModel):
    messages: list[ChatMessageOut]


class AttachmentIn(BaseModel):
    """One chat attachment (image, PDF, or text file) as a base64 data URL."""
    data_url: str
    filename: str | None = None


class UserMessageIn(BaseModel):
    text: str
    attachments: list[AttachmentIn] = []


# --- continue-chat (agent continuation) schemas ------------------------


class CallChatOut(BaseModel):
    """A call's continuation with its full transcript (OpenAI-shape messages)."""
    id: str
    workflow_id: str
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
