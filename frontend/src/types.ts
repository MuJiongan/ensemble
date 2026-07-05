export interface IOPort {
  name: string;
  type_hint: string;
  required: boolean;
}

/** A file resolved by the backend file-viewer endpoint (GET /api/files). */
export interface FsFile {
  path: string;
  name: string;
  kind: 'text' | 'markdown' | 'html' | 'image' | 'pdf' | 'video' | 'binary' | 'directory';
  mime?: string | null;
  size?: number;
  /** text / markdown / html */
  content?: string;
  truncated?: boolean;
  total_lines?: number | null;
  language?: string | null;
  /** image / pdf, base64 data: URL */
  data_url?: string;
  note?: string;
}

export interface WFNode {
  id: string;
  workflow_id: string;
  name: string;
  description: string;
  code: string;
  inputs: IOPort[];
  outputs: IOPort[];
  position: { x: number; y: number };
}

export interface WFEdge {
  id: string;
  workflow_id: string;
  from_node_id: string;
  from_output: string;
  to_node_id: string;
  to_input: string;
}

export interface Workflow {
  id: string;
  name: string;
  input_node_id: string | null;
  output_node_id: string | null;
}

export interface WorkflowDetail extends Workflow {
  nodes: WFNode[];
  edges: WFEdge[];
}

/** Portable project bundle for import/export. */
export interface WorkflowExport {
  exported_at?: string | null;
  name: string;
  input_node_id: string | null;
  output_node_id: string | null;
  nodes: Array<Omit<WFNode, 'workflow_id' | 'position'>>;
  edges: Array<Omit<WFEdge, 'workflow_id'>>;
}

export type RunStatus = 'pending' | 'running' | 'success' | 'error' | 'cancelled';
export type NodeRunStatus = 'pending' | 'running' | 'success' | 'error' | 'skipped';

export interface NodeRun {
  id: string;
  node_id: string;
  status: NodeRunStatus;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  logs?: unknown[];
  llm_calls?: unknown[];
  tool_calls?: unknown[];
  error: string | null;
  duration_ms: number;
  cost: number;
}

export type NodeRunField = 'inputs' | 'outputs' | 'logs' | 'llm_calls' | 'tool_calls';

export interface NodeRunSummary {
  id: string;
  node_id: string;
  status: NodeRunStatus;
  error: string | null;
  duration_ms: number;
  cost: number;
  log_count: number;
  llm_call_count: number;
  tool_call_count: number;
}

export interface RunWorkflowSnapshotNode {
  id: string;
  name: string;
  description?: string;
  /** Present only after loading a full snapshot or snapshot node code. */
  code?: string;
  has_code?: boolean;
  inputs: IOPort[];
  outputs: IOPort[];
  position?: { x: number; y: number };
}

export interface RunWorkflowSnapshotEdge {
  id: string;
  from_node_id: string;
  from_output: string;
  to_node_id: string;
  to_input: string;
}

export interface RunWorkflowSnapshot {
  id: string;
  input_node_id: string | null;
  output_node_id: string | null;
  nodes: RunWorkflowSnapshotNode[];
  edges: RunWorkflowSnapshotEdge[];
}

export interface RunSummary {
  id: string;
  workflow_id: string;
  kind: string;
  status: RunStatus;
  inputs: Record<string, unknown>;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  total_cost: number;
}

export interface RunModelStat {
  model: string;
  calls: number;
  promptTokens: number;
  completionTokens: number;
  cost: number;
}

export interface Run extends RunSummary {
  /** Code-free snapshot summary. Full snapshot/code loads on demand. */
  workflow_snapshot: RunWorkflowSnapshot | null;
  /** Lightweight per-node status rows. Full traces load via GET /api/runs/{run_id}/node-runs/{id}. */
  node_runs: NodeRunSummary[];
  model_stats: RunModelStat[];
  tool_call_count: number;
}

export interface RunOutputs {
  outputs: Record<string, unknown>;
}

export interface RunSnapshot {
  workflow_snapshot: RunWorkflowSnapshot | null;
}

export interface SnapshotNodeCode {
  node_id: string;
  code: string;
}

export interface RunCardNode {
  id: string;
  name: string;
  status: NodeRunStatus;
}

export interface RunCard {
  id: string;
  workflow_id: string;
  status: RunStatus;
  error: string | null;
  total_cost: number;
  node_count: number;
  nodes: RunCardNode[];
  input_node_name: string | null;
  output_node_name: string | null;
}

/** How a provider was connected. ``api`` providers paste a bearer token (and
 * carry the catalog base URL); ``oauth`` providers (codex, xai) sign in and the
 * backend stores tokens server-side. */
export interface ProviderConnection {
  method: 'api' | 'oauth';
  apiKey?: string;
  baseURL?: string;
}

/** A chosen model + reasoning variant for a target (orchestrator or node).
 * ``variant`` is the reasoning tier (low/medium/high/max/...) or null for off. */
export interface ModelSelection {
  providerID: string;
  modelID: string;
  variant: string | null;
}

