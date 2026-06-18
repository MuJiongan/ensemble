import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { CloseButton } from './CloseButton';
import { HeroComposer } from './HeroComposer';

type Mode = 'import' | 'export';

interface Props {
  mode: Mode;
  value: string;
  onChange?: (value: string) => void;
  onConfirm?: () => void;
  onClose: () => void;
}

/** Centered dialog for project import/export — same shell as other modals,
 * with the hero landing-page input box inside. */
export function ProjectTransferPanel({
  mode,
  value,
  onChange,
  onConfirm,
  onClose,
}: Props) {
  const [copied, setCopied] = useState(false);
  const isImport = mode === 'import';

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

  const handleExportCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

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
        zIndex: 1200,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-transfer-title"
        onClick={(e) => e.stopPropagation()}
        className="shadow-card"
        style={{
          width: '100%',
          maxWidth: 640,
          maxHeight: '90vh',
          background: 'var(--paper)',
          border: '1px solid var(--rule)',
          borderRadius: 4,
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <div style={{ padding: '18px 20px 20px', overflow: 'auto', minHeight: 0 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 12,
            }}
          >
            <div
              id="project-transfer-title"
              className="smallcaps"
              style={{ color: 'var(--ink-3)' }}
            >
              {isImport ? 'import project' : 'export project'}
            </div>
            <CloseButton onClick={onClose} title="close dialog" />
          </div>
          <p
            className="serif"
            style={{
              fontStyle: 'italic',
              fontSize: 15,
              lineHeight: 1.55,
              color: 'var(--ink)',
              margin: '10px 0 14px',
            }}
          >
            {isImport
              ? 'paste the exported project JSON below.'
              : 'copy this JSON to share or import elsewhere.'}
          </p>
          <HeroComposer
            value={value}
            onChange={isImport ? onChange : undefined}
            readOnly={!isImport}
            autoFocus={isImport}
            minRows={isImport ? 3 : 12}
            placeholder={isImport ? '{"version":1,"name":"my project",...}' : undefined}
            footerHint={isImport ? undefined : (copied ? 'copied' : undefined)}
            actionLabel={isImport ? 'import' : (copied ? 'copied' : 'copy')}
            onAction={isImport ? (onConfirm ?? (() => {})) : handleExportCopy}
            actionDisabled={!value.trim()}
          />
        </div>
      </div>
    </div>,
    document.body,
  );
}