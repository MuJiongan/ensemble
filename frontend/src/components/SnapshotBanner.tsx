import type { Run } from '../types';

export function SnapshotBanner({ run, onExit }: { run: Run; onExit: () => void }) {
  const statusColor =
    run.status === 'success'
      ? 'var(--state-ok)'
      : run.status === 'error'
        ? 'var(--state-err)'
        : 'var(--ink-4)';
  const statusGlyph =
    run.status === 'success' ? '✓' : run.status === 'error' ? '×' : '·';
  return (
    <div
      style={{
        padding: '6px 12px',
        background: 'var(--paper-2)',
        borderBottom: '1px solid var(--rule)',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        fontSize: 11.5,
        minWidth: 0,
        flexShrink: 0,
      }}
    >
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'baseline',
          gap: 6,
          minWidth: 0,
          overflow: 'hidden',
        }}
      >
        <span style={{ color: statusColor, fontSize: 10 }}>{statusGlyph}</span>
        <span
          className="serif"
          style={{
            fontStyle: 'italic',
            color: 'var(--ink-3)',
            fontSize: 12,
            whiteSpace: 'nowrap',
          }}
        >
          snapshot
        </span>
        <span
          className="mono"
          style={{
            color: 'var(--ink-4)',
            fontSize: 10.5,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {run.id.slice(0, 8)}
        </span>
      </span>
      <span style={{ flex: 1 }} />
      <button
        type="button"
        onClick={onExit}
        style={{
          background: 'transparent',
          border: 0,
          padding: '2px 0',
          cursor: 'pointer',
          color: 'var(--accent-ink)',
          fontSize: 11.5,
          fontFamily: 'var(--serif)',
          fontStyle: 'italic',
          whiteSpace: 'nowrap',
        }}
        title="return to the live, editable canvas"
      >
        ← live
      </button>
    </div>
  );
}
