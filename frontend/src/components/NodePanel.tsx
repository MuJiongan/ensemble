import { useEffect, useMemo, useState } from 'react';
import Editor from '@monaco-editor/react';
import type { WFNode, IOPort, WorkflowDetail, Run, CurrentRun } from '../types';
import { api } from '../api';
import {
  NodeTraceCard, aggregateEvents, nodeRunToTrace, type NodeTrace,
} from './NodeTraceCard';
import { CloseButton } from './CloseButton';

interface Props {
  node: WFNode;
  workflow: WorkflowDetail;
  onClose: () => void;
  onChange: () => void;
  /** Render as read-only — used when inspecting a snapshot. Hides the save
   * button and locks the editor. */
  readOnly?: boolean;
  /** When set, the trace tab is bound to this single historical run. The
   * snapshot view passes the run that produced the snapshot. */
  pinnedRun?: Run;
  /** Live in-flight run on this workflow. When present (and `pinnedRun` is
   * not), the trace tab streams events for `node.id` from the run's WS. */
  currentRun?: CurrentRun | null;
  /** Forward a node-level error from the trace tab to the orchestrator. */
  onSendErrorToOrchestrator?: (message: string) => void;
}

type Tab = 'code' | 'i/o' | 'trace';

const PANEL_STYLE: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  background: 'var(--paper)',
  display: 'flex',
  flexDirection: 'column',
  zIndex: 20,
};

/**
 * Node side panel.
 *
 * Topology (node name, description, port shape, input/output role) is owned
 * exclusively by the orchestrator and rendered read-only here — the user
 * asks the orchestrator via chat to make those changes. Per-node model and
 * tools likewise live with the orchestrator; user-level defaults are set
 * once in workflow settings.
 *
 * Content the user can still refine directly:
 *   - code (Monaco editor)
 */
