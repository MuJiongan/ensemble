import type { AssistantMessage, ChatBlock, ChatMessage, ChatToolCall } from './components/ChatPanel';
import type { LiveLLMCall } from './components/NodeTraceCard';
import type { ChatHistoryMessage, Run, RunEvent, WorkflowDetail, WorkflowExport } from './types';

export interface ModelStat {
  model: string;
  calls: number;
  promptTokens: number;
  completionTokens: number;
  cost: number;
}

interface LLMCallRecord {
  model?: string;
  usage?: Record<string, unknown>;
  cost?: number;
}

function tokensFromUsage(usage: Record<string, unknown> | undefined): {
  prompt: number;
  completion: number;
} {
  if (!usage) return { prompt: 0, completion: 0 };
  return {
    prompt: Number(usage.prompt_tokens) || 0,
    completion: Number(usage.completion_tokens) || 0,
  };
}

export function modelStatsFromCalls(calls: LLMCallRecord[]): ModelStat[] | null {
  const stats = modelStatsFromLlmCalls(calls);
  return stats.length > 0 ? stats : null;
}

function modelStatsFromLlmCalls(calls: LLMCallRecord[]): ModelStat[] {
  const byModel = new Map<string, ModelStat>();
  for (const c of calls) {
    const model = c.model?.trim() || 'unknown';
    const cur = byModel.get(model) ?? {
      model,
      calls: 0,
      promptTokens: 0,
      completionTokens: 0,
      cost: 0,
    };
    cur.calls += 1;
    const t = tokensFromUsage(c.usage);
    cur.promptTokens += t.prompt;
    cur.completionTokens += t.completion;
    cur.cost += Number(c.cost) || 0;
    byModel.set(model, cur);
  }
  return [...byModel.values()].sort((a, b) => b.cost - a.cost || b.calls - a.calls);
}

/** Aggregate per-model call counts, token usage, and cost from persisted
 * node_runs. Returns null when the run recorded no LLM calls. */
export function modelStatsFromRun(run: Run): ModelStat[] | null {
  const calls: LLMCallRecord[] = [];
  for (const nr of run.node_runs) {
    for (const c of (nr.llm_calls as LLMCallRecord[]) ?? []) {
      calls.push(c);
    }
  }
  return modelStatsFromCalls(calls);
}

/** Same aggregation from streamed llm_call_finished events — used while a
 * run is still in flight (node_runs aren't written until completion). */
export function modelStatsFromEvents(events: RunEvent[]): ModelStat[] | null {
  const calls = events
    .filter((e): e is Extract<RunEvent, { type: 'llm_call_finished' }> =>
      e.type === 'llm_call_finished',
    )
    .map((e) => ({ model: e.model, usage: e.usage, cost: e.cost }));
  return modelStatsFromCalls(calls);
}

/** Prefer persisted node_runs; fall back to live events when the run hasn't
 * materialised its node_runs yet. */
export function modelStatsForRun(
  run: Run,
  liveEvents?: RunEvent[],
): ModelStat[] | null {
  return modelStatsFromRun(run) ?? (liveEvents ? modelStatsFromEvents(liveEvents) : null);
}

export function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Total tool calls across all node_runs (direct + LLM-mediated). */
export function toolCallCountFromRun(run: Run): number {
  let n = 0;
  for (const nr of run.node_runs) {
    n += ((nr.tool_calls as unknown[]) ?? []).length;
  }
  return n;
}

/** Total from streamed tool_call_finished events (in-flight runs). */
export function toolCallCountFromEvents(events: RunEvent[]): number {
  return events.filter((e) => e.type === 'tool_call_finished').length;
}

export function toolCallCountForRun(
  run: Run,
  liveEvents?: RunEvent[],
): number | null {
  const fromRun = toolCallCountFromRun(run);
  if (fromRun > 0) return fromRun;
  if (liveEvents) {
    const fromLive = toolCallCountFromEvents(liveEvents);
    if (fromLive > 0) return fromLive;
  }
  return null;
}

