import { useEffect, useRef } from 'react';

interface Props {
  value: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  placeholder?: string;
  footerHint?: string;
  actionLabel: string;
  onAction: () => void;
  actionDisabled?: boolean;
  disabled?: boolean;
  autoFocus?: boolean;
  minRows?: number;
  /** Max height of the green box; content scrolls inside once full. */
  maxComposerHeight?: string;
}

/** Shared hero composer shell — the green-bordered input used on the landing
 * page and for project import/export. */
export function HeroComposer({
  value,
  onChange,
  readOnly = false,
  placeholder,
  footerHint,
  actionLabel,
  onAction,
  actionDisabled = false,
  disabled = false,
  autoFocus = false,
  minRows = 3,
  maxComposerHeight = 'min(55vh, 400px)',
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const minTextareaHeight = Math.max(72, minRows * 24);

  useEffect(() => {
    if (!autoFocus) return;
    textareaRef.current?.focus();
  }, [autoFocus]);

  return (
    <div
      className="hero-input field-shell"
      style={{
        padding: '14px 16px 12px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        maxHeight: maxComposerHeight,
        minHeight: 0,
      }}
    >
      <textarea
        ref={textareaRef}
        rows={minRows}
        readOnly={readOnly}
        className="field field--plain field--prose field--scrollable"
        placeholder={placeholder}
        value={value}
        onChange={onChange ? (e) => onChange(e.target.value) : undefined}
        disabled={disabled}
        spellCheck={false}
        style={{
          resize: 'none',
          fontStyle: 'italic',
          fontSize: 16,
          padding: '2px 4px',
          flex: 1,
          minHeight: minTextareaHeight,
          minWidth: 0,
        }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
        {footerHint ? (
          <span
            className="serif"
            style={{ fontStyle: 'italic', fontSize: 11.5, color: 'var(--ink-4)' }}
          >
            {footerHint}
          </span>
        ) : (
          <span />
        )}
        <span style={{ flex: 1 }} />
        <button
          type="button"
          className="btn-ink btn-ink--accent"
          onClick={onAction}
          disabled={actionDisabled}
        >
          {actionLabel} <span className="italic-em">→</span>
        </button>
      </div>
    </div>
  );
}
