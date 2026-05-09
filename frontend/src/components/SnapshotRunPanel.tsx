import { useEffect, useMemo, useState } from 'react';
import { PortRow } from './ValueViewer';
import type { Run } from '../types';

export function SnapshotRunPanel({
  run,
  onExit,
  onRerun,
  runInProgress,
}: {
  run: Run;
  onExit: () => void;
  onRerun: (inputs: Record<string, unknown>) => Promise<void>;
  /** True when another run on this workflow is still executing. The UI
   * blocks rerun in that case so we don't stack parallel runs against
   * one workflow. */
  runInProgress?: boolean;
}) {
  const errored = run.node_runs.filter((nr) => nr.status === 'error');
  const inputs = Object.entries(run.inputs ?? {});
  const outputs = Object.entries(run.outputs ?? {});

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
      <div>
        <div className="smallcaps" style={{ color: 'var(--ink-3)', marginBottom: 4 }}>
          run details
        </div>
        <div
          className="serif"
          style={{ fontSize: 18, fontStyle: 'italic', color: 'var(--ink)' }}
        >
          run {run.id.slice(0, 8)}
        </div>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'auto 1fr',
          columnGap: 14,
          rowGap: 4,
          fontSize: 12,
        }}
      >
        <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>status</span>
        <span className="mono" style={{ fontSize: 11 }}>
          {run.status}
        </span>
        <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>cost</span>
        <span className="mono" style={{ fontSize: 11 }}>
          ${(run.total_cost ?? 0).toFixed(4)}
        </span>
        <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>nodes</span>
        <span className="mono" style={{ fontSize: 11 }}>
          {run.workflow_snapshot?.nodes.length ?? 0} ·{' '}
          {run.node_runs.filter((n) => n.status === 'success').length} ok ·{' '}
          {errored.length} err
        </span>
      </div>

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
                  background: 'rgba(180, 60, 60, 0.06)',
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

      {/* rerun affordance — visible whenever the snapshot is runnable
          (has a designated input node). When the input node has no input
          ports, the form skips field rendering and just confirms execute. */}
      {run.workflow_snapshot?.input_node_id && !formOpen && (
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <button
            type="button"
            onClick={() => setFormOpen(true)}
            disabled={!!runInProgress}
            className="smallcaps"
            style={{
              background: 'transparent',
              border: '1px solid var(--rule)',
              padding: '6px 12px',
              cursor: runInProgress ? 'not-allowed' : 'pointer',
              color: runInProgress ? 'var(--ink-4)' : 'var(--accent-ink)',
              fontSize: 10,
              fontFamily: 'var(--serif)',
              fontStyle: 'italic',
              textTransform: 'none',
              letterSpacing: 0,
              opacity: runInProgress ? 0.6 : 1,
            }}
            title={
              runInProgress
                ? 'a run is already in progress on this workflow — wait for it to finish'
                : inputPorts.length > 0
                  ? 'run this exact graph again with edited inputs'
                  : 'run this exact graph again'
            }
          >
            {inputPorts.length > 0 ? 'rerun with new inputs →' : 'rerun →'}
          </button>
          {runInProgress && (
            <span
              className="serif"
              style={{ fontStyle: 'italic', color: 'var(--ink-4)', fontSize: 11.5 }}
            >
              a run is already in flight
            </span>
          )}
        </div>
      )}

      {formOpen && (
        <div
          style={{
            border: '1px solid var(--rule)',
            background: 'var(--paper-2)',
            padding: '12px 14px',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 8,
            }}
          >
            <span className="smallcaps" style={{ color: 'var(--ink-3)' }}>
              {inputPorts.length > 0 ? 'new inputs' : 'rerun'}
            </span>
            <span
              className="serif"
              style={{ fontStyle: 'italic', color: 'var(--ink-4)', fontSize: 11.5 }}
            >
              · runs the snapshot, not the live graph
            </span>
          </div>
          {inputPorts.length === 0 && (
            <div
              className="serif"
              style={{ fontStyle: 'italic', color: 'var(--ink-4)', fontSize: 12 }}
            >
              this workflow takes no inputs.
            </div>
          )}
          {inputPorts.map((p) => (
            <label
              key={p.name}
              style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
            >
              <span
                className="mono"
                style={{ fontSize: 10.5, color: 'var(--ink-3)' }}
              >
                {p.name}
                {p.type_hint && p.type_hint !== 'any' && (
                  <span style={{ color: 'var(--ink-4)' }}>
                    {' · '}
                    {p.type_hint}
                  </span>
                )}
                {p.required && (
                  <span style={{ color: 'var(--accent-ink)', marginLeft: 6 }}>·</span>
                )}
              </span>
              <textarea
                value={formValues[p.name] ?? ''}
                onChange={(e) =>
                  setFormValues((prev) => ({ ...prev, [p.name]: e.target.value }))
                }
                rows={1}
                className="mono"
                style={{
                  fontSize: 11.5,
                  fontFamily: 'var(--mono)',
                  background: 'var(--paper)',
                  border: '1px solid var(--rule)',
                  padding: '6px 8px',
                  resize: 'vertical',
                  minHeight: 28,
                  color: 'var(--ink)',
                }}
                disabled={submitting}
              />
            </label>
          ))}
          {submitError && (
            <div
              className="serif"
              style={{
                fontStyle: 'italic',
                color: 'var(--state-err)',
                fontSize: 11.5,
              }}
            >
              {submitError}
            </div>
          )}
          {runInProgress && (
            <div
              className="serif"
              style={{
                fontStyle: 'italic',
                color: 'var(--state-err)',
                fontSize: 11.5,
              }}
            >
              another run is in flight on this workflow — wait for it to finish.
            </div>
          )}
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              onClick={submitRerun}
              disabled={submitting || !!runInProgress}
              className="ed-btn ed-btn--primary"
              style={{ fontSize: 11 }}
              title={
                runInProgress
                  ? 'a run is already in progress on this workflow'
                  : undefined
              }
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
              className="ed-btn"
              style={{ fontSize: 11 }}
            >
              cancel
            </button>
          </div>
        </div>
      )}

      {inputs.length > 0 && (
        <div>
          <div className="smallcaps" style={{ color: 'var(--ink-3)', marginBottom: 6 }}>
            inputs
          </div>
          {inputs.map(([k, v]) => (
            <PortRow
              key={k}
              name={k}
              value={v}
              viewerTitle={`run ${run.id.slice(0, 8)} · ${k}`}
              viewerSubtitle="input"
            />
          ))}
        </div>
      )}

      {outputs.length > 0 && (
        <div>
          <div className="smallcaps" style={{ color: 'var(--ink-3)', marginBottom: 6 }}>
            outputs
          </div>
          {outputs.map(([k, v]) => (
            <PortRow
              key={k}
              name={k}
              value={v}
              viewerTitle={`run ${run.id.slice(0, 8)} · ${k}`}
              viewerSubtitle="output"
            />
          ))}
        </div>
      )}

      <span style={{ flex: 1 }} />

      <button
        type="button"
        onClick={onExit}
        className="smallcaps"
        style={{
          alignSelf: 'flex-start',
          background: 'transparent',
          border: '1px solid var(--rule)',
          padding: '6px 12px',
          cursor: 'pointer',
          color: 'var(--accent-ink)',
          fontSize: 10,
          fontFamily: 'var(--serif)',
          fontStyle: 'italic',
          textTransform: 'none',
          letterSpacing: 0,
        }}
      >
        ← back to live
      </button>
    </div>
  );
}