export const DEFAULT_WORKFLOW_NAME = 'untitled project';

export function formatWorkflowExport(data: WorkflowExport): string {
  return JSON.stringify(data, null, 2);
}

export function parseWorkflowExport(text: string): WorkflowExport {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("that doesn't look like valid JSON — paste the full exported project text.");
  }
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('that JSON is not a project export (expected an object with a "nodes" list).');
  }
  const obj = parsed as Partial<WorkflowExport>;
  if (!Array.isArray(obj.nodes)) {
    throw new Error('that JSON is not a project export (missing a "nodes" list).');
  }
  if (obj.edges != null && !Array.isArray(obj.edges)) {
    throw new Error('that project export is malformed ("edges" must be a list).');
  }
  const badNode = obj.nodes.findIndex(
    (n) => !n || typeof (n as { id?: unknown }).id !== 'string',
  );
  if (badNode !== -1) {
    throw new Error(`node ${badNode + 1} is missing a string "id" — this export looks incomplete.`);
  }
  return obj as WorkflowExport;
}

/** Build a portable export bundle from a run's frozen snapshot. */
export function snapshotToExport(run: Run, projectName?: string): WorkflowExport | null {
  const s = run.workflow_snapshot;
  if (!s) return null;
  const base = (projectName || 'project').trim() || 'project';
  return {
    exported_at: new Date().toISOString(),
    name: `${base} (run ${run.id.slice(0, 8)})`,
    input_node_id: s.input_node_id,
    output_node_id: s.output_node_id,
    nodes: s.nodes.map((n) => ({
      id: n.id,
      name: n.name,
      description: n.description ?? '',
      code: n.code,
      inputs: n.inputs,
      outputs: n.outputs,
      config: n.config,
      position: n.position ?? { x: 0, y: 0 },
    })),
    edges: s.edges.map((e) => ({
      id: e.id,
      from_node_id: e.from_node_id,
      from_output: e.from_output,
      to_node_id: e.to_node_id,
      to_input: e.to_input,
    })),
  };
}

// Tools that mutate the graph — when we see one of these complete, refresh
// the canvas detail.
export const GRAPH_MUTATING_TOOLS = new Set([
  'add_node',
  'remove_node',
  'rename_node',
  'configure_node',
  'add_edge',
  'remove_edge',
  'set_input_node',
  'set_output_node',
  'clean_canvas',
]);

// Tools that mutate workflow metadata (not the graph) — refresh the project
// list so the header / switcher pick up the new name.
export const WORKFLOW_METADATA_TOOLS = new Set(['rename_project']);

/** Coerce a Run's `workflow_snapshot` into a WorkflowDetail so the Canvas can
 * render it the same way it renders the live graph. The snapshot omits the
 * Workflow's user-visible `name` field; we synthesise one from the run id. */
export function snapshotToDetail(run: Run): WorkflowDetail | null {
  const s = run.workflow_snapshot;
  if (!s) return null;
  return {
    id: s.id,
    name: `run ${run.id.slice(0, 8)}`,
    input_node_id: s.input_node_id,
    output_node_id: s.output_node_id,
    nodes: s.nodes.map((n) => ({
      id: n.id,
      workflow_id: s.id,
      name: n.name,
      description: n.description ?? '',
      code: n.code,
      inputs: n.inputs,
      outputs: n.outputs,
      config: n.config,
      position: n.position ?? { x: 0, y: 0 },
    })),
    edges: s.edges.map((e) => ({
      id: e.id,
      workflow_id: s.id,
      from_node_id: e.from_node_id,
      from_output: e.from_output,
      to_node_id: e.to_node_id,
      to_input: e.to_input,
    })),
  };
}

/**
 * One-line, human-readable summary of a run — a preview of the input values,
 * shown instead of the opaque run id wherever a run needs a title.
 */
