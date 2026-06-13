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

export interface NodeConfig {
  model: string;
}

export interface WFNode {
  id: string;
  workflow_id: string;
  name: string;
  description: string;
  code: string;
  inputs: IOPort[];
  outputs: IOPort[];
  config: NodeConfig;
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

export type RunStatus = 'pending' | 'running' | 'success' | 'error' | 'cancelled';
export type NodeRunStatus = 'pending' | 'running' | 'success' | 'error' | 'skipped';

export interface NodeRun {
  id: string;
  node_id: string;
  status: NodeRunStatus;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  logs: unknown[];
  llm_calls: unknown[];
  tool_calls: unknown[];
  error: string | null;
  duration_ms: number;
  cost: number;
}

export interface RunWorkflowSnapshotNode {
  id: string;
  name: string;
  description?: string;
  code: string;
  inputs: IOPort[];
  outputs: IOPort[];
  config: NodeConfig;
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

export interface Run {
  id: string;
  workflow_id: string;
  kind: string;
  status: RunStatus;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  error: string | null;
  total_cost: number;
  workflow_snapshot: RunWorkflowSnapshot | null;
  node_runs: NodeRun[];
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
  /** Default model + variant for ctx.call_llm inside nodes. */
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
  // Emitted by a node's ctx.call_llm loop when it summarized older history to
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
  finalOutputs: Record<string, unknown> | null;
  error: string | null;
  totalCost: number;
  // True when this run executes against a frozen snapshot that may diverge
  // from the live graph (rerun-from-snapshot). The live canvas suppresses
  // its node-state overlay in that case — node ids in the snapshot can
  // miss live nodes (and vice versa), so the dots would be misleading.
  // Snapshot view is the right place to watch progress for these runs;
  // the rerun handler stays in snapshot view while it executes.
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
  status: 'pending' | 'ok' | 'err';
  result?: unknown;
}

/** Extended-thinking trace from the model. Renders as a collapsible block. */
export interface ChatBlockThinking {
  t: 'thinking';
  text: string;
}

export type ChatBlock = ChatBlockP | ChatBlockTool | ChatBlockThinking;

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
}

export type OrchestratorEvent =
  | { kind: 'user_message'; id: string; text: string }
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
  // progress while the orchestrator awaits the result.
  | { kind: 'run_started'; run_id: string; workflow_id: string }
  // Emitted once per turn when the agent loop summarized older history to
  // stay within the model's context window. Purely informational — the chat
  // shows a divider so the user knows context was compacted mid-turn.
  | { kind: 'context_compacted' }
  | { kind: 'error'; message: string }
  | { kind: 'done' };
