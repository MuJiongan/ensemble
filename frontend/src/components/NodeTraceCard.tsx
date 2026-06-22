import type {
  WorkflowDetail, NodeRun, NodeRunStatus, RunEvent,
} from '../types';
import { modelStatsFromCalls, formatTokenCount } from '../appHelpers';
import { JsonView } from './JsonView';
import { ExecutionStats } from './ExecutionStats';
import { LogsView } from './LogsView';
import { PortRow, ValueRow } from './ValueViewer';
import { NodeIOBlock } from './NodeIOBlock';

// --- types ----------------------------------------------------------------
//
// One entry per ctx.call_llm invocation, broken down by agent-loop round so
// reasoning / content / tool-arg streams render in chronological order rather
// than as one concatenated blob.

export type LiveCallStatus = 'streaming' | 'done' | 'error';
export type ToolCallStatus = 'streaming' | 'pending' | 'ok' | 'err';

export interface NestedToolCall {
  tc_index: number;
  round: number;
  tool: string;
  args_str: string;
  args?: Record<string, unknown>;
  status: ToolCallStatus;
  result?: unknown;
  error?: string;
}

export interface CallRound {
  round: number;
  reasoning: string;
  content: string;
  toolCalls: NestedToolCall[];
}

export interface LiveLLMCall {
  call_id: string;
  /** Short human name from ctx.call_llm(label=...); falls back to "call N". */
  label?: string;
  model: string;
  tools: string[];
  rounds: CallRound[];
  roundsByIdx: Map<number, CallRound>;
  status: LiveCallStatus;
  cost?: number;
  usage?: Record<string, unknown>;
  errorMsg?: string;
  // True when this finished call has a saved conversation that can be resumed
  // as a chat. Only set on historical (persisted) traces — live calls aren't
  // continuable until they finish and persist.
  hasChat?: boolean;
}

export type DirectCallStatus = 'pending' | 'ok' | 'err';

export interface DirectToolCall {
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
  status: DirectCallStatus;
  result?: unknown;
  error?: string;
}

export interface NodeTrace {
  node_id: string;
  // The persisted NodeRun id, present only for historical/snapshot traces
  // (not live aggregation). Needed to address a call's continuation.
  node_run_id?: string;
  status: NodeRunStatus;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  logs: string[];
  llmCalls: LiveLLMCall[];
  llmCallById: Map<string, LiveLLMCall>;
  directToolCalls: DirectToolCall[];
  directToolCallById: Map<string, DirectToolCall>;
  // One entry per time the node's call_llm loop summarized older history to
  // fit the context window; `summarized` is the message count folded in.
  compactions: { summarized: number }[];
  error?: string | null;
  duration_ms?: number;
  cost?: number;
}

const STATE_CLASS: Record<NodeRunStatus, string> = {
  pending: 'idle',
  running: 'running',
  success: 'success',
  error: 'error',
  skipped: 'skipped',
};

function ensureRound(call: LiveLLMCall, round: number | undefined): CallRound {
  const r = round ?? 0;
  let cr = call.roundsByIdx.get(r);
  if (!cr) {
    cr = { round: r, reasoning: '', content: '', toolCalls: [] };
    call.roundsByIdx.set(r, cr);
    const i = call.rounds.findIndex((x) => x.round > r);
    if (i === -1) call.rounds.push(cr);
    else call.rounds.splice(i, 0, cr);
  }
  return cr;
}

function findNestedTool(round: CallRound, tc_index: number | undefined): NestedToolCall | undefined {
  const idx = tc_index ?? 0;
  return round.toolCalls.find((t) => t.tc_index === idx);
}

// --- aggregator -----------------------------------------------------------
//
// Folds a chronological RunEvent[] into per-node NodeTrace records. Pure /
// memoisable — call repeatedly as new events arrive on the WS.