export function summariseRun(run: Run): { text: string; kind: 'value' | 'id' } {
  const populated = Object.entries(run.inputs ?? {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== '',
  );

  if (populated.length === 0) {
    return { text: run.id.slice(0, 8), kind: 'id' };
  }

  const previewValue = (v: unknown): string => {
    if (typeof v === 'string') return v;
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  };

  const truncate = (s: string, n: number) =>
    s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;

  const TOTAL_BUDGET = 60;

  if (populated.length === 1) {
    const [, v] = populated[0];
    return {
      text: truncate(previewValue(v).replace(/\s+/g, ' ').trim(), TOTAL_BUDGET),
      kind: 'value',
    };
  }

  const perValueBudget = Math.max(8, Math.floor(TOTAL_BUDGET / populated.length));
  const joined = populated
    .map(([, v]) => truncate(previewValue(v).replace(/\s+/g, ' ').trim(), perValueBudget))
    .join(' · ');
  return { text: truncate(joined, TOTAL_BUDGET), kind: 'value' };
}

export function historyToChatMessages(history: ChatHistoryMessage[]): ChatMessage[] {
  return history.map((m) => {
    if (m.role === 'user') {
      return {
        role: 'user',
        text: m.text ?? '',
        ...(m.images?.length ? { images: m.images } : {}),
        ...(m.files?.length ? { files: m.files } : {}),
      };
    }
    return {
      role: 'assistant',
      content: (m.content ?? []).map((b): ChatBlock => {
        if (b.t === 'thinking') return { t: 'thinking', text: b.text };
        if (b.t === 'p') return { t: 'p', text: b.text };
        return {
          t: 'tool',
          tool: b.tool,
          args: b.args,
          argsFull: b.args_full,
          status: b.status === 'pending' ? 'pending' : b.status,
          result: b.result,
        };
      }),
      ...(m.cost && m.cost > 0 ? { cost: m.cost } : {}),
    };
  });
}

// --- continue-chat (agent continuation) transcript -----------------------------

interface OAIToolCall {
  id?: string;
  function?: { name?: string; arguments?: string };
}

interface OAIMessage {
  role?: string;
  content?: unknown;
  tool_calls?: OAIToolCall[];
  tool_call_id?: string;
}

/** Flatten an OpenAI message `content` (a string, or an array of content parts)
 * to plain text — the parts a chat bubble renders. */
function oaiContentToText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part;
        if (part && typeof part === 'object' && 'text' in part) {
          return String((part as { text?: unknown }).text ?? '');
        }
        return '';
      })
      .join('');
  }
  return '';
}

/** Tool messages carry the JSON-encoded recorded result; surface the parsed
 * value so the tool card can render it richly, falling back to raw text. */
