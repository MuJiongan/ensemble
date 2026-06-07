// Browser notifications for run completion.
//
// Permission must be requested from a user gesture (button click, message
// send), so callers prime this on the same path that kicks off a run —
// `ensureNotificationPermission()` is a no-op after the first call.

import type { RunStatus } from './types';

let permissionAsked = false;

export function ensureNotificationPermission(): void {
  if (typeof Notification === 'undefined') return;
  if (permissionAsked) return;
  permissionAsked = true;
  if (Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}

export function notifyRunFinished({
  runId,
  workflowName,
  status,
  error,
  outputs,
  durationMs,
}: {
  runId: string;
  workflowName: string;
  status: RunStatus;
  error: string | null;
  outputs: Record<string, unknown> | null;
  durationMs: number;
}): void {
  if (typeof Notification === 'undefined') return;
  if (Notification.permission !== 'granted') return;
  if (status === 'cancelled') return;

  const title = workflowName;
  const body = buildBody({ status, error, outputs, durationMs });

  try {
    new Notification(title, { body, tag: `run-${runId}` });
  } catch {
    /* ignore — some browsers throw when the page lacks user activation */
  }
}

function buildBody({
  status,
  error,
  outputs,
  durationMs,
}: {
  status: RunStatus;
  error: string | null;
  outputs: Record<string, unknown> | null;
  durationMs: number;
}): string {
  if (status === 'success') {
    const hint = briefOutputHint(outputs);
    const timing =
      Number.isFinite(durationMs) && durationMs > 0
        ? `Finished in ${formatDuration(durationMs)}`
        : 'Finished';
    return hint ? `${timing} — ${hint}` : timing;
  }
  if (status === 'error') {
    return error
      ? truncate(error.replace(/\s+/g, ' ').trim(), 100)
      : 'Failed';
  }
  return status;
}

function briefOutputHint(outputs: Record<string, unknown> | null): string {
  if (!outputs) return '';
  const entries = Object.entries(outputs).filter(
    ([, v]) => v !== null && v !== undefined && v !== '',
  );
  if (entries.length === 0) return '';
  if (entries.length === 1) {
    const v = entries[0][1];
    if (typeof v === 'string') {
      const text = v.replace(/\s+/g, ' ').trim();
      return text.length <= 60 ? text : '';
    }
    return '';
  }
  return `${entries.length} outputs`;
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const mins = Math.floor(s / 60);
  const rem = Math.round(s - mins * 60);
  return rem ? `${mins}m${rem}s` : `${mins}m`;
}
