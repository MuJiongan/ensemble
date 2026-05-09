import { useEffect, useState } from 'react';
import type {
  WorkflowDetail, Run, RunStatus, IOPort, CurrentRun,
} from '../types';
import { api } from '../api';
import { DEFAULT_WORKFLOW_NAME, summariseRun } from '../appHelpers';

function runStatusColor(status: RunStatus): string {
  switch (status) {
    case 'success': return 'var(--state-ok)';
    case 'error': return 'var(--state-err)';
    case 'running':
    case 'pending':
      return 'var(--state-run)';
    case 'cancelled': return 'var(--ink-4)';
  }
}

interface Props {
  workflow: WorkflowDetail;
  currentRun: CurrentRun | null;
  onStart: (inputs: Record<string, unknown>) => void;
  onCancel: () => void;
  /** Optional close handler. When omitted, the panel is treated as the
   * always-on default surface and the close button is hidden. */
  onClose?: () => void;
  /** When set, the history list shows a "view on canvas" affordance per run.
   * The host (App) handles fetching the snapshot and swapping the canvas. */
  onViewRunOnCanvas?: (runId: string) => void;
  /** True while an orchestrator turn is streaming for this workflow. The
   * graph may be mid-build (added nodes, no edges yet) or about to mutate
   * again — manual runs are blocked until the turn settles. */
  orchestrating?: boolean;
}

const PANEL_STYLE: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  background: 'var(--paper)',
  display: 'flex',
  flexDirection: 'column',
  zIndex: 30,
};

