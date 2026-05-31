import { useEffect, useRef, useState } from 'react';

export function Hero({
  hasApiKey,
  disabled,
  onSend,
  onOpenSettings,
}: {
  hasApiKey: boolean;
  disabled: boolean;
  onSend: (text: string) => void;
  onOpenSettings: () => void;
}) {
  const [text, setText] = useState('');
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (hasApiKey) taRef.current?.focus();
  }, [hasApiKey]);

  const submit = () => {
    const t = text.trim();
    if (!t || disabled) return;
    setText('');
    onSend(t);
  };

  return (
    <div
      className="dotgrid"
      style={{
        position: 'absolute',
        inset: 0,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        // Extra 54px on the bottom (= top-bar height) so the visual center of
        // the hero aligns with the viewport center, not the center of <main>.
        padding: '40px 24px 94px',
        boxSizing: 'border-box',
        overflow: 'auto',
      }}
    >
      <div style={{ width: '100%', maxWidth: 640, display: 'flex', flexDirection: 'column', gap: 18 }}>
        <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>ensemble</span>
        <h1
          className="serif"
          style={{
            margin: 0,
            fontSize: 40,
            fontStyle: 'italic',
            fontWeight: 400,
            letterSpacing: '-0.01em',
            color: 'var(--ink)',
            lineHeight: 1.15,
          }}
        >
          {hasApiKey ? 'describe what you want to achieve.' : 'set up your keys to begin.'}
        </h1>
        <p
          className="serif"
          style={{
            margin: 0,
            fontStyle: 'italic',
            fontSize: 15,
            color: 'var(--ink-3)',
            lineHeight: 1.55,
          }}
        >
          {hasApiKey
            ? 'the orchestrator agent will dynamically assemble a team of specialized agents to solve your query — refine and execute when ready.'
            : 'the application runs on your own llm key — any openai-compatible endpoint works (openrouter, openai, a self-hosted gateway, etc.). add it once, then describe a problem and ensemble will assemble a team of specialized agents to help you.'}
        </p>

        {hasApiKey ? (
          <div
            style={{
              marginTop: 8,
              background: 'var(--paper)',
              border: '1px solid var(--rule)',
              borderRadius: 6,
              padding: '14px 16px 12px',
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
              boxShadow: '0 1px 0 rgba(26, 23, 20, 0.04), 0 12px 32px -16px rgba(26, 23, 20, 0.18)',
            }}
          >
            <textarea
              ref={taRef}
              rows={3}
              className="field"
              placeholder="e.g. take a company name, search recent news, and produce a sentiment-labeled briefing"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  submit();
                }
              }}
              style={{
                resize: 'none',
                fontFamily: 'var(--serif)',
                fontStyle: 'italic',
                fontSize: 16,
                lineHeight: 1.5,
                background: 'transparent',
                border: 0,
                outline: 'none',
                padding: 0,
                color: 'var(--ink)',
              }}
              disabled={disabled}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span
                className="serif"
                style={{ fontStyle: 'italic', fontSize: 11.5, color: 'var(--ink-4)' }}
              >
                ⌘ + enter to send
              </span>
              <span style={{ flex: 1 }} />
              <button
                className="btn-ink"
                onClick={submit}
                disabled={disabled || !text.trim()}
              >
                ask ensemble <span className="italic-em">→</span>
              </button>
            </div>
          </div>
        ) : (
          <div
            style={{
              marginTop: 8,
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
              background: 'var(--paper)',
              border: '1px solid var(--rule)',
              borderRadius: 6,
              padding: '18px 20px',
            }}
          >
            <div style={{ fontSize: 13, color: 'var(--ink-3)', lineHeight: 1.6 }}>
              you'll need an{' '}
              <span className="mono" style={{ fontSize: 12 }}>llm</span> api key for any
              openai-compatible endpoint (openrouter by default) — keys are stored in your
              browser only.
            </div>
            <div>
              <button className="btn-ink" onClick={onOpenSettings}>
                open settings <span className="italic-em">→</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