export function aggregateEvents(events: RunEvent[]): NodeTrace[] {
  const byId = new Map<string, NodeTrace>();
  const order: string[] = [];

  const ensureNode = (id: string): NodeTrace => {
    let t = byId.get(id);
    if (!t) {
      t = {
        node_id: id,
        status: 'pending',
        logs: [],
        llmCalls: [],
        llmCallById: new Map(),
        directToolCalls: [],
        directToolCallById: new Map(),
        compactions: [],
      };
      byId.set(id, t);
      order.push(id);
    }
    return t;
  };

  const ensureCall = (
    t: NodeTrace,
    call_id: string,
    model = '',
    tools: string[] = [],
    label?: string,
  ): LiveLLMCall => {
    let c = t.llmCallById.get(call_id);
    if (!c) {
      c = {
        call_id,
        label,
        model,
        tools,
        rounds: [],
        roundsByIdx: new Map(),
        status: 'streaming',
      };
      t.llmCallById.set(call_id, c);
      t.llmCalls.push(c);
    } else {
      if (model && !c.model) c.model = model;
      if (tools.length && !c.tools.length) c.tools = tools;
      if (label && !c.label) c.label = label;
    }
    return c;
  };

  for (const ev of events) {
    if (ev.type === 'node_started') {
      const t = ensureNode(ev.node_id);
      t.status = 'running';
      t.inputs = ev.inputs;
    } else if (ev.type === 'log') {
      ensureNode(ev.node_id).logs.push(ev.msg);
    } else if (ev.type === 'llm_call_started') {
      ensureCall(ensureNode(ev.node_id), ev.call_id, ev.model, ev.tools, ev.label);
    } else if (ev.type === 'llm_round_started') {
      ensureRound(ensureCall(ensureNode(ev.node_id), ev.call_id), ev.round);
    } else if (ev.type === 'llm_call_chunk') {
      const call = ensureCall(ensureNode(ev.node_id), ev.call_id);
      const r = ensureRound(call, ev.round);
      if (ev.kind === 'content') {
        r.content += ev.delta;
      } else if (ev.kind === 'reasoning') {
        r.reasoning += ev.delta;
      } else if (ev.kind === 'tool_args') {
        let tc = findNestedTool(r, ev.tc_index);
        if (!tc) {
          tc = {
            tc_index: ev.tc_index ?? 0,
            round: r.round,
            tool: ev.tool || '',
            args_str: '',
            status: 'streaming',
          };
          r.toolCalls.push(tc);
        }
        if (ev.tool && !tc.tool) tc.tool = ev.tool;
        tc.args_str += ev.delta;
      }
    } else if (ev.type === 'llm_call_finished') {
      const call = ensureCall(ensureNode(ev.node_id), ev.call_id, ev.model);
      call.status = ev.error ? 'error' : 'done';
      call.cost = ev.cost;
      call.usage = ev.usage;
      call.errorMsg = ev.error;
      // Authoritative final content — replaces the LAST round's content
      // (the round with no further tool calls is the one that emitted it).
      if (ev.content && call.rounds.length) {
        call.rounds[call.rounds.length - 1].content = ev.content;
      }
    } else if (ev.type === 'tool_call_started') {
      const t = ensureNode(ev.node_id);
      if (ev.via === 'llm' && ev.call_id) {
        const call = ensureCall(t, ev.call_id);
        const r = ensureRound(call, ev.round);
        let tc = findNestedTool(r, ev.tc_index);
        if (!tc) {
          tc = {
            tc_index: ev.tc_index ?? 0,
            round: r.round,
            tool: ev.tool,
            args_str: JSON.stringify(ev.args),
            status: 'pending',
          };
          r.toolCalls.push(tc);
        } else {
          tc.tool = ev.tool || tc.tool;
        }
        tc.args = ev.args;
        tc.status = 'pending';
      } else if (ev.via === 'direct' && ev.call_id) {
        let dtc = t.directToolCallById.get(ev.call_id);
        if (!dtc) {
          dtc = {
            call_id: ev.call_id,
            tool: ev.tool,
            args: ev.args,
            status: 'pending',
          };
          t.directToolCallById.set(ev.call_id, dtc);
          t.directToolCalls.push(dtc);
        } else {
          dtc.tool = ev.tool || dtc.tool;
          dtc.args = ev.args;
        }
      }
    } else if (ev.type === 'tool_call_finished') {
      const t = ensureNode(ev.node_id);
      if (ev.via === 'llm' && ev.call_id) {
        const call = ensureCall(t, ev.call_id);
        const r = ensureRound(call, ev.round);
        let tc = findNestedTool(r, ev.tc_index);
        if (!tc) {
          tc = {
            tc_index: ev.tc_index ?? 0,
            round: r.round,
            tool: ev.tool,
            args_str: JSON.stringify(ev.args),
            status: 'pending',
          };
          r.toolCalls.push(tc);
        }
        tc.tool = ev.tool || tc.tool;
        tc.args = ev.args;
        tc.result = ev.result;
        tc.error = ev.error;
        tc.status = ev.error ? 'err' : 'ok';
      } else {
        let dtc: DirectToolCall | undefined;
        if (ev.call_id) dtc = t.directToolCallById.get(ev.call_id);
        if (!dtc) {
          dtc = {
            call_id: ev.call_id ?? `direct-${t.directToolCalls.length + 1}`,
            tool: ev.tool,
            args: ev.args,
            status: 'pending',
          };
          t.directToolCallById.set(dtc.call_id, dtc);
          t.directToolCalls.push(dtc);
        }
        dtc.tool = ev.tool || dtc.tool;
        dtc.args = ev.args;
        dtc.result = ev.result;
        dtc.error = ev.error;
        dtc.status = ev.error ? 'err' : 'ok';
      }
    } else if (ev.type === 'context_compacted') {
      ensureNode(ev.node_id).compactions.push({ summarized: ev.summarized });
    } else if (ev.type === 'node_finished') {
      const t = ensureNode(ev.node_id);
      t.status = ev.status;
      t.inputs = ev.inputs;
      t.outputs = ev.outputs;
      t.error = ev.error;
      t.duration_ms = ev.duration_ms;
      t.cost = ev.cost;
      if (t.logs.length === 0 && ev.logs && ev.logs.length) {
        t.logs = ev.logs as string[];
      }
      // Mark any still-streaming live calls as done so the spinner stops.
      for (const c of t.llmCalls) {
        if (c.status === 'streaming') c.status = 'done';
        for (const r of c.rounds) {
          for (const tc of r.toolCalls) {
            if (tc.status === 'streaming' || tc.status === 'pending') {
              tc.status = tc.error ? 'err' : 'ok';
            }
          }
        }
      }
    }
  }
  return order.map((id) => byId.get(id)!).filter(Boolean);
}