function parseToolResult(content: unknown): unknown {
  const text = oaiContentToText(content);
  if (!text) return text;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Render a continuation's persisted OpenAI-shape transcript as chat bubbles — the same
 * `ChatMessage[]` model the orchestrator chat uses, so one renderer drives both.
 * Assistant tool calls become pending tool blocks; the following `tool`-role
 * results are matched back by `tool_call_id` and resolve their card.
 */
export function messagesToChat(messages: Array<Record<string, unknown>>): ChatMessage[] {
  const out: ChatMessage[] = [];
  const toolBlockById = new Map<string, ChatToolCall>();
  // One `agent` call is an agent loop — assistant → tool → assistant → … → final
  // — i.e. several assistant messages. Render them as ONE assistant bubble per
  // turn (collapsing consecutive assistant/tool messages), splitting only on a
  // user message. Matches the single-bubble live view.
  let current: AssistantMessage | null = null;

  for (const raw of messages ?? []) {
    const m = raw as OAIMessage;
    if (m.role === 'user') {
      out.push({ role: 'user', text: oaiContentToText(m.content) });
      current = null; // the next assistant message starts a fresh turn
      // tool_call_ids are only unique within a turn; scope resolution to this
      // turn so a later result can't resolve a same-id block from an earlier one.
      toolBlockById.clear();
    } else if (m.role === 'assistant') {
      if (!current) {
        current = { role: 'assistant', content: [] };
        out.push(current);
      }
      const text = oaiContentToText(m.content);
      if (text) current.content.push({ t: 'p', text });
      for (const tc of m.tool_calls ?? []) {
        const argsStr = tc.function?.arguments ?? '';
        let argsFull: Record<string, unknown> | null = null;
        try {
          const parsed = argsStr ? JSON.parse(argsStr) : null;
          if (parsed && typeof parsed === 'object') argsFull = parsed as Record<string, unknown>;
        } catch {
          // leave argsFull null — the card falls back to the args string.
        }
        const block: ChatToolCall = {
          t: 'tool',
          tool: tc.function?.name ?? '',
          args: argsStr,
          argsFull,
          // A finished transcript's tool calls already ran — default to a
          // resolved state so a dangling call (no matching result message,
          // e.g. a transcript cut mid-tool) doesn't spin forever. The matching
          // tool-role result below attaches the payload.
          status: 'ok',
        };
        current.content.push(block);
        if (tc.id) toolBlockById.set(tc.id, block);
      }
    } else if (m.role === 'tool') {
      const block = m.tool_call_id ? toolBlockById.get(m.tool_call_id) : undefined;
      if (block) {
        const parsed = parseToolResult(m.content);
        // A tool failure is recorded as {error: "<non-empty message>"} (backend
        // llm.py/ctx.py/mcp.py). Key off a non-empty STRING error value, not mere
        // key presence: a benign success body with error:null/"" or a non-string
        // field named `error` is not a failure (the live path only flags a real
        // ev.error). Avoids the ✕-on-reopen vs ✓-live divergence.
        const errVal =
          parsed && typeof parsed === 'object' && !Array.isArray(parsed)
            ? (parsed as Record<string, unknown>).error
            : undefined;
        block.status = typeof errVal === 'string' && errVal ? 'err' : 'ok';
        block.result = parsed;
      }
    }
    // system messages (rare for agent — there's no implicit system prompt)
    // are skipped: they aren't part of the visible conversation.
  }
  // Drop any assistant turn that ended up empty (defensive).
  return out.filter((m) => m.role !== 'assistant' || m.content.length > 0);
}

/**
 * Render an in-flight call's streamed rounds as a single streaming assistant
 * bubble, so a live agent shows in the chat pane the same way a persisted
 * continuation does. (The initial prompt isn't in the event stream, so the live
 * view is the assistant side only — the full transcript loads once the run
 * persists and the conversation becomes continuable.)
 */
export function liveCallToChat(call: LiveLLMCall): ChatMessage[] {
  const blocks: ChatBlock[] = [];
  for (const r of call.rounds) {
    if (r.reasoning) blocks.push({ t: 'thinking', text: r.reasoning });
    if (r.content) blocks.push({ t: 'p', text: r.content });
    for (const tc of r.toolCalls) {
      const status: ChatToolCall['status'] =
        tc.error ? 'err' : tc.status === 'ok' ? 'ok' : tc.status === 'err' ? 'err' : 'pending';
      blocks.push({
        t: 'tool',
        tool: tc.tool,
        args: tc.args_str || (tc.args ? JSON.stringify(tc.args) : ''),
        argsFull: tc.args ?? null,
        status,
        result: tc.error ?? tc.result,
      });
    }
  }
  return [{ role: 'assistant', content: blocks, streaming: call.status === 'streaming' }];
}

/** Pick a project name from the user's first message. */
export function deriveWorkflowName(text: string): string {
  const trimmed = text.trim().replace(/\s+/g, ' ');
  if (!trimmed) return DEFAULT_WORKFLOW_NAME;
  return trimmed.length > 80 ? trimmed.slice(0, 77) + '…' : trimmed;
}
