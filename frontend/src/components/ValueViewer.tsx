import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { JsonView } from './JsonView';
import { Markdown } from './Markdown';
import { CloseButton } from './CloseButton';

/**
 * Compact, clickable row for a single named value (e.g. a node input/output
 * port, a log bundle, a tool-call result). Shows a one-line preview plus a
 * size hint; clicking opens a full-screen overlay with the rendered value.
 *
 * The goal is to keep the run-trace dense — one row per piece of data —
 * while still making the full payload trivially reachable.
 */

const MD_HINTS =
  /(^|\n)\s*(#{1,6}\s|[-*+]\s|>\s|\d+\.\s|```)|\*\*[^*]+\*\*|__[^_]+__|\[[^\]]+\]\([^)]+\)|^\|.+\|$/m;

// LaTeX math: $$...$$ display, \(...\) / \[...\] explicit delimiters, or
// $...$ inline whose body contains a backslash/^/_ (avoids currency false
// positives like "I owe $5").
const MATH_HINTS = /\$\$[\s\S]+?\$\$|\\\(|\\\[|\$[^$\n]*[\\^_][^$\n]*\$/;

function looksLikeMarkdown(s: string): boolean {
  if (s.length < 3) return false;
  if (MATH_HINTS.test(s)) return true;
  if (s.length < 12) return false;
  return MD_HINTS.test(s);
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

interface PreviewInfo {
  text: string;        // one-line preview (already truncated)
  size: string | null; // size badge (null = hide)
  // primitives small enough we can render inline without an overlay.
  inline: boolean;
}

function describe(value: unknown): PreviewInfo {
  if (value === null) return { text: 'null', size: null, inline: true };
  if (value === undefined) return { text: 'undefined', size: null, inline: true };
  if (typeof value === 'boolean') return { text: String(value), size: null, inline: true };
  if (typeof value === 'number') return { text: String(value), size: null, inline: true };
  if (typeof value === 'string') {
    const collapsed = value.replace(/\s+/g, ' ').trim();
    const lines = value.split('\n').length;
    const chars = value.length;
    const inline = !value.includes('\n') && chars <= 80;
    const size = inline
      ? null
      : lines > 1
        ? `${lines} lines · ${formatBytes(chars)}`
        : formatBytes(chars);
    return {
      text: collapsed.length > 120 ? collapsed.slice(0, 120) + '…' : collapsed,
      size,
      inline,
    };
  }
  if (Array.isArray(value)) {
    return {
      text: value.length === 0 ? '[ ]' : `[${value.length === 1 ? '1 item' : `${value.length} items`}]`,
      size: null,
      inline: value.length === 0,
    };
  }
  if (typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>);
    return {
      text:
        keys.length === 0
          ? '{ }'
          : `{ ${keys.slice(0, 4).join(', ')}${keys.length > 4 ? ', …' : ''} }`,
      size: keys.length ? `${keys.length} ${keys.length === 1 ? 'key' : 'keys'}` : null,
      inline: keys.length === 0,
    };
  }
  return { text: String(value), size: null, inline: true };
}

function ValueBody({ value, large = false }: { value: unknown; large?: boolean }) {
  if (typeof value === 'string') {
    if (looksLikeMarkdown(value)) {
      return <Markdown large={large}>{value}</Markdown>;
    }
    return (
      <pre
        className="mono viewer-plain"
        style={{
          fontSize: large ? 13.5 : 12,
          lineHeight: large ? 1.6 : 1.5,
          color: 'var(--ink-2)',
          margin: 0,
          padding: large ? '16px 18px' : '12px 16px',
          background: 'var(--surface-raised)',
          border: '1px solid var(--rule)',
          borderRadius: 4,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {value}
      </pre>
    );
  }
  return <JsonView value={value} large={large} />;
}

function copy(value: unknown) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  navigator.clipboard?.writeText(text).catch(() => {});
}

interface ViewerOverlayProps {
  title: string;
  subtitle?: string;
  value: unknown;
  onClose: () => void;
}

function parseViewerTitle(title: string): { runRef: string | null; name: string } {
  const sep = title.indexOf(' · ');
  if (sep === -1) return { runRef: null, name: title };
  const runRef = title.slice(0, sep).trim();
  const name = title.slice(sep + 3).trim();
  return { runRef: runRef || null, name: name || title };
}

export function ViewerOverlay({ title, subtitle, value, onClose }: ViewerOverlayProps) {
  const [copied, setCopied] = useState(false);
  const { runRef, name } = parseViewerTitle(title);
  const isMarkdown = typeof value === 'string' && looksLikeMarkdown(value);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  const onCopy = () => {
    copy(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return createPortal(
    <div onClick={onClose} className="viewer-backdrop fade-in">
      <aside
        onClick={(e) => e.stopPropagation()}
        className="viewer-panel"
        role="dialog"
        aria-labelledby="viewer-title"
      >
        <header className="viewer-panel__head">
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 16,
            }}
          >
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="viewer-panel__meta">
                {subtitle && <span className="smallcaps viewer-panel__kind">{subtitle}</span>}
                {runRef && <span className="viewer-panel__run">{runRef}</span>}
              </div>
              <h2 id="viewer-title" className="viewer-panel__title">
                {name}
              </h2>
            </div>
            <CloseButton onClick={onClose} title="close (esc)" />
          </div>
          <div className="viewer-panel__actions">
            <button className="text-btn text-btn--accent" onClick={onCopy}>
              {copied ? 'copied' : 'copy'}
            </button>
            <span style={{ flex: 1 }} />
            <span className="viewer-panel__hint">esc to close</span>
          </div>
        </header>
        <div className={`viewer-panel__body scroll${isMarkdown ? ' viewer-prose' : ''}`}>
          <ValueBody value={value} large />
        </div>
      </aside>
    </div>,
    document.body,
  );
}

interface PortRowProps {
  name: string;
  typeHint?: string;
  value: unknown;
  /** Title shown in the overlay header when expanded. */
  viewerTitle: string;
  /** Optional subtitle for the overlay header (e.g. "from analyze.summary"). */
  viewerSubtitle?: string;
  /** `card` — raised field tiles for snapshot run details; `row` — dense trace list. */
  variant?: 'row' | 'card';
  /** Left-rail accent when variant is `card`. */
  cardAccent?: 'input' | 'output';
}

/**
 * One row that summarizes a value. Inline-renders trivial primitives;
 * for anything larger it shows a preview + size, and clicking opens
 * the full payload in a fullscreen overlay.
 */
export function PortRow({
  name,
  typeHint,
  value,
  viewerTitle,
  viewerSubtitle,
  variant = 'row',
  cardAccent,
}: PortRowProps) {
  const [open, setOpen] = useState(false);
  const info = describe(value);

  if (variant === 'card') {
    const accentClass =
      cardAccent === 'input'
        ? 'port-card--input'
        : cardAccent === 'output'
          ? 'port-card--output'
          : '';
    const cardClass = `port-card${accentClass ? ` ${accentClass}` : ''}`;
    const head = (
      <div className="port-card__head">
        <div className="port-card__label">
          <span className="port-card__name">{name}</span>
          {typeHint && <span className="port-card__hint">{typeHint}</span>}
        </div>
        {!info.inline && (
          <div className="port-card__meta">
            {info.size && <span className="port-card__size">{info.size}</span>}
            <span className="port-card__open" aria-hidden>
              open <span className="ed-btn__mark">⤢</span>
            </span>
          </div>
        )}
      </div>
    );
    const body = (
      <div
        className={`port-card__value${info.inline ? ' port-card__value--inline' : ''}`}
      >
        {info.text}
      </div>
    );

    if (info.inline) {
      return (
        <div className={cardClass}>
          {head}
          {body}
        </div>
      );
    }

    return (
      <>
        <button
          type="button"
          onClick={() => setOpen(true)}
          className={cardClass}
        >
          {head}
          {body}
        </button>
        {open && (
          <ViewerOverlay
            title={viewerTitle}
            subtitle={viewerSubtitle}
            value={value}
            onClose={() => setOpen(false)}
          />
        )}
      </>
    );
  }

  const labelEl = (
    <>
      <span className="mono" style={{ fontSize: 11.5, color: 'var(--ink)' }}>
        {name}
      </span>
      {typeHint && (
        <span
          className="serif"
          style={{ fontStyle: 'italic', color: 'var(--ink-4)', fontSize: 11 }}
        >
          {typeHint}
        </span>
      )}
    </>
  );

  if (info.inline) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 8,
          padding: '5px 0',
          borderBottom: '1px solid var(--rule-2)',
          fontSize: 12,
        }}
      >
        {labelEl}
        <span
          className="mono"
          style={{
            color: 'var(--ink-2)',
            fontSize: 11,
            flex: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {info.text}
        </span>
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="port-row"
        style={{
          display: 'flex',
          width: '100%',
          alignItems: 'baseline',
          gap: 8,
          padding: '6px 8px',
          margin: '2px -8px',
          background: 'transparent',
          border: 0,
          borderRadius: 3,
          cursor: 'pointer',
          textAlign: 'left',
          fontFamily: 'inherit',
          color: 'inherit',
          transition: 'background .12s',
        }}
      >
        {labelEl}
        <span
          className="serif"
          style={{
            fontStyle: 'italic',
            color: 'var(--ink-3)',
            fontSize: 12,
            flex: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {info.text}
        </span>
        {info.size && (
          <span className="smallcaps" style={{ fontSize: 9, color: 'var(--ink-4)' }}>
            {info.size}
          </span>
        )}
        <span
          className="ed-btn__mark"
          aria-hidden
          style={{ color: 'var(--ink-4)', fontSize: 12, marginLeft: 2 }}
        >
          ⤢
        </span>
      </button>
      {open && (
        <ViewerOverlay
          title={viewerTitle}
          subtitle={viewerSubtitle}
          value={value}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

interface ValueRowProps {
  /** Label shown on the row (e.g. "logs", "result"). */
  label: string;
  value: unknown;
  viewerTitle: string;
  viewerSubtitle?: string;
}

/** Single-row variant where there's no "port name" — just a label and value. */
export function ValueRow({ label, value, viewerTitle, viewerSubtitle }: ValueRowProps) {
  return (
    <PortRow
      name={label}
      value={value}
      viewerTitle={viewerTitle}
      viewerSubtitle={viewerSubtitle}
    />
  );
}