// --- historical NodeRun → NodeTrace ---------------------------------------
//
// Lets the per-node renderer accept a frozen NodeRun row (snapshot view) the
// same way it accepts a live aggregation. Historical llm_calls don't carry
// per-round streaming detail, so each is folded into a single "round 0"
// LiveLLMCall preserving content / cost / tool_calls_made.

interface HistoricalLLMCall {
  call_id?: string;
  label?: string;
  model?: string;
  tools?: string[];
  // Lean run payload flag: this call has a saved conversation to continue.
  has_chat?: boolean;
  content?: string;
  tool_calls_made?: Array<{
    name?: string;
    arguments?: Record<string, unknown>;
    result?: unknown;
    error?: string;
  }>;
  usage?: Record<string, unknown>;
  cost?: number;
}

interface HistoricalToolCall {
  call_id?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  via?: 'llm' | 'direct';
}

export function nodeRunToTrace(nr: NodeRun): NodeTrace {
  const llmCalls: LiveLLMCall[] = [];
  const llmCallById = new Map<string, LiveLLMCall>();

  const rawCalls = (nr.llm_calls as unknown as HistoricalLLMCall[]) ?? [];
  rawCalls.forEach((rec, i) => {
    const id = rec.call_id ?? `hist-llm-${i}`;
    const toolCalls: NestedToolCall[] = (rec.tool_calls_made ?? []).map((tc, j) => ({
      tc_index: j,
      round: 0,
      tool: tc.name ?? '',
      args_str: JSON.stringify(tc.arguments ?? {}),
      args: tc.arguments,
      status: tc.error ? 'err' : 'ok',
      result: tc.result,
      error: tc.error,
    }));
    const round: CallRound = {
      round: 0,
      reasoning: '',
      content: rec.content ?? '',
      toolCalls,
    };
    const c: LiveLLMCall = {
      call_id: id,
      label: rec.label,
      model: rec.model ?? '',
      tools: rec.tools ?? [],
      rounds: [round],
      roundsByIdx: new Map([[0, round]]),
      status: 'done',
      cost: rec.cost,
      usage: rec.usage,
      hasChat: rec.has_chat ?? false,
    };
    llmCalls.push(c);
    llmCallById.set(id, c);
  });

  // Direct tool calls: NodeRun.tool_calls aggregates everything (LLM-mediated
  // and direct). The LLM-mediated ones are already represented inside
  // llm_calls.tool_calls_made above, so to avoid duplication we surface only
  // entries marked `via: "direct"` (or untagged ones from older rows that
  // didn't record `via` at all — those predate the dual-path tracking).
  const directToolCalls: DirectToolCall[] = [];
  const directToolCallById = new Map<string, DirectToolCall>();
  const rawTools = (nr.tool_calls as unknown as HistoricalToolCall[]) ?? [];
  rawTools.forEach((tc, i) => {
    if (tc.via === 'llm') return;
    const id = tc.call_id ?? `hist-tool-${i}`;
    const dtc: DirectToolCall = {
      call_id: id,
      tool: tc.name ?? '',
      args: tc.arguments ?? {},
      status: tc.error ? 'err' : 'ok',
      result: tc.result,
      error: tc.error,
    };
    directToolCalls.push(dtc);
    directToolCallById.set(id, dtc);
  });

  return {
    node_id: nr.node_id,
    node_run_id: nr.id,
    status: nr.status,
    inputs: nr.inputs,
    outputs: nr.outputs,
    logs: (nr.logs as string[]) ?? [],
    llmCalls,
    llmCallById,
    directToolCalls,
    directToolCallById,
    // Snapshot NodeRun rows don't persist compaction markers — live-only.
    compactions: [],
    error: nr.error,
    duration_ms: nr.duration_ms,
    cost: nr.cost,
  };
}

