import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { CloseButton } from './CloseButton';

type DialogShellProps = {
  title: string;
  message: string;
  onDismiss: () => void;
  actions: React.ReactNode;
};

function DialogShell({ title, message, onDismiss, actions }: DialogShellProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onDismiss();
    };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onDismiss]);

  return createPortal(
    <div
      onClick={onDismiss}
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
        aria-labelledby="dialog-title"
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
            <div id="dialog-title" className="smallcaps" style={{ color: 'var(--ink-3)' }}>
              {title}
            </div>
            <CloseButton onClick={onDismiss} title="close dialog" />
          </div>
          <p
            className="serif"
            style={{
              fontStyle: 'italic',
              fontSize: 15,
              lineHeight: 1.55,
              color: 'var(--ink)',
              margin: '10px 0 0',
            }}
          >
            {message}
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
          {actions}
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function ConfirmDialog({
  title = 'confirm',
  message,
  confirmLabel = 'continue',
  cancelLabel = 'cancel',
  variant = 'default',
  onConfirm,
  onCancel,
}: {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'default' | 'danger';
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    confirmRef.current?.focus();
  }, []);

  return (
    <DialogShell title={title} message={message} onDismiss={onCancel} actions={
      <>
        <button className="text-btn" onClick={onCancel}>
          {cancelLabel}
        </button>
        <button
          ref={confirmRef}
          className={variant === 'danger' ? 'ed-btn ed-btn--danger' : 'ed-btn ed-btn--primary'}
          onClick={onConfirm}
        >
          {confirmLabel}
          {variant === 'danger' ? (
            <span className="ed-btn__mark">×</span>
          ) : (
            <span className="ed-btn__mark">→</span>
          )}
        </button>
      </>
    } />
  );
}

export function AlertDialog({
  title = 'notice',
  message,
  okLabel = 'ok',
  variant = 'default',
  onClose,
}: {
  title?: string;
  message: string;
  okLabel?: string;
  variant?: 'default' | 'error';
  onClose: () => void;
}) {
  const okRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    okRef.current?.focus();
  }, []);

  return (
    <DialogShell title={title} message={message} onDismiss={onClose} actions={
      <button
        ref={okRef}
        className={variant === 'error' ? 'ed-btn ed-btn--danger' : 'ed-btn ed-btn--primary'}
        onClick={onClose}
      >
        {okLabel}
        <span className="ed-btn__mark">→</span>
      </button>
    } />
  );
}
