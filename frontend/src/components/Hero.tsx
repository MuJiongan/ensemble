import { useState } from 'react';
import { AttachmentChips, type PendingAttachment } from './ImageAttachments';
import { HeroComposer } from './HeroComposer';

export function Hero({
  hasApiKey,
  disabled,
  onSend,
  onImport,
  onOpenSettings,
  pendingAttachments,
  onRemoveAttachment,
  draggingFile,
  attachmentNotice,
}: {
  hasApiKey: boolean;
  disabled: boolean;
  onSend: (text: string) => void;
  onImport: () => void;
  onOpenSettings: () => void;
  pendingAttachments?: PendingAttachment[];
  onRemoveAttachment?: (id: string) => void;
  draggingFile?: boolean;
  attachmentNotice?: string | null;
}) {
  const [text, setText] = useState('');
  const attachments = pendingAttachments ?? [];

  const submit = () => {
    const t = text.trim();
    if ((!t && attachments.length === 0) || disabled) return;
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
        overflowY: 'auto',
        overflowX: 'hidden',
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
          <>
            <div
              style={{
                marginTop: 8,
                ...(draggingFile
                  ? { outline: '1.5px dashed var(--accent-ink)', outlineOffset: 2, borderRadius: 4 }
                  : {}),
              }}
            >
              {attachmentNotice && (
                <div
                  className="serif"
                  style={{
                    fontStyle: 'italic',
                    fontSize: 12,
                    color: 'var(--ink-4)',
                    marginBottom: 8,
                  }}
                >
                  {attachmentNotice}
                </div>
              )}
              {onRemoveAttachment && (
                <div style={{ marginBottom: attachments.length > 0 ? 8 : 0 }}>
                  <AttachmentChips attachments={attachments} onRemove={onRemoveAttachment} />
                </div>
              )}
              <div
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    submit();
                  }
                }}
              >
                <HeroComposer
                  value={text}
                  onChange={setText}
                  autoFocus={hasApiKey}
                  disabled={disabled}
                  maxComposerHeight="min(40vh, 320px)"
                  placeholder="e.g. take a company name, search recent news, and produce a sentiment-labeled briefing"
                  footerHint="⌘ + enter to send"
                  actionLabel="ask ensemble"
                  onAction={submit}
                  actionDisabled={disabled || (!text.trim() && attachments.length === 0)}
                />
              </div>
            </div>
            <p className="serif hero-import-prompt">
              or{' '}
              <button
                type="button"
                className="hero-import-link hero-import-link--prominent"
                onClick={onImport}
              >
                import a project
              </button>
            </p>
          </>
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
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>
              <button className="btn-ink" onClick={onOpenSettings}>
                open settings <span className="italic-em">→</span>
              </button>
              <button type="button" className="hero-import-link" onClick={onImport}>
                import project
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
