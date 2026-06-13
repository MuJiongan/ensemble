import { useEffect, useMemo, useState } from 'react';
import { IOSection } from './IOSection';
import type { CurrentRun, Run, RunStatus } from '../types';
import { api } from '../api';
import { modelStatsForRun, summariseRun, toolCallCountForRun } from '../appHelpers';
import { ExecutionStats } from './ExecutionStats';

export function SnapshotRunPanel({
  run,
  onExit,
  onRerun,
  currentRun,
}: {
  run: Run;
  onExit: () => void;
  onRerun: (inputs: Record<string, unknown>) => Promise<void>;
  /** When set and bound to this run, live llm_call_finished events are
   * folded into model stats before node_runs are persisted. */
  currentRun?: CurrentRun | null;
}) {
  const errored = run.node_runs.filter((nr) => nr.status === 'error');
  const inputs = Object.entries(run.inputs ?? {});

  // The viewed run's live status when its WS is attached, else the status
  // frozen into the Run row at fetch time. The live stream leads the DB —
  // `run_finished` arrives over the WS before the Run row is persisted (and
  // before the parent's refetch lands) — so prefer it for everything that
  // should flip the moment the run ends: the status line, the in-flight
  // cancel, and the final outputs.
  const liveStatus: RunStatus =
    currentRun && currentRun.id === run.id ? currentRun.status : run.status;
  const liveOutputs =
    currentRun && currentRun.id === run.id ? currentRun.finalOutputs : null;
  const outputs = Object.entries(liveOutputs ?? run.outputs ?? {});
  const inFlight = liveStatus === 'running' || liveStatus === 'pending';
  const [cancelling, setCancelling] = useState(false);
  useEffect(() => { setCancelling(false); }, [run.id]);
  const cancelThisRun = async () => {
    setCancelling(true);
    try { await api.cancelRun(run.id); } catch { /* ignore */ }
  };

  // Re-run form state. The snapshot's input node defines the port shape;
  // we pre-fill each field with the prior run's value (JSON-stringified)
  // so the user can tweak just one knob and rerun.
  const inputPorts = (() => {
    const snap = run.workflow_snapshot;
    if (!snap) return [];
    const inputNode = snap.nodes.find((n) => n.id === snap.input_node_id);
    return inputNode?.inputs ?? [];
  })();
  const initialFormValues = useMemo<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    for (const p of inputPorts) {
      const prior = (run.inputs ?? {})[p.name];
      out[p.name] = prior === undefined || prior === null
        ? ''
        : typeof prior === 'string'
          ? prior
          : JSON.stringify(prior);
    }
    return out;
    // Snapshot inputs are immutable for a given run.id, so we intentionally
    // key only off run.id. Depending on the inputPorts/initialFormValues
    // identity would reset the form whenever the parent swaps in a refreshed
    // Run object (e.g. post-finish polling), clobbering in-progress edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id]);
  const [formOpen, setFormOpen] = useState(false);
  const [formValues, setFormValues] = useState<Record<string, string>>(initialFormValues);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  useEffect(() => {
    setFormOpen(false);
    setFormValues(initialFormValues);
    setSubmitting(false);
    setSubmitError(null);
    // See note on initialFormValues: reset only when the bound run id changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id]);

  const submitRerun = async () => {
    const parsed: Record<string, unknown> = {};
    for (const p of inputPorts) {
      const raw = formValues[p.name];
      if (raw === undefined || raw === '') {
        parsed[p.name] = null;
        continue;
      }
      try { parsed[p.name] = JSON.parse(raw); } catch { parsed[p.name] = raw; }
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onRerun(parsed);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      // On success the parent swaps `run` to the new run, which also resets
      // the form via the run.id effect — but the same SnapshotRunPanel
      // instance keeps its state, so clear `submitting` here too or the
      // execute button stays stuck on "starting…" for the next rerun.
      setSubmitting(false);
    }
  };
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        padding: '20px 24px',
        overflow: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
      }}
    >
      {(() => {
        const summary = summariseRun(run);
        const okCount = run.node_runs.filter((n) => n.status === 'success').length;
        const totalNodes = run.workflow_snapshot?.nodes.length ?? 0;
        const liveEvents =
          currentRun && currentRun.id === run.id ? currentRun.events : undefined;
        const modelStats = modelStatsForRun(run, liveEvents);
        const toolCalls = toolCallCountForRun(run, liveEvents);
        return (
          <div>
            <div
              style={{
                display: 'flex',
                alignItems: 'baseline',
                justifyContent: 'space-between',
                gap: 12,
                marginBottom: 4,
              }}
            >
              <span className="smallcaps" style={{ color: 'var(--ink-3)' }}>
                run details
              </span>
              <span
                className="mono"
                title={run.id}
                style={{
                  color: 'var(--ink-4)',
                  fontSize: 10,
                  letterSpacing: '0.02em',
                }}
              >
                {run.id.slice(0, 8)}
              </span>
            </div>
            {summary.kind === 'value' ? (
              <div
                className="serif"
                style={{
                  fontSize: 24,
                  fontStyle: 'italic',
                  color: 'var(--ink)',
                  lineHeight: 1.15,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={summary.text}
              >
                {summary.text}
              </div>
            ) : (
              <div
                className="serif"
                style={{
                  fontSize: 20,
                  fontStyle: 'italic',
                  color: 'var(--ink-3)',
                  lineHeight: 1.15,
                }}
              >
                no inputs
              </div>
            )}
            {/* Byline-style stat row — no card chrome, just italic-serif
                values separated by serif dots. Reads as a credit line under
                the headline rather than a dashboard widget. */}
            <div
              className="serif"
              style={{
                marginTop: 8,
                display: 'flex',
                alignItems: 'baseline',
                flexWrap: 'wrap',
                gap: 0,
                fontSize: 13,
                fontStyle: 'italic',
                color: 'var(--ink-3)',
              }}
            >
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <StatusDot status={liveStatus} />
                <span style={{ color: runStatusColor(liveStatus) }}>{liveStatus}</span>
              </span>
              <span className="asterisk" style={{ margin: '0 10px' }}>·</span>
              <span className="mono" style={{ color: 'var(--ink-2)' }}>
                ${(run.total_cost ?? 0).toFixed(4)}
              </span>
              <span className="asterisk" style={{ margin: '0 10px' }}>·</span>
              <span style={{ color: 'var(--ink-2)' }}>
                <span className="mono">{totalNodes}</span> nodes
                <span style={{ color: 'var(--ink-4)' }}>
                  {' ('}
                  {okCount} ok
                  {errored.length > 0 && `, ${errored.length} err`}
                  {')'}
                </span>
              </span>
            </div>
            <ExecutionStats
              modelStats={modelStats}
              toolCalls={toolCalls}
              marginTop={8}
            />
            <div
              style={{
                marginTop: 14,
                height: 1,
                background: 'var(--rule)',
              }}
            />
          </div>
        );
      })()}

      {errored.length > 0 && (
        <div>
          <div className="smallcaps" style={{ color: 'var(--ink-3)', marginBottom: 6 }}>
            errors
          </div>
          {errored.map((nr) => {
            const nodeName =
              run.workflow_snapshot?.nodes.find((n) => n.id === nr.node_id)?.name ??
              nr.node_id;
            return (
              <div
                key={nr.id}
                style={{
                  background: 'var(--err-bg)',
                  borderLeft: '2px solid var(--state-err)',
                  padding: '6px 10px',
                  marginBottom: 6,
                  fontSize: 12,
                }}
              >
                <div className="mono" style={{ color: 'var(--state-err)' }}>
                  {nodeName}
                </div>
                <div
                  className="serif"
                  style={{ fontStyle: 'italic', color: 'var(--ink-2)' }}
                >
                  {nr.error}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <IOSection
        title="inputs"
        emptyText="this run used no inputs."
        accent="input"
        entries={inputs.map(([k, v]) => ({
          name: k,
          value: v,
          viewerTitle: `run ${run.id.slice(0, 8)} · ${k}`,
          viewerSubtitle: 'input',
        }))}
      />
      <IOSection
        title="outputs"
        emptyText={
          liveStatus === 'success'
            ? 'this run produced no outputs.'
            : 'no outputs recorded.'
        }
        accent="output"
        entries={outputs.map(([k, v]) => ({
          name: k,
          value: v,
          viewerTitle: `run ${run.id.slice(0, 8)} · ${k}`,
          viewerSubtitle: 'output',
        }))}
      />

      {/* in-flight cancel — this run is still executing; stopping it is the
          one action that makes sense here, so it takes the rerun's spot. */}
      {inFlight && (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <button
            type="button"
            onClick={cancelThisRun}
            disabled={cancelling}
            className="snapshot-action-btn snapshot-action-btn--secondary"
            style={{ color: 'var(--state-err)' }}
            title="stop this run"
          >
            {cancelling ? 'cancelling…' : 'cancel run'}
          </button>
        </div>
      )}

      {/* rerun affordance — visible whenever the snapshot is runnable
          (has a designated input node). When the input node has no input
          ports, the form skips field rendering and just confirms execute. */}
      {!inFlight && run.workflow_snapshot?.input_node_id && !formOpen && (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <button
            type="button"
            onClick={() => setFormOpen(true)}
            className="snapshot-action-btn"
            title={
              inputPorts.length > 0
                ? 'run this exact graph again with edited inputs'
                : 'run this exact graph again'
            }
          >
            {inputPorts.length > 0 ? 'rerun with new inputs →' : 'rerun →'}
          </button>
        </div>
      )}

      {formOpen && (
        <section className="snapshot-rerun-form">
          <div className="snapshot-rerun-form__head">
            <span className="smallcaps snapshot-rerun-form__title">
              {inputPorts.length > 0 ? 'new inputs' : 'rerun'}
            </span>
            <span className="snapshot-rerun-form__hint">
              runs the snapshot, not the live graph
            </span>
          </div>

          {inputPorts.length === 0 ? (
            <div className="snapshot-rerun-form__empty">
              this project takes no inputs.
            </div>
          ) : (
            <div className="snapshot-rerun-form__fields">
              {inputPorts.map((p) => (
                <label key={p.name} className="snapshot-rerun-form__field">
                  <span className="snapshot-rerun-form__label">
                    {p.name}
                    {p.type_hint && p.type_hint !== 'any' && (
                      <span className="snapshot-rerun-form__label-meta">{p.type_hint}</span>
                    )}
                    {p.required && (
                      <span className="snapshot-rerun-form__label-required" title="required">
                        required
                      </span>
                    )}
                  </span>
                  <textarea
                    value={formValues[p.name] ?? ''}
                    onChange={(e) =>
                      setFormValues((prev) => ({ ...prev, [p.name]: e.target.value }))
                    }
                    rows={1}
                    className="field field--prose field--compact field--underline"
                    placeholder={
                      p.type_hint === 'path' ? '/users/you/recordings' : 'plain text or json'
                    }
                    style={{ minHeight: 28 }}
                    disabled={submitting}
                  />
                </label>
              ))}
            </div>
          )}

          {submitError && (
            <div className="snapshot-rerun-form__error">{submitError}</div>
          )}

          <div className="snapshot-rerun-form__actions">
            <button
              type="button"
              onClick={submitRerun}
              disabled={submitting}
              className="ed-btn ed-btn--primary ed-btn--mini"
            >
              {submitting ? 'starting…' : 'execute'}{' '}
              <span className="ed-btn__mark">→</span>
            </button>
            <button
              type="button"
              onClick={() => {
                setFormOpen(false);
                setFormValues(initialFormValues);
                setSubmitError(null);
              }}
              disabled={submitting}
              className="ed-btn ed-btn--mini"
            >
              cancel
            </button>
          </div>
        </section>
      )}

      <span style={{ flex: 1 }} />

      <button
        type="button"
        onClick={onExit}
        className="snapshot-action-btn"
        style={{ alignSelf: 'flex-start' }}
      >
        ← back to live
      </button>
    </div>
  );
}

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

function StatusDot({ status }: { status: RunStatus }) {
  return (
    <span
      aria-hidden
      style={{
        width: 7,
        height: 7,
        borderRadius: '50%',
        background: runStatusColor(status),
        display: 'inline-block',
        flex: 'none',
      }}
    />
  );
}

