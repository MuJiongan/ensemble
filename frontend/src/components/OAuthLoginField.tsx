/**
 * Sign-in widget for OAuth-backed provider presets (Codex, xAI subscription).
 *
 * State lives server-side (see backend ``app/auth``). On mount we poll
 * ``/api/auth/{id}/status`` once to determine current state; clicking Sign in
 * triggers ``/start``, opens the authorize URL in a popup, then polls until
 * the backend reports ``signed_in`` (or ``error``).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { OAuthPreset } from '../llmProviders';
import {
  cancelLogin,
  fetchStatus,
  isCachedSignedIn,
  logout as apiLogout,
  pollUntilDone,
  startLogin,
  type AuthStatus,
} from '../auth';

interface Props {
  preset: OAuthPreset;
}

export function OAuthLoginField({ preset }: Props) {
  const [status, setStatus] = useState<AuthStatus>(
    isCachedSignedIn(preset.authProviderId) ? 'signed_in' : 'signed_out',
  );
  const [label, setLabel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const popupRef = useRef<Window | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // On mount + when the preset changes, refresh the real server-side status.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await fetchStatus(preset.authProviderId);
        if (cancelled) return;
        setStatus(s.status);
        setLabel(s.label ?? null);
        setError(s.error ?? null);
      } catch {
        /* leave cached state intact */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [preset.authProviderId]);

  const onSignIn = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const { authorizeUrl } = await startLogin(preset.authProviderId);
      if (authorizeUrl) {
        popupRef.current = window.open(authorizeUrl, '_blank', 'noopener,noreferrer');
      }
      setStatus('pending');
      abortRef.current = new AbortController();
      const result = await pollUntilDone(preset.authProviderId, abortRef.current.signal);
      setStatus(result.status);
      setLabel(result.label ?? null);
      setError(result.error ?? null);
    } catch (e) {
      setStatus('error');
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      abortRef.current = null;
      // Best-effort: close the popup if it's still around.
      try {
        popupRef.current?.close();
      } catch {
        /* cross-origin or already closed */
      }
      popupRef.current = null;
    }
  }, [preset.authProviderId]);

  const onCancel = useCallback(async () => {
    abortRef.current?.abort();
    await cancelLogin(preset.authProviderId);
    setStatus('signed_out');
    setError(null);
    setBusy(false);
  }, [preset.authProviderId]);

  const onSignOut = useCallback(async () => {
    setBusy(true);
    try {
      await apiLogout(preset.authProviderId);
      setStatus('signed_out');
      setLabel(null);
      setError(null);
    } finally {
      setBusy(false);
    }
  }, [preset.authProviderId]);

  return (
    <div>
      <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
        {preset.label.toLowerCase()} account
      </label>
      <div
        style={{
          borderBottom: '1px solid var(--rule)',
          padding: '4px 0 10px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          {status === 'signed_in' && (
            <div
              className="serif"
              style={{ fontSize: 13.5, color: 'var(--ink)' }}
            >
              signed in
              {label && (
                <>
                  {' as '}
                  <span className="mono" style={{ fontSize: 12 }}>{label}</span>
                </>
              )}
              .
            </div>
          )}
          {status === 'pending' && (
            <div
              className="serif"
              style={{ fontStyle: 'italic', fontSize: 13, color: 'var(--ink-3)' }}
            >
              waiting for browser authorization… complete sign-in in the popup window.
            </div>
          )}
          {status === 'error' && (
            <div
              className="serif"
              style={{ fontStyle: 'italic', fontSize: 13, color: 'var(--state-err, #b04030)' }}
            >
              {error || 'sign-in failed.'} try again.
            </div>
          )}
          {status === 'signed_out' && (
            <div
              className="serif"
              style={{ fontStyle: 'italic', fontSize: 13, color: 'var(--ink-3)' }}
            >
              not signed in.
            </div>
          )}
        </div>
        {status === 'signed_in' ? (
          <button className="btn-ink" onClick={onSignOut} disabled={busy}>
            sign out
          </button>
        ) : status === 'pending' ? (
          <button className="btn-ink" onClick={onCancel}>
            cancel
          </button>
        ) : (
          <button className="btn-ink" onClick={onSignIn} disabled={busy}>
            sign in <span className="italic-em">→</span>
          </button>
        )}
      </div>
      <div
        className="serif"
        style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--ink-4)', marginTop: 6, lineHeight: 1.5 }}
      >
        {preset.description}
      </div>
    </div>
  );
}