export function RunPanel({
  workflow,
  currentRun,
  onStart,
  onCancel,
  onClose,
  onViewRunOnCanvas,
  orchestrating,
}: Props) {
  const inputNode = workflow.nodes.find((n) => n.id === workflow.input_node_id);
  const inputPorts: IOPort[] = inputNode?.inputs ?? [];
  const [values, setValues] = useState<Record<string, string>>({});
  const [history, setHistory] = useState<Run[]>([]);

  // Only consider currentRun ours if it belongs to the workflow we're showing.
  const ownRun = currentRun && currentRun.workflow_id === workflow.id ? currentRun : null;

  useEffect(() => {
    api.listRuns(workflow.id).then(setHistory).catch(() => {});
  }, [workflow.id]);

  // Refresh history whenever the attached run changes (a new one was
  // started/clicked) or its status changes. Covers both the "the run just
  // started, show it in the list" case and the "the run finished, flip its
  // status" case in one effect.
  useEffect(() => {
    if (!ownRun) return;
    api.listRuns(workflow.id).then(setHistory).catch(() => {});
  }, [ownRun?.id, ownRun?.status, workflow.id]);

  // While any row in `history` still reads as running/pending, poll so its
  // status flips once the backend marks it terminal. Covers the case where
  // the panel didn't observe the run start (e.g. reopened mid-flight) so
  // `ownRun` is null and the effect above never fires. Self-stops once no
  // row is in flight.
  const anyHistoryRunning = history.some(
    (h) => h.status === 'running' || h.status === 'pending',
  );
  useEffect(() => {
    if (!anyHistoryRunning) return;
    const id = setInterval(() => {
      api.listRuns(workflow.id).then(setHistory).catch(() => {});
    }, 3000);
    return () => clearInterval(id);
  }, [anyHistoryRunning, workflow.id]);

  const start = () => {
    if (!workflow.input_node_id) {
      alert('set an input node first (click a node, then "set as input").');
      return;
    }
    if (!workflow.output_node_id) {
      if (!confirm('no output node set. continue anyway?')) return;
    }
    const inputs: Record<string, unknown> = {};
    for (const p of inputPorts) {
      const raw = values[p.name];
      if (raw === undefined || raw === '') {
        inputs[p.name] = null;
        continue;
      }
      try { inputs[p.name] = JSON.parse(raw); } catch { inputs[p.name] = raw; }
    }
    onStart(inputs);
  };

  const running = ownRun?.status === 'running' || ownRun?.status === 'pending';
  const status = ownRun?.status;

  return (
    <div className="fade-in" style={PANEL_STYLE}>
      <div style={{ padding: '14px 18px 12px', borderBottom: '1px solid var(--rule)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="smallcaps">run</span>
          <span style={{ flex: 1 }} />
          {onClose && (
            <button
              className="text-btn"
              onClick={onClose}
              title="close run panel"
            >
              close
            </button>
          )}
        </div>
        <div
          className="serif"
          style={{
            fontStyle: 'italic',
            fontSize: 22,
            marginTop: 6,
            color: workflow.name ? 'var(--ink)' : 'var(--ink-3)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
          title={workflow.name || DEFAULT_WORKFLOW_NAME}
        >
          {workflow.name || DEFAULT_WORKFLOW_NAME}
        </div>
        {inputNode && (
          <div className="serif" style={{ fontStyle: 'italic', fontSize: 12.5, color: 'var(--ink-3)', marginTop: 2 }}>
            entry — <span className="mono" style={{ fontStyle: 'normal' }}>{inputNode.name}</span>
          </div>
        )}
      </div>

      <div
        className="scroll"
        style={{
          flex: 1,
          overflow: 'auto',
          padding: 22,
          display: 'flex',
          flexDirection: 'column',
          gap: 22,
        }}
      >
        <div>
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              gap: 8,
              marginBottom: 6,
            }}
          >
            <span className="smallcaps" style={{ color: 'var(--ink-3)' }}>
              project runs
            </span>
            {history.length > 0 && (
              <span
                className="serif"
                style={{
                  fontStyle: 'italic',
                  color: 'var(--ink-4)',
                  fontSize: 11.5,
                }}
              >
                {history.length} {history.length === 1 ? 'run' : 'runs'}
              </span>
            )}
          </div>
          <div style={{ borderTop: '1px solid var(--rule)' }}>
            {history.length === 0 ? (
              <div
                className="serif"
                style={{
                  fontStyle: 'italic',
                  color: 'var(--ink-4)',
                  fontSize: 12.5,
                  padding: '10px 0',
                }}
              >
                no runs yet — fill inputs below and execute.
              </div>
            ) : (
              history.map((h) => {
              const summary = summariseRun(h);
              const isId = summary.kind === 'id';
              const rowRunning = h.status === 'running' || h.status === 'pending';
              const canView = !!onViewRunOnCanvas;
              const canDelete = !rowRunning;
              const onView = () => onViewRunOnCanvas?.(h.id);
              const onDelete = async (e: React.MouseEvent) => {
                e.stopPropagation();
                if (!confirm('delete this run? its trace and outputs will be lost.')) return;
                try {
                  await api.deleteRun(h.id);
                  setHistory((prev) => prev.filter((r) => r.id !== h.id));
                } catch (err) {
                  alert(`couldn't delete run: ${err instanceof Error ? err.message : String(err)}`);
                }
              };
              return (
                <div
                  key={h.id}
                  role={canView ? 'button' : undefined}
                  tabIndex={canView ? 0 : -1}
                  onClick={canView ? onView : undefined}
                  onKeyDown={canView ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onView(); }
                  } : undefined}
                  title={
                    canView
                      ? "view this run's graph on the canvas"
                      : isId
                        ? h.id
                        : summary.text
                  }
                  style={{
                    display: 'flex',
                    width: '100%',
                    padding: '6px 8px',
                    margin: '0 -8px',
                    alignItems: 'center',
                    gap: 8,
                    background: rowRunning ? '#fbf7ec' : 'transparent',
                    borderBottom: '1px solid var(--rule-2)',
                    borderLeft: rowRunning
                      ? '2px solid var(--state-run)'
                      : '2px solid transparent',
                    cursor: canView ? 'pointer' : 'default',
                    textAlign: 'left',
                  }}
                >
                  <span
                    className={isId ? 'mono' : 'serif'}
                    style={{
                      fontSize: isId ? 10.5 : 12.5,
                      color: isId ? 'var(--ink-4)' : 'var(--ink-2)',
                      flex: 1,
                      minWidth: 0,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {summary.text}
                  </span>
                  <span
                    className="smallcaps"
                    style={{
                      fontSize: 9,
                      color: runStatusColor(h.status),
                      fontWeight: rowRunning ? 600 : undefined,
                    }}
                  >
                    {h.status}
                  </span>
                  <button
                    type="button"
                    onClick={onDelete}
                    disabled={!canDelete}
                    title={canDelete ? 'delete this run' : 'cancel the run before deleting'}
                    aria-label="delete run"
                    style={{
                      background: 'transparent',
                      border: 0,
                      padding: '0 4px',
                      cursor: canDelete ? 'pointer' : 'not-allowed',
                      color: 'var(--ink-4)',
                      fontSize: 14,
                      lineHeight: 1,
                      opacity: canDelete ? 0.6 : 0.25,
                    }}
                    onMouseEnter={(e) => { if (canDelete) e.currentTarget.style.opacity = '1'; }}
                    onMouseLeave={(e) => { if (canDelete) e.currentTarget.style.opacity = '0.6'; }}
                  >
                    ×
                  </button>
                </div>
              );
            })
            )}
          </div>
        </div>

      </div>

      <div
        style={{
          background: 'var(--paper-2)',
          borderTop: '1px solid var(--rule)',
          display: 'flex',
          flexDirection: 'column',
          flexShrink: 0,
        }}
      >
        <div style={{ padding: '14px 18px 12px' }}>
          <div
            className="smallcaps"
            style={{ color: 'var(--ink-3)', marginBottom: 12 }}
          >
            input
          </div>
          {!inputNode && (
            <div
              className="serif"
              style={{
                fontStyle: 'italic',
                color: 'var(--ink-3)',
                fontSize: 13,
              }}
            >
              no input node selected. open a node and mark it as input from the config tab.
            </div>
          )}
          {inputNode && inputPorts.length === 0 && (
            <div
              className="serif"
              style={{
                fontStyle: 'italic',
                color: 'var(--ink-4)',
                fontSize: 13,
              }}
            >
              this node takes no declared inputs.
            </div>
          )}
          <div
            className="scroll"
            style={{ maxHeight: 240, overflowY: 'auto' }}
          >
            {inputPorts.map((p, idx) => (
              <div
                key={p.name}
                style={{ marginTop: idx === 0 ? 0 : 18 }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: 8,
                    marginBottom: 2,
                  }}
                >
                  <span
                    className="mono"
                    style={{ fontSize: 11.5, color: 'var(--ink-2)' }}
                  >
                    {p.name}
                  </span>
                  {p.type_hint && p.type_hint !== 'any' && (
                    <span
                      className="serif"
                      style={{ fontStyle: 'italic', color: 'var(--ink-4)', fontSize: 11 }}
                    >
                      {p.type_hint}
                    </span>
                  )}
                  <span style={{ flex: 1 }} />
                  <span
                    className="serif"
                    style={{
                      fontStyle: 'italic',
                      fontSize: 11,
                      color: p.required ? 'var(--accent-ink)' : 'var(--ink-4)',
                    }}
                  >
                    {p.required ? 'required' : 'optional'}
                  </span>
                </div>
                <textarea
                  rows={1}
                  className="field"
                  value={values[p.name] ?? ''}
                  onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
                  placeholder={p.type_hint === 'path' ? '/users/you/recordings' : 'plain text or json'}
                  style={{
                    fontFamily: 'var(--mono)',
                    fontStyle: 'normal',
                    fontSize: 12.5,
                    padding: '5px 0',
                    minHeight: 26,
                    resize: 'vertical',
                    lineHeight: 1.5,
                  }}
                />
              </div>
            ))}
          </div>
        </div>
        <div
          style={{
            padding: '12px 18px 14px',
            borderTop: '1px solid var(--rule)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <span
            className="smallcaps"
            style={{
              color:
                running || orchestrating ? 'var(--state-run)' :
                status === 'error' ? 'var(--state-err)' :
                status === 'success' ? 'var(--state-ok)' :
                'var(--ink-3)',
            }}
          >
            new run
          </span>
          {running ? (
            <button className="ed-btn ed-btn--danger" onClick={onCancel}>
              cancel <span className="ed-btn__mark">×</span>
            </button>
          ) : (
            <button
              className="ed-btn ed-btn--primary"
              onClick={start}
              disabled={!inputNode || !!orchestrating}
              title={
                orchestrating
                  ? 'wait until the orchestrator finishes its turn'
                  : status === 'error'
                    ? 'last run failed — adjust and try again'
                    : status === 'cancelled'
                      ? 'last run was cancelled — adjust and rerun'
                      : status === 'success'
                        ? 'tweak inputs and rerun'
                        : undefined
              }
            >
              {status === 'error' || status === 'cancelled'
                ? 'try again'
                : status === 'success'
                  ? 'rerun'
                  : 'execute'}{' '}
              <span className="ed-btn__mark">→</span>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

