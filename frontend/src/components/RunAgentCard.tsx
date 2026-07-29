import type { RunEvent } from '../types';

export type RunAgentToolStatus = 'pending' | 'ok' | 'err';

export interface RunAgentResult {
  status?: string;
  outputs?: Record<string, unknown>;
  error?: string | null;
  duration_ms?: number;
  total_cost?: number;
  _display?: {
    run_id?: string;
    node_run?: Record<string, unknown>;
  };
}

export interface RunAgentInspection {
  name: string;
  description: string;
  code: string;
  status: RunAgentToolStatus;
  result?: RunAgentResult;
  runEvents: RunEvent[];
}

interface Props {
  args: string;
  argsFull?: Record<string, unknown> | null;
  status: RunAgentToolStatus;
  result?: unknown;
  onInspect?: (inspection: RunAgentInspection) => void;
}

export function RunAgentCard({
  argsFull,
  status,
  result,
  onInspect,
}: Props) {
  const config = argsFull ?? {};
  const name = typeof config.name === 'string' && config.name.trim()
    ? config.name
    : 'inline_agent';
  const description = typeof config.description === 'string' ? config.description : '';
  const code = typeof config.code === 'string' ? config.code : '';
  const runResult = (result ?? {}) as RunAgentResult;
  const running = status === 'pending';
  const failed = status === 'err' || !!runResult.error;
  const statusLabel = running ? 'running' : failed ? 'failed' : 'done';
  const statusColor = running
    ? 'var(--ink-4)'
    : failed
      ? 'var(--state-err)'
      : 'var(--state-ok)';

  const inspect = () => onInspect?.({
    name,
    description,
    code,
    status,
    result: runResult,
    // Live events have a single owner in App and are joined to this
    // inspection by invocation id when the card is opened.
    runEvents: [],
  });

  return (
    <button
      type="button"
      className="tool-call run-agent-card fade-in"
      onClick={inspect}
      title="inspect this agent in the left pane"
      aria-label={`Inspect agent ${name}`}
      data-status={statusLabel}
      style={{ cursor: onInspect ? 'pointer' : 'default' }}
    >
      <span className="run-agent-card__header">
        <span className="run-agent-card__eyebrow smallcaps">agent</span>
        <span className="run-agent-card__status smallcaps" style={{ color: statusColor }}>
          <span className={`node-state-dot ${running ? 'running' : failed ? 'error' : 'success'}`} />
          {statusLabel}
        </span>
      </span>

      <span className="run-agent-card__body">
        <span className="run-agent-card__name mono">{name}</span>
        {description && (
          <span className="run-agent-card__description serif">{description}</span>
        )}
      </span>
    </button>
  );
}
