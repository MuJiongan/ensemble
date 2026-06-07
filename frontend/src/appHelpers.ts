import type { ChatBlock, ChatMessage } from './components/ChatPanel';
import type { ChatHistoryMessage, Run, RunEvent, WorkflowDetail } from './types';

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
    if (m.role === 'user') return { role: 'user', text: m.text ?? '' };
    return {
      role: 'assistant',
      content: (m.content ?? []).map((b): ChatBlock => {
        if (b.t === 'thinking') return { t: 'thinking', text: b.text };
        if (b.t === 'p') return { t: 'p', text: b.text };
        return {
          t: 'tool',
          tool: b.tool,
          args: b.args,
          status: b.status === 'pending' ? 'pending' : b.status,
          result: b.result,
        };
      }),
      ...(m.cost && m.cost > 0 ? { cost: m.cost } : {}),
    };
  });
}

/** Pick a project name from the user's first message. */
export function deriveWorkflowName(text: string): string {
  const trimmed = text.trim().replace(/\s+/g, ' ');
  if (!trimmed) return DEFAULT_WORKFLOW_NAME;
  return trimmed.length > 80 ? trimmed.slice(0, 77) + '…' : trimmed;
}