export function NodePanel({
  node, workflow, onClose, onChange, readOnly, pinnedRun, currentRun,
  onSendErrorToOrchestrator,
}: Props) {
  // Trace tab visibility + data source. Three regimes, in priority order:
  //   1. The pinned run is also the live attached one (rerun-from-snapshot
  //      mid-execution, viewed from snapshot view). Stream live events —
  //      the historical NodeRun rows aren't materialised yet.
  //   2. Pinned run only (snapshot view, post-completion). Read the
  //      historical NodeRun for this node from the frozen Run row.
  //   3. Live attached run on this workflow (no pin). Stream live events.
  const liveRunForThisNode =
    currentRun &&
    currentRun.workflow_id === workflow.id &&
    (!pinnedRun || currentRun.id === pinnedRun.id)
      ? currentRun
      : null;

  const trace: NodeTrace | null = useMemo(() => {
    if (liveRunForThisNode) {
      const all = aggregateEvents(liveRunForThisNode.events);
      return all.find((t) => t.node_id === node.id) ?? null;
    }
    if (pinnedRun) {
      const nr = pinnedRun.node_runs.find((x) => x.node_id === node.id);
      return nr ? nodeRunToTrace(nr) : null;
    }
    return null;
  }, [liveRunForThisNode?.events, pinnedRun, node.id]);

  const traceTabAvailable = !!pinnedRun || !!liveRunForThisNode;

  const [tab, setTab] = useState<Tab>('code');
  const [code, setCode] = useState(node.code);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setCode(node.code);
    setDirty(false);
    setTab('code');
  }, [node.id]);

  // If the trace tab disappears (run was cleared, snapshot exited) while it
  // was selected, fall back to code so we don't render an empty pane.
  useEffect(() => {
    if (tab === 'trace' && !traceTabAvailable) setTab('code');
  }, [tab, traceTabAvailable]);

  const isInput = workflow.input_node_id === node.id;
  const isOutput = workflow.output_node_id === node.id;
  const role = isInput && isOutput
    ? null
    : isInput ? 'input' : isOutput ? 'output' : null;

  const save = async () => {
    await api.patchNode(node.id, {
      code,
    });
    setDirty(false);
    onChange();
  };

  return (
    <div className="fade-in" style={PANEL_STYLE}>
      <div style={{ padding: '14px 18px 10px', borderBottom: '1px solid var(--rule)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="smallcaps">node</span>
          <span style={{ flex: 1 }} />
          {dirty && !readOnly && (
            <button className="btn-ink" style={{ padding: '5px 12px', fontSize: 11 }} onClick={save}>
              save
            </button>
          )}
          <CloseButton onClick={onClose} title="close node panel" />
        </div>
        <div
          className="serif mono"
          title={node.name}
          style={{
            fontFamily: 'var(--mono)',
            fontSize: 18,
            marginTop: 6,
            color: 'var(--ink)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {node.name}
        </div>
        {role && (
          <div className="serif" style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--accent-ink)', marginTop: -2 }}>
            · {role}
          </div>
        )}
        {node.description && (
          <div
            className="serif"
            style={{
              fontStyle: 'italic',
              fontSize: 13,
              color: 'var(--ink-3)',
              marginTop: 4,
              lineHeight: 1.45,
            }}
          >
            {node.description}
          </div>
        )}
      </div>

      <div style={{ display: 'flex', borderBottom: '1px solid var(--rule)', padding: '0 18px' }}>
        {/* `trace` shows when this node is part of a run we can read — a
         * frozen snapshot (pinnedRun) or a live in-flight run on this
         * workflow. Otherwise it'd just render an empty pane, so we hide
         * the button. */}
        {(traceTabAvailable
          ? (['code', 'i/o', 'trace'] as const)
          : (['code', 'i/o'] as const)
        ).map((k) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className="smallcaps"
            style={{
              padding: '10px 12px',
              borderBottom: tab === k ? '1.5px solid var(--ink)' : '1.5px solid transparent',
              color: tab === k ? 'var(--ink)' : 'var(--ink-4)',
              marginRight: 4,
              background: 'transparent',
              border: 'none',
              borderBottomStyle: 'solid',
              borderBottomWidth: 1.5,
              borderBottomColor: tab === k ? 'var(--ink)' : 'transparent',
              cursor: 'pointer',
            }}
          >
            {k}
          </button>
        ))}
      </div>

      <div className="scroll" style={{ flex: 1, overflow: 'auto' }}>
        {tab === 'code' && (
          <div style={{ height: '100%', minHeight: 400 }}>
            <Editor
              height="100%"
              theme="vs-dark"
              language="python"
              value={code}
              onChange={(v) => {
                if (readOnly) return;
                setCode(v ?? '');
                setDirty(true);
              }}
              options={{
                minimap: { enabled: false },
                fontSize: 12,
                fontFamily: "'Fragment Mono', ui-monospace, 'SF Mono', Menlo, monospace",
                scrollBeyondLastLine: false,
                lineNumbers: 'off',
                readOnly: !!readOnly,
              }}
            />
          </div>
        )}

        {tab === 'i/o' && (
          <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
            <PortSchemaSection
              title="inputs"
              ports={node.inputs}
              emptyText="no inputs."
              accent="input"
              showRequired
            />
            <PortSchemaSection
              title="outputs"
              ports={node.outputs}
              emptyText="no outputs."
              accent="output"
            />
            <p className="node-io-footnote">
              ports are shaped by ensemble — ask in the chat to add, rename, or remove one.
            </p>
          </div>
        )}

        {tab === 'trace' && (
          <div style={{ padding: 18 }}>
            {trace ? (
              <NodeTraceCard
                workflow={workflow}
                trace={trace}
                runId={pinnedRun?.id ?? liveRunForThisNode?.id}
                onSendErrorToOrchestrator={onSendErrorToOrchestrator}
              />
            ) : (
              <div
                className="serif"
                style={{ fontStyle: 'italic', color: 'var(--ink-3)', fontSize: 13, lineHeight: 1.55 }}
              >
                {liveRunForThisNode
                  ? 'waiting for this node to start…'
                  : 'this node has no trace in the selected run.'}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function PortSchemaSection({
  title,
  ports,
  emptyText,
  accent,
  showRequired,
}: {
  title: string;
  ports: IOPort[];
  emptyText: string;
  accent: 'input' | 'output';
  showRequired?: boolean;
}) {
  return (
    <section className="snapshot-io-section">
      <div className="snapshot-io-section__head">
        <span className="smallcaps snapshot-io-section__title">{title}</span>
        {ports.length > 0 && (
          <span className="snapshot-io-section__count">
            {ports.length} {ports.length === 1 ? 'port' : 'ports'}
          </span>
        )}
      </div>
      {ports.length === 0 ? (
        <div className="snapshot-io-section__empty">{emptyText}</div>
      ) : (
        <div className="snapshot-io-fields">
          {ports.map((port, i) => (
            <PortSchemaCard key={i} port={port} accent={accent} showRequired={showRequired} />
          ))}
        </div>
      )}
    </section>
  );
}

function PortSchemaCard({
  port,
  accent,
  showRequired,
}: {
  port: IOPort;
  accent: 'input' | 'output';
  showRequired?: boolean;
}) {
  return (
    <div className={`port-card port-card--schema port-card--${accent}`}>
      <div className="port-card__head">
        <div className="port-card__label">
          <span className="port-card__name">{port.name}</span>
          <span className="port-card__hint">{port.type_hint || 'any'}</span>
        </div>
        {showRequired && (
          <span
            className={`port-card__status${port.required ? ' port-card__status--required' : ''}`}
          >
            {port.required ? 'required' : 'optional'}
          </span>
        )}
      </div>
    </div>
  );
}