export interface Settings {
  /** Connected providers keyed by catalog provider id (openai, anthropic,
   * openrouter, codex, ...). Holds the api key + base url, or just the oauth
   * marker. */
  connections: Record<string, ProviderConnection>;
  parallel_api_key: string;
  /** Model + variant used by the orchestrator chat. */
  orchestrator: ModelSelection | null;
  /** Default model + variant for ctx.agent inside nodes. */
  node: ModelSelection | null;
  /**
   * MCP (Model Context Protocol) server config as a raw JSON string, in
   * opencode's shape — a map of server name → `{type: "local", command: [...]}`
   * or `{type: "remote", url: "..."}`. Sent to the backend as the
   * `X-Mcp-Servers` header; the runner connects to these servers and exposes
   * their tools to node code. Empty string means "no MCP servers".
   */
  mcp_servers: string;
  /**
   * Custom instructions injected into the orchestrator system prompt each
   * turn. The orchestrator decides which parts to propagate into node code.
   */
  custom_instructions: string;
}

// --- streaming run events --------------------------------------------------

export type ToolVia = 'direct' | 'llm';

export type LLMChunkKind = 'content' | 'reasoning' | 'tool_args';

export type RunEvent =
  // Emitted once at run start (before run_started) with the connection state
  // of every configured MCP server, so the UI can prompt for re-login when a
  // server the run may depend on needs authentication.
  | {
      type: 'mcp_status';
      servers: Record<string, { status: string; tool_count?: number; error?: string }>;
    }
  | { type: 'run_started'; node_count: number; order: string[] }
  | { type: 'node_started'; node_id: string; inputs: Record<string, unknown> }
  | { type: 'log'; node_id: string; msg: string }
  | {
      type: 'llm_call_started';
      node_id: string;
      call_id: string;
      model: string;
      tools: string[];
      label?: string;
    }
  | {
      type: 'llm_round_started';
      node_id: string;
      call_id: string;
      round: number;
    }
  | {
      type: 'llm_call_chunk';
      node_id: string;
      call_id: string;
      kind: LLMChunkKind;
      round: number;
      delta: string;
      tc_index?: number;
      tool?: string;
    }
  | {
      type: 'llm_call_finished';
      node_id: string;
      call_id: string;
      model: string;
      content: string;
      usage: Record<string, unknown>;
      cost: number;
      error?: string;
    }
  | {
      type: 'tool_call_started';
      node_id: string;
      tool: string;
      args: Record<string, unknown>;
      via: ToolVia;
      call_id?: string;
      tc_index?: number;
      round?: number;
    }
  | {
      type: 'tool_call_finished';
      node_id: string;
      tool: string;
      args: Record<string, unknown>;
      result?: unknown;
      error?: string;
      via: ToolVia;
      call_id?: string;
      tc_index?: number;
      round?: number;
    }
  | {
      type: 'node_finished';
      node_id: string;
      status: NodeRunStatus;
      inputs: Record<string, unknown>;
      outputs: Record<string, unknown>;
      logs: string[];
      llm_calls: unknown[];
      tool_calls: unknown[];
      error: string | null;
      duration_ms: number;
      cost: number;
    }
  | {
      type: 'run_finished';
      status: RunStatus;
      outputs: Record<string, unknown>;
      error: string | null;
      total_cost: number;
    }
  // Synthetic, not produced by the runner: sent when the run's row/state is
  // deleted out from under a subscriber (or a subscriber attaches to a run
  // that no longer exists). Terminal — the server closes the socket after.
  | { type: 'run_deleted'; run_id: string }
  // Emitted by a node's ctx.agent loop when it summarized older history to
  // stay within the model's context window. `summarized` is the number of
  // messages folded into the anchor. Rendered as a marker in the node trace.
  | {
      type: 'context_compacted';
      node_id: string;
      call_id?: string;
      summarized: number;
    };

export interface CurrentRun {
  id: string;
  workflow_id: string;
  status: RunStatus;
  startedAt: number;
  events: RunEvent[];
  nodeStates: Record<string, NodeRunStatus>;
  error: string | null;
  totalCost: number;
  // True when this run executes against a frozen snapshot that may diverge
  // from the live graph (rerun-from-snapshot). Snapshot view can use this
  // run's live node states safely because it is tied to one explicit run.
  executesOnSnapshot: boolean;
}

// --- orchestrator chat session --------------------------------------------

export interface OrchestratorSession {
  id: string;
  workflow_id: string;
}

export interface ChatBlockP {
  t: 'p';
  text: string;
}

export interface ChatBlockTool {
  t: 'tool';
  tool: string;
  args: string;
  /** Full parsed argument dict — `args` is only a lossy summary. Used to show
   * raw input parameters when a tool card is expanded. */
  args_full?: Record<string, unknown> | null;
  status: 'pending' | 'ok' | 'err';
  result?: unknown;
}

/** Extended-thinking trace from the model. Renders as a collapsible block. */
export interface ChatBlockThinking {
  t: 'thinking';
  text: string;
}

