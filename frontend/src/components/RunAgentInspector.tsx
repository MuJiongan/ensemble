import { useEffect, useMemo, useState } from 'react';
import Editor from '@monaco-editor/react';
import type { NodeRun, NodeRunStatus, WorkflowDetail } from '../types';
import type { RunAgentInspection, RunAgentResult } from './RunAgentCard';
import {
  aggregateEvents,
  type LiveLLMCall,
  NodeLlmCallsView,
  NodeTraceCard,
  nodeRunToTrace,
} from './NodeTraceCard';
import { CloseButton } from './CloseButton';

type Tab = 'code' | 'run' | 'calls';

interface Props {
  inspection: RunAgentInspection;
  onClose: () => void;
  onContinue?: (runId: string, nodeRunId: string, callId: string) => void;
  onViewLive?: (callId: string, label: string) => void;
}

function nodeStatus(value: unknown): NodeRunStatus {
  return value === 'pending' || value === 'running' || value === 'success' ||
    value === 'error' || value === 'skipped'
    ? value
    : 'error';
}

export function RunAgentInspector({
  inspection,
  onClose,
  onContinue,
  onViewLive,
}: Props) {
  const { name, description, code, status, runEvents } = inspection;
  const runResult = (inspection.result ?? {}) as RunAgentResult;
  const rawNodeRun = runResult._display?.node_run as Partial<NodeRun> | undefined;
  const hiddenRunId = runResult._display?.run_id;
  const running = status === 'pending';
  const failed = status === 'err' || !!runResult.error;
  const [tab, setTab] = useState<Tab>(running ? 'run' : 'code');

  const nodeRun = useMemo<NodeRun | null>(() => {
    if (!rawNodeRun) return null;
    return {
      id: typeof rawNodeRun.id === 'string' ? rawNodeRun.id : 'inline_agent',
      node_id: typeof rawNodeRun.node_id === 'string' ? rawNodeRun.node_id : 'inline_agent',
      status: nodeStatus(rawNodeRun.status),
      inputs: rawNodeRun.inputs ?? {},
      outputs: rawNodeRun.outputs ?? runResult.outputs ?? {},
      logs: rawNodeRun.logs ?? [],
      llm_calls: rawNodeRun.llm_calls ?? [],
      tool_calls: rawNodeRun.tool_calls ?? [],
      error: rawNodeRun.error ?? runResult.error ?? null,
      duration_ms: Number(rawNodeRun.duration_ms ?? runResult.duration_ms ?? 0),
      cost: Number(rawNodeRun.cost ?? runResult.total_cost ?? 0),
    };
  }, [rawNodeRun, runResult.outputs, runResult.error, runResult.duration_ms, runResult.total_cost]);

  const workflow = useMemo<WorkflowDetail>(() => ({
    id: 'inline_agent',
    name: 'inline agent',
    input_node_id: null,
    output_node_id: null,
    nodes: [{
      id: 'inline_agent',
      workflow_id: 'inline_agent',
      name,
      description,
      code,
      inputs: [],
      outputs: [],
      position: { x: 0, y: 0 },
    }],
    edges: [],
  }), [name, description, code]);

  const completedTrace = useMemo(
    () => nodeRun ? nodeRunToTrace(nodeRun) : null,
    [nodeRun],
  );
  const liveTrace = useMemo(
    () => aggregateEvents(runEvents).find((item) => item.node_id === 'inline_agent') ?? null,
    [runEvents],
  );
  const settledTrace = useMemo(() => {
    if (!completedTrace) return liveTrace;
    if (!liveTrace) return completedTrace;
    const calls: LiveLLMCall[] = completedTrace.llmCalls.length > 0
      ? completedTrace.llmCalls
      : liveTrace.llmCalls.map((call) => ({
          ...call,
          status: failed ? 'error' : 'done',
          errorMsg: call.errorMsg ?? (failed ? runResult.error ?? 'agent stopped' : undefined),
        }));
    const directCalls = completedTrace.directToolCalls.length > 0
      ? completedTrace.directToolCalls
      : liveTrace.directToolCalls;
    return {
      ...completedTrace,
      logs: completedTrace.logs.length > 0 ? completedTrace.logs : liveTrace.logs,
      llmCalls: calls,
      llmCallById: new Map(calls.map((call) => [call.call_id, call])),
      directToolCalls: directCalls,
      directToolCallById: new Map(directCalls.map((call) => [call.call_id, call])),
      compactions: liveTrace.compactions,
    };
  }, [completedTrace, liveTrace, failed, runResult.error]);
  const trace = running ? (liveTrace ?? completedTrace) : settledTrace;
  const hasCalls = (trace?.llmCalls.length ?? 0) > 0;
  const tabs: Tab[] = ['code', 'run', ...(hasCalls ? (['calls'] as const) : [])];

  useEffect(() => {
    setTab((current) => {
      if (!hasCalls && current === 'calls') return running ? 'run' : 'code';
      if (!running) return current;
      if (hasCalls && (current === 'code' || current === 'run')) return 'calls';
      if (!hasCalls && current === 'code') return 'run';
      return current;
    });
  }, [running, hasCalls]);

  const openCall = (call: LiveLLMCall, index: number) => {
    const callName = call.label?.trim() || `call ${index + 1}`;
    if (running && onViewLive) {
      onViewLive(call.call_id, `${name} · ${callName}`);
      return;
    }
    if (hiddenRunId && nodeRun && onContinue) {
      onContinue(hiddenRunId, nodeRun.id, call.call_id);
    }
  };

  const statusLabel = running ? 'running' : failed ? 'failed' : 'done';
  const statusColor = running
    ? 'var(--ink-4)'
    : failed
      ? 'var(--state-err)'
      : 'var(--state-ok)';

  return (
    <div className="run-agent-inspector">
      <div className="run-agent-inspector__header">
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0 }}>
            <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>agent</span>
            <span className="mono" style={{ color: 'var(--accent-ink)', fontSize: 13 }}>
              {name}
            </span>
          </div>
          {description && (
            <div className="serif run-agent-inspector__description">{description}</div>
          )}
        </div>
        <span style={{ flex: 1 }} />
        <span className="smallcaps" style={{ color: statusColor, fontSize: 9 }}>
          {running && <span className="caret" style={{ marginRight: 4 }} />}
          {statusLabel}
        </span>
        <CloseButton onClick={onClose} title="close agent inspector" />
      </div>

      <div className="run-agent-inspector__tabs">
        {tabs.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className="smallcaps"
            style={{
              padding: '10px 12px',
              marginRight: 4,
              background: 'transparent',
              border: 'none',
              borderBottom: `1.5px solid ${tab === key ? 'var(--ink)' : 'transparent'}`,
              color: tab === key ? 'var(--ink)' : 'var(--ink-4)',
              cursor: 'pointer',
              fontSize: 9,
            }}
          >
            {key === 'calls' ? 'llm calls' : key}
          </button>
        ))}
      </div>

      <div className="run-agent-inspector__body scroll">
        {tab === 'code' && (
          code ? (
            <Editor
              height="100%"
              theme="vs-dark"
              language="python"
              value={code}
              options={{
                minimap: { enabled: false },
                fontSize: 12,
                fontFamily: "'Fragment Mono', ui-monospace, 'SF Mono', Menlo, monospace",
                scrollBeyondLastLine: false,
                lineNumbers: 'off',
                readOnly: true,
              }}
            />
          ) : (
            <div className="serif run-agent-inspector__empty">code is unavailable.</div>
          )
        )}

        {tab === 'run' && (
          <div style={{ padding: 16 }}>
            {trace ? (
              <NodeTraceCard workflow={workflow} trace={trace} />
            ) : running ? (
              <div className="serif run-agent-inspector__empty">starting the inline agent…</div>
            ) : (
              <pre className="mono" style={{ color: failed ? 'var(--state-err)' : 'var(--ink-3)', whiteSpace: 'pre-wrap' }}>
                {runResult.error || 'no execution trace returned.'}
              </pre>
            )}
          </div>
        )}

        {tab === 'calls' && trace && (
          <div style={{ padding: 16 }}>
            <NodeLlmCallsView
              trace={trace}
              live={running}
              onOpen={
                (running && onViewLive) || (!running && hiddenRunId && nodeRun && onContinue)
                  ? openCall
                  : undefined
              }
            />
          </div>
        )}
      </div>
    </div>
  );
}
