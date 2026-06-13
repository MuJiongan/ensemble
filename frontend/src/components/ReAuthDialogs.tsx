/**
 * Re-authentication prompts, surfaced proactively (not from inside Settings):
 *
 *   - McpAuthDialog — a run reported MCP servers that couldn't authenticate.
 *     A small modal (same shell as ConfirmDialog) since it interrupts a run
 *     the user is watching.
 *   - LlmAuthToast — an OAuth LLM provider's stored session went stale.
 *     A corner notification, not a modal: nothing is mid-flight, so it
 *     shouldn't take over the screen.
 *
 * Neither runs an OAuth flow itself — both just route the user to Settings,
 * which owns sign-in (per-server MCP login controls, provider connect
 * dialogs) and shows the same warnings inline.
 */
import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { CloseButton } from './CloseButton';

function OpenSettingsButton({ onClick }: { onClick: () => void }) {
  return (
    <button className="ed-btn ed-btn--primary" type="button" onClick={onClick}>
      open settings
      <span className="ed-btn__mark">→</span>
    </button>
  );
}

/** Modal shown when a run reports that MCP servers need authentication.
 * Same shell as ConfirmDialog/AlertDialog. */
export function McpAuthDialog({
  servers,
  onOpenSettings,
  onClose,
}: {
  servers: string[];
  onOpenSettings: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return createPortal(
    <div
      onClick={onClose}
      className="fade-in"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'var(--overlay)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '8vh 24px',
        zIndex: 1100,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        className="shadow-card"
        style={{
          width: '100%',
          maxWidth: 420,
          background: 'var(--paper)',
          border: '1px solid var(--rule)',
          borderRadius: 4,
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '18px 20px 16px' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 12,
            }}
          >
            <div className="smallcaps" style={{ color: 'var(--ink-3)' }}>
              mcp sign-in required
            </div>
            <CloseButton onClick={onClose} title="dismiss" />
          </div>
          <p
            className="serif"
            style={{
              fontStyle: 'italic',
              fontSize: 14,
              lineHeight: 1.55,
              color: 'var(--ink)',
              margin: '10px 0 0',
            }}
          >
            <span className="mono" style={{ fontStyle: 'normal', fontSize: 12.5 }}>
              {servers.join(', ')}
            </span>{' '}
            couldn't authenticate, so this run went without{' '}
            {servers.length === 1 ? 'its' : 'their'} tools. sign in from settings, then re-run.
          </p>
        </div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            gap: 10,
            padding: '12px 20px 16px',
            borderTop: '1px solid var(--rule-2)',
          }}
        >
          <button className="text-btn" type="button" onClick={onClose}>
            dismiss
          </button>
          <OpenSettingsButton onClick={onOpenSettings} />
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** Corner notification shown when an OAuth LLM provider's stored session is
 * no longer usable. Non-blocking — the user may not need the provider right
 * now. */
export function LlmAuthToast({
  providers,
  onOpenSettings,
  onClose,
}: {
  providers: string[];
  onOpenSettings: () => void;
  onClose: () => void;
}) {
  return createPortal(
    <div
      className="fade-in shadow-card"
      style={{
        position: 'fixed',
        right: 16,
        bottom: 16,
        width: 320,
        background: 'var(--paper)',
        border: '1px solid var(--rule)',
        borderRadius: 4,
        padding: '12px 14px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        zIndex: 950,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <span className="smallcaps" style={{ color: 'var(--ink-3)' }}>
          session expired
        </span>
        <span style={{ flex: 1 }} />
        <button className="text-btn" type="button" onClick={onClose}>
          dismiss
        </button>
      </div>
      <p
        className="serif"
        style={{
          fontStyle: 'italic',
          fontSize: 12.5,
          lineHeight: 1.5,
          color: 'var(--ink-3)',
          margin: 0,
        }}
      >
        your{' '}
        <span className="mono" style={{ fontStyle: 'normal', fontSize: 11.5 }}>
          {providers.join(', ')}
        </span>{' '}
        sign-in expired — model calls will fail until you sign in again.
      </p>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <OpenSettingsButton onClick={onOpenSettings} />
      </div>
    </div>,
    document.body,
  );
}