export interface ChatBlockNotice {
  t: 'notice';
  text: string;
  kind?: 'compaction' | 'run' | 'info';
}

export type ChatBlock = ChatBlockP | ChatBlockTool | ChatBlockThinking | ChatBlockNotice;

export interface ChatHistoryUser {
  role: 'user';
  text: string;
  content?: null;
  /** Attached images as base64 data URLs; omitted when none. */
  images?: string[] | null;
  /** Attached non-image file tiles; omitted when none. */
  files?: { name: string; kind: string }[] | null;
}

export interface ChatHistoryAssistant {
  role: 'assistant';
  text?: null;
  content: ChatBlock[];
  /** Provider-reported USD cost for the round; omitted when unknown / 0. */
  cost?: number | null;
}

export type ChatHistoryMessage = ChatHistoryUser | ChatHistoryAssistant;

export interface ChatHistory {
  messages: ChatHistoryMessage[];
  /** True while the backend still has an in-flight orchestrator turn for this session. */
  active_turn?: boolean;
  /** Running/pending orchestrator-started runs for this workflow. */
  active_runs?: Pick<Run, 'id' | 'workflow_id' | 'status'>[];
}

export type OrchestratorEvent =
  | { kind: 'user_message'; id: string; text: string; auto?: boolean }
  // assistant_text fires once per LLM round with the full text — kept for
  // backwards compat with non-streaming clients (currently unused by App).
  | { kind: 'assistant_text'; text: string }
  // assistant_text_chunk fires for each token delta during a round.
  | { kind: 'assistant_text_chunk'; text: string }
  // assistant_thinking_chunk fires for each reasoning-token delta during a round.
  | { kind: 'assistant_thinking_chunk'; text: string }
  // assistant_cost fires once per LLM round (after persistence) with the
  // provider-reported USD cost for that round. The chat bubble accumulates
  // it across rounds in the same turn.
  | { kind: 'assistant_cost'; cost: number }
  | {
      kind: 'tool_call_start';
      tool: string;
      args: string;
      args_full?: Record<string, unknown>;
    }
  | {
      kind: 'tool_call_end';
      tool: string;
      args: string;
      status: 'ok' | 'err';
      result?: unknown;
    }
  // Emitted by the agent loop when the orchestrator's `run_workflow` tool
  // kicks off a run. The frontend attaches the run panel to the run's WS
  // (same code path the manual Run button uses), so the user sees live
  // progress while the run continues in the background.
  | { kind: 'run_started'; run_id: string; workflow_id: string }
  // Emitted once per turn when the agent loop summarized older history to
  // stay within the model's context window. Purely informational — the chat
  // shows a divider so the user knows context was compacted mid-turn.
  | { kind: 'context_compacted' }
  | { kind: 'error'; message: string }
  | { kind: 'done' };

// --- continue-chat (agent continuations) -------------------------------

/** A call's continuation with its full OpenAI-shape transcript. */
export interface CallChat {
  id: string;
  workflow_id: string;
  run_id: string;
  node_run_id: string;
  call_id: string;
  label: string;
  model: string;
  provider_id: string;
  variant: string;
  tools: string[];
  messages: Array<Record<string, unknown>>;
}

/** Events streamed for one continue-chat turn. Mirrors the run event contract
 * a node's ctx.agent emits — minus node_id (a continuation belongs to no node). The
 * terminal `run_finished` carries the grown conversation so the client can sync
 * against the persisted transcript. */
export type CallChatTurnEvent =
  | { type: 'run_started' }
  | {
      type: 'mcp_status';
      servers: Record<string, { status: string; tool_count?: number; error?: string }>;
    }
  | { type: 'llm_call_started'; call_id: string; model: string; tools: string[]; label?: string }
  | { type: 'llm_round_started'; call_id: string; round: number }
  | {
      type: 'llm_call_chunk';
      call_id: string;
      kind: LLMChunkKind;
      round: number;
      delta: string;
      tc_index?: number;
      tool?: string;
    }
  | {
      type: 'llm_call_finished';
      call_id: string;
      model: string;
      content: string;
      usage: Record<string, unknown>;
      cost: number;
      error?: string;
    }
  | {
      type: 'tool_call_started';
      tool: string;
      args: Record<string, unknown>;
      via: ToolVia;
      call_id?: string;
      tc_index?: number;
      round?: number;
    }
  | {
      type: 'tool_call_finished';
      tool: string;
      args: Record<string, unknown>;
      result?: unknown;
      error?: string;
      via: ToolVia;
      call_id?: string;
      tc_index?: number;
      round?: number;
    }
  | { type: 'context_compacted'; call_id?: string; summarized: number }
  | {
      type: 'run_finished';
      status: RunStatus;
      messages: Array<Record<string, unknown>>;
      usage: Record<string, unknown>;
      cost: number;
      error: string | null;
    }
  | { type: 'run_deleted'; run_id: string }
  | { type: 'error'; error: string };
