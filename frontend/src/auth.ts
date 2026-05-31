/**
 * Frontend client for the backend ``/api/auth/{provider}`` endpoints.
 *
 * The login flow is:
 *   1. POST ``/api/auth/{id}/start`` → server starts a loopback OAuth callback
 *      server and returns an ``authorize_url``.
 *   2. UI opens the URL in a popup window.
 *   3. UI polls ``GET /api/auth/{id}/status`` every ~1.5s; when the server
 *      reports ``signed_in``, the popup has redirected to localhost and the
 *      backend has stored the tokens.
 *   4. UI updates the cached "signed in" flag in localStorage so other
 *      components (e.g. the Hero gate) can render correctly without a round
 *      trip.
 *
 * ``logout`` clears the server-side credential and the cached flag.
 */
import { SETTINGS_CHANGED_EVENT } from './localSettings';

const STATUS_POLL_INTERVAL_MS = 1500;
const STATUS_POLL_TIMEOUT_MS = 5 * 60 * 1000;

export type AuthStatus = 'signed_in' | 'signed_out' | 'pending' | 'error';

export interface AuthStatusResponse {
  status: AuthStatus;
  label?: string | null;
  error?: string | null;
}

const SIGNED_IN_KEY = 'orchestra:oauth-signed-in';

function readSignedInSet(): Set<string> {
  if (typeof window === 'undefined') return new Set();
  try {
    const raw = window.localStorage.getItem(SIGNED_IN_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : []);
  } catch {
    return new Set();
  }
}

function writeSignedInSet(set: Set<string>): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(SIGNED_IN_KEY, JSON.stringify(Array.from(set)));
  window.dispatchEvent(new CustomEvent(SETTINGS_CHANGED_EVENT));
}

export function isCachedSignedIn(authProviderId: string): boolean {
  return readSignedInSet().has(authProviderId);
}

function markSignedIn(authProviderId: string): void {
  const set = readSignedInSet();
  set.add(authProviderId);
  writeSignedInSet(set);
}

function markSignedOut(authProviderId: string): void {
  const set = readSignedInSet();
  set.delete(authProviderId);
  writeSignedInSet(set);
}

export async function startLogin(
  authProviderId: string,
): Promise<{ authorizeUrl: string; status: string }> {
  const res = await fetch(`/api/auth/${authProviderId}/start`, { method: 'POST' });
  if (!res.ok) {
    // FastAPI returns ``{detail: "..."}`` for HTTPException; surface it so
    // the UI doesn't just say "start failed (500)" for predictable failures
    // like a pinned OAuth callback port already in use.
    let detail = '';
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON body — fall back to the status code */
    }
    throw new Error(detail || `start failed (${res.status})`);
  }
  const body = await res.json();
  return { authorizeUrl: body.authorize_url, status: body.status };
}

export async function fetchStatus(authProviderId: string): Promise<AuthStatusResponse> {
  const res = await fetch(`/api/auth/${authProviderId}/status`);
  if (!res.ok) throw new Error(`status failed (${res.status})`);
  return res.json();
}

export async function logout(authProviderId: string): Promise<void> {
  await fetch(`/api/auth/${authProviderId}/logout`, { method: 'POST' });
  markSignedOut(authProviderId);
}

export async function cancelLogin(authProviderId: string): Promise<void> {
  try {
    await fetch(`/api/auth/${authProviderId}/cancel`, { method: 'POST' });
  } catch {
    /* best-effort */
  }
}

/** Drive the full login flow. Resolves when the server reports signed_in, or
 * rejects on error/timeout. Caller is responsible for opening the popup. */
export async function pollUntilDone(
  authProviderId: string,
  signal?: AbortSignal,
): Promise<AuthStatusResponse> {
  const started = Date.now();
  while (true) {
    if (signal?.aborted) throw new Error('cancelled');
    const status = await fetchStatus(authProviderId).catch(
      (e): AuthStatusResponse => ({ status: 'error', error: String(e) }),
    );
    if (status.status === 'signed_in') {
      markSignedIn(authProviderId);
      return status;
    }
    if (status.status === 'error') {
      markSignedOut(authProviderId);
      return status;
    }
    if (Date.now() - started > STATUS_POLL_TIMEOUT_MS) {
      return { status: 'error', error: 'login timed out' };
    }
    await new Promise((r) => setTimeout(r, STATUS_POLL_INTERVAL_MS));
  }
}