// --- per-node renderer ----------------------------------------------------
//
// Renders a single NodeTrace — header (status / duration / cost), error,
// inputs / outputs / logs, LLM-call cards, direct tool calls. Same component
// drives the live in-flight view (events streaming in via aggregateEvents)
// and the snapshot view (one-shot conversion via nodeRunToTrace).

interface NodeTraceCardProps {
  workflow: WorkflowDetail;
  trace: NodeTrace;
  /** Hook for the "send to orchestrator" button on errors. Omit to hide. */
  onSendErrorToOrchestrator?: (message: string) => void;
  runId?: string;
}

function buildErrorPrompt({
  runId, nodeName, error,
}: {
  runId?: string;
  nodeName: string;
  error: string;
}): string {
  const prefix = runId ? `Node "${nodeName}" failed during run ${runId.slice(0, 8)}:` : `Node "${nodeName}" failed:`;
  return `${prefix}\n\n${error}\n\nPlease diagnose and fix.`;
}

export function NodeTraceCard({
  workflow, trace, onSendErrorToOrchestrator, runId,
}: NodeTraceCardProps) {
  const nodeName =
    workflow.nodes.find((n) => n.id === trace.node_id)?.name ?? trace.node_id;
  // LLM calls live in their own "llm calls" tab now; this tab is structure:
  // status, I/O, logs, direct tool calls, and compaction notes.
  const hasSecondary =
    trace.logs.length > 0 ||
    trace.directToolCalls.length > 0 ||
    trace.compactions.length > 0;

  return (
    <div className="fade-in" style={{ padding: 0 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 8,
          padding: '4px 0 6px',
          borderBottom: '1px solid var(--rule-2)',
          marginBottom: 10,
        }}
      >
        <span className={`node-state-dot ${STATE_CLASS[trace.status]}`} />
        <span className="mono" style={{ fontSize: 11.5, color: 'var(--ink)' }}>
          {nodeName}
        </span>
        <span style={{ flex: 1 }} />
        <span
          className="smallcaps"
          style={{
            fontSize: 9,
            color:
              trace.status === 'success' ? 'var(--state-ok)' :
              trace.status === 'error' ? 'var(--state-err)' :
              trace.status === 'skipped' ? 'var(--ink-4)' : 'var(--ink-3)',
          }}
        >
          {trace.status}
          {typeof trace.duration_ms === 'number' ? ` · ${trace.duration_ms}ms` : ''}
          {typeof trace.cost === 'number' && trace.cost > 0
            ? ` · $${trace.cost.toFixed(4)}`
            : ''}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {trace.error && (
          <>
            <pre
              className="mono"
              style={{ fontSize: 11, color: 'var(--state-err)', whiteSpace: 'pre-wrap', margin: 0 }}
            >
              {trace.error}
            </pre>
            {onSendErrorToOrchestrator && (
              <button
                type="button"
                className="ed-btn ed-btn--mini"
                onClick={() =>
                  onSendErrorToOrchestrator(
                    buildErrorPrompt({ runId, nodeName, error: trace.error ?? '' }),
                  )
                }
                title="forward this error to the orchestrator"
              >
                send to orchestrator <span className="ed-btn__mark">→</span>
              </button>
            )}
          </>
        )}

        <NodeIOBlock
          workflow={workflow}
          nodeId={trace.node_id}
          nodeName={nodeName}
          inputs={trace.inputs}
          outputs={trace.outputs}
        />

        {hasSecondary && (
          <div className="trace-secondary">
            {trace.logs.length > 0 && (
              <section className="snapshot-io-section snapshot-io-section--detail">
                <div className="snapshot-io-section__head">
                  <span className="smallcaps snapshot-io-section__title">logs</span>
                  <span className="snapshot-io-section__count">
                    {trace.logs.length} {trace.logs.length === 1 ? 'entry' : 'entries'}
                  </span>
                </div>
                <LogsView logs={trace.logs} viewerTitle={`${nodeName} · logs`} />
              </section>
            )}

            {trace.directToolCalls.length > 0 && (
              <section className="snapshot-io-section snapshot-io-section--detail">
                <div className="snapshot-io-section__head">
                  <span className="smallcaps snapshot-io-section__title">tool calls</span>
                  <span className="snapshot-io-section__count">
                    {trace.directToolCalls.length} direct
                  </span>
                </div>
                <div className="trace-dense-list">
                  {trace.directToolCalls.map((dtc) => (
                    <ToolTraceCard
                      key={dtc.call_id}
                      tool={dtc.tool}
                      args={dtc.args}
                      status={dtc.status}
                      result={dtc.result}
                      error={dtc.error}
                    />
                  ))}
                </div>
              </section>
            )}

            {trace.compactions.length > 0 && (
              <div
                className="trace-compaction-note fade-in"
                title="Older turns were summarized to keep this node's call_llm loop within the model's context window."
              >
                <span aria-hidden>⤵</span>
                context compacted
                {trace.compactions.length > 1 ? ` ×${trace.compactions.length}` : ''}
                {` · ${trace.compactions.reduce((n, c) => n + c.summarized, 0)} msgs`}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// --- llm calls tab --------------------------------------------------------
//
// The node's LLM calls, lifted out of the trace tab into their own tab. Per
// model stats up top, then one card per ctx.call_llm. Clicking a card opens the
// call in the shared chat pane: a finished call as a continuation (composer
// enabled), an in-flight call streaming live (read-only until it lands). A rare
// finished-but-unsaved call (over the persist budget) keeps the inline rounds.

interface NodeLlmCallsViewProps {
  trace: NodeTrace;
  /** True when these are an in-flight run's calls (streamed from events). */
  live?: boolean;
  /** Open a call in the chat pane (continue if finished, watch if live). */
  onOpen?: (call: LiveLLMCall, callIndex: number) => void;
}

export function NodeLlmCallsView({ trace, live, onOpen }: NodeLlmCallsViewProps) {
  if (trace.llmCalls.length === 0) {
    return (
      <div
        className="serif"
        style={{ fontStyle: 'italic', color: 'var(--ink-3)', fontSize: 13, lineHeight: 1.55 }}
      >
        this node made no LLM calls in the selected run.
      </div>
    );
  }
  // A per-model rollup only earns its place when there's more than one call —
  // a single call's card already carries its own model + token + cost line.
  const modelStats =
    trace.llmCalls.length > 1
      ? modelStatsFromCalls(
          trace.llmCalls.map((c) => ({ model: c.model, usage: c.usage, cost: c.cost })),
        )
      : null;
  return (
    <div className="fade-in" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {modelStats && <ExecutionStats modelStats={modelStats} marginTop={0} />}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {trace.llmCalls.map((c, idx) => {
          // Live calls are always openable (watch them stream); finished calls
          // need a saved transcript to continue.
          const canOpen =
            !!onOpen && (live || (!!trace.node_run_id && c.status === 'done' && !!c.hasChat));
          return (
            <LLMCallCard
              key={c.call_id}
              call={c}
              index={idx}
              live={live}
              onContinue={canOpen ? () => onOpen!(c, idx) : undefined}
            />
          );
        })}
      </div>
    </div>
  );
}

// --- LLM call cards -------------------------------------------------------

function callDisplayName(call: LiveLLMCall, index: number): string {
  const label = call.label?.trim();
  return label || `call ${index + 1}`;
}

function llmStatusMeta(call: LiveLLMCall): {
  label: string;
  color: string;
  live: boolean;
} {
  if (call.status === 'error') {
    return { label: 'failed', color: 'var(--state-err)', live: false };
  }
  if (call.status === 'streaming') {
    return { label: 'streaming', color: 'var(--ink-4)', live: true };
  }
  return { label: 'done', color: 'var(--state-ok)', live: false };
}

function LLMCallCard({
  call, index, onContinue, live,
}: {
  call: LiveLLMCall;
  index: number;
  /** When set, the call renders as an elegant card whose click opens the call
   * in the chat pane (continue if finished, watch if live). */
  onContinue?: () => void;
  /** True when this is an in-flight run's call (streamed, read-only). */
  live?: boolean;
}) {
  const isStreaming = call.status === 'streaming';
  const status = llmStatusMeta(call);
  const callName = callDisplayName(call, index);
  const lastRoundIdx = call.rounds.length - 1;
  const showWaiting =
    isStreaming &&
    call.rounds.every((r) => !r.content && !r.reasoning && r.toolCalls.length === 0);
  const costHint =
    !isStreaming && typeof call.cost === 'number' && call.cost > 0
      ? `$${call.cost.toFixed(4)}`
      : undefined;
  // Count tool *calls actually made* (across rounds), not the tools available
  // to the call — "8 tool calls" is what the user means, not "2 tools".
  const toolCallCount = call.rounds.reduce((n, r) => n + r.toolCalls.length, 0);
  const toolHint = toolCallCount > 0
    ? `${toolCallCount} tool ${toolCallCount === 1 ? 'call' : 'calls'}`
    : undefined;
  const hintParts = [
    call.model || '…',
    toolHint,
    costHint,
  ].filter(Boolean);

  // A continuable call renders as an elegant clickable card (matching the I/O
  // cards): model + token/cost/tool stats + an output preview, with the whole
  // card opening the continuation in the chat pane. The transcript itself reads
  // there, not inline.
  if (onContinue) {
    const usage = (call.usage ?? {}) as Record<string, unknown>;
    const inTok = Number(usage.prompt_tokens) || 0;
    const outTok = Number(usage.completion_tokens) || 0;
    const metaParts = [
      inTok ? `${formatTokenCount(inTok)} in` : undefined,
      outTok ? `${formatTokenCount(outTok)} out` : undefined,
      costHint,
      toolHint,
    ].filter(Boolean) as string[];
    const preview = call.rounds[lastRoundIdx]?.content?.trim() || '';
    const streaming = live && call.status === 'streaming';

    return (
      <button
        type="button"
        className="port-card port-card--llm fade-in"
        onClick={onContinue}
        title={streaming ? 'watch this call stream' : 'continue this conversation'}
      >
        <div className="port-card__head">
          <span className="port-card__label">
            <span className="port-card__name">{callName}</span>
            <span className="port-card__hint">{call.model || '…'}</span>
          </span>
          <span className="port-card__meta">
            {streaming && (
              <span
                className="smallcaps"
                style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 9, color: 'var(--ink-4)' }}
              >
                <span className="caret" />
                streaming
              </span>
            )}
            <span className="port-card__open">{streaming ? 'watch →' : 'continue →'}</span>
          </span>
        </div>
        {metaParts.length > 0 && (
          <div
            className="mono"
            style={{ marginTop: 7, fontSize: 10, letterSpacing: '0.04em', color: 'var(--ink-4)' }}
          >
            {metaParts.join('  ·  ')}
          </div>
        )}
        {preview ? (
          <div className="port-card__value">{preview}</div>
        ) : streaming ? (
          <div
            className="serif"
            style={{ marginTop: 7, fontStyle: 'italic', fontSize: 12.5, color: 'var(--ink-4)' }}
          >
            running…
          </div>
        ) : null}
      </button>
    );
  }

  return (
    <details className="trace-fold trace-fold--nested fade-in" open={isStreaming}>
      <summary className="trace-fold__summary trace-fold__summary--nested">
        <span className="trace-fold__lead">
          <span className="trace-fold__chevron" aria-hidden>▸</span>
          <span className="trace-fold__label">
            <span className="mono" style={{ fontSize: 11 }}>{callName}</span>
            <span className="trace-fold__hint">{hintParts.join(' · ')}</span>
          </span>
        </span>
        <span className="port-card__status" style={{ color: status.color }}>
          {status.live && <span className="caret" style={{ marginRight: 4 }} />}
          {status.label}
        </span>
      </summary>
      <div className="trace-fold__body trace-fold__body--nested">
        {call.errorMsg && (
          <pre
            className="mono"
            style={{
              fontSize: 11,
              color: 'var(--state-err)',
              whiteSpace: 'pre-wrap',
              margin: '0 0 4px',
            }}
          >
            {call.errorMsg}
          </pre>
        )}
        {call.rounds.map((r, i) => (
          <CallRoundView
            key={r.round}
            round={r}
            showRoundBadge={call.rounds.length > 1}
            isLastRound={i === lastRoundIdx}
            callStreaming={isStreaming}
          />
        ))}
        {showWaiting && (
          <div
            className="serif"
            style={{ fontStyle: 'italic', fontSize: 11.5, color: 'var(--ink-4)' }}
          >
            waiting for first token…
          </div>
        )}
      </div>
    </details>
  );
}

function CallRoundView({
  round, showRoundBadge, isLastRound, callStreaming,
}: {
  round: CallRound;
  showRoundBadge: boolean;
  isLastRound: boolean;
  callStreaming: boolean;
}) {
  const live = callStreaming && isLastRound;
  const reasoningLive = live && !round.content && round.toolCalls.length === 0;
  const contentLive = live && !!round.content && round.toolCalls.length === 0;
  const roundTitle = `round ${round.round + 1}`;

  return (
    <div className="trace-round">
      {showRoundBadge && (
        <div className="trace-llm-card__round-label">round {round.round + 1}</div>
      )}
      {round.reasoning && (
        <PortRow
          name={reasoningLive ? 'thinking' : 'thought'}
          value={round.reasoning}
          viewerTitle={`${roundTitle} · thinking`}
          viewerSubtitle="llm"
          variant="row"
        />
      )}
      {round.content && (
        <PortRow
          name={contentLive ? 'streaming' : 'output'}
          value={round.content}
          viewerTitle={`${roundTitle} · output`}
          viewerSubtitle="llm"
          variant="row"
        />
      )}
      {round.toolCalls.map((tc) => (
        <ToolTraceCard
          key={`${tc.round}-${tc.tc_index}`}
          tool={tc.tool}
          args={tc.args}
          argsStr={tc.args_str}
          status={tc.status}
          result={tc.result}
          error={tc.error}
        />
      ))}
    </div>
  );
}

function toolStatusMeta(status: ToolCallStatus | DirectCallStatus): {
  label: string;
  color: string;
  live: boolean;
} {
  switch (status) {
    case 'ok':
      return { label: 'done', color: 'var(--state-ok)', live: false };
    case 'err':
      return { label: 'failed', color: 'var(--state-err)', live: false };
    case 'pending':
      return { label: 'running', color: 'var(--ink-4)', live: true };
    case 'streaming':
      return { label: 'streaming', color: 'var(--ink-4)', live: true };
  }
}

function ToolTraceCard({
  tool,
  args,
  argsStr,
  status,
  result,
  error,
}: {
  tool: string;
  args?: Record<string, unknown>;
  argsStr?: string;
  status: ToolCallStatus | DirectCallStatus;
  result?: unknown;
  error?: string;
}) {
  const meta = toolStatusMeta(status);
  const argsDisplay =
    args !== undefined ? JSON.stringify(args) : (argsStr ?? '');
  const preview =
    argsDisplay.length > 140 ? argsDisplay.slice(0, 140) + '…' : argsDisplay;

  return (
    <details className="trace-fold trace-fold--tool" open={meta.live}>
      <summary className="trace-fold__summary trace-fold__summary--tool">
        <span className="trace-fold__lead trace-fold__lead--wide">
          <span className="trace-fold__chevron" aria-hidden>▸</span>
          <span className="mono" style={{ fontSize: 11.5, color: 'var(--accent-ink)', flexShrink: 0 }}>
            {tool || '…'}
          </span>
          <span
            className="mono trace-fold__hint"
            style={{
              fontSize: 11,
              fontStyle: 'normal',
              fontFamily: 'var(--mono)',
            }}
          >
            {preview || '…'}
          </span>
        </span>
        <span className="port-card__status" style={{ color: meta.color }}>
          {meta.live && <span className="caret" style={{ marginRight: 4 }} />}
          {meta.label}
        </span>
      </summary>
      <div className="trace-fold__body trace-fold__body--tool" onClick={(e) => e.stopPropagation()}>
          <div>
            <div className="smallcaps" style={{ marginBottom: 4 }}>args</div>
            {args !== undefined ? (
              <JsonView value={args} />
            ) : (
              <pre
                className="mono"
                style={{ fontSize: 11, color: 'var(--ink-3)', whiteSpace: 'pre-wrap', margin: 0 }}
              >
                {argsStr || '…'}
              </pre>
            )}
          </div>
          {status === 'ok' && result !== undefined && (
            <ValueRow
              label="result"
              value={result}
              viewerTitle={`${tool || 'tool'} · result`}
            />
          )}
          {status === 'err' && error && (
            <div>
              <div
                className="smallcaps"
                style={{ marginBottom: 4, color: 'var(--state-err)' }}
              >
                error
              </div>
              <pre
                className="mono"
                style={{ fontSize: 11, color: 'var(--state-err)', whiteSpace: 'pre-wrap', margin: 0 }}
              >
                {error}
              </pre>
            </div>
          )}
      </div>
    </details>
  );
}
