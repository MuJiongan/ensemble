import type {
  Workflow, WorkflowDetail, WorkflowExport, WFNode, WFEdge, Run, IOPort, NodeConfig,
  OrchestratorSession, ChatHistory, OrchestratorEvent, FsFile,
} from './types';
import { settingsHeaders, type LlmTarget } from './localSettings';

/** Request failure carrying the HTTP status, so callers can branch on it
 * (404 = the resource is gone, vs. transient/network-ish failures). */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  target: LlmTarget = 'base',
): Promise<T> {
  const headers: Record<string, string> = { ...settingsHeaders(target) };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text();
    throw new ApiError(`${method} ${path} → ${r.status}: ${text}`, r.status);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

export interface NewNodePayload {
  name: string;
  description?: string;
  code?: string;
  inputs?: IOPort[];
  outputs?: IOPort[];
  config?: NodeConfig;
  position?: { x: number; y: number };
}

export interface PatchNodePayload {
  name?: string;
  description?: string;
  code?: string;
  inputs?: IOPort[];
  outputs?: IOPort[];
  config?: NodeConfig;
  position?: { x: number; y: number };
}

export const api = {
  listWorkflows: () => request<Workflow[]>('GET', '/api/workflows'),
  createWorkflow: (name: string) => request<Workflow>('POST', '/api/workflows', { name }),
  exportWorkflow: (id: string) =>
    request<WorkflowExport>('GET', `/api/workflows/${id}/export`),
  importWorkflow: (body: WorkflowExport) =>
    request<Workflow>('POST', '/api/workflows/import', body),
  getWorkflow: (id: string) => request<WorkflowDetail>('GET', `/api/workflows/${id}`),
  patchWorkflow: (id: string, body: Partial<Pick<Workflow, 'name' | 'input_node_id' | 'output_node_id'>>) =>
    request<Workflow>('PATCH', `/api/workflows/${id}`, body),
  deleteWorkflow: (id: string) => request<{ ok: true }>('DELETE', `/api/workflows/${id}`),

  createNode: (wid: string, body: NewNodePayload) =>
    request<WFNode>('POST', `/api/workflows/${wid}/nodes`, body),
  patchNode: (id: string, body: PatchNodePayload) =>
    request<WFNode>('PATCH', `/api/nodes/${id}`, body),
  deleteNode: (id: string) => request<{ ok: true }>('DELETE', `/api/nodes/${id}`),

  createEdge: (wid: string, body: Omit<WFEdge, 'id' | 'workflow_id'>) =>
    request<WFEdge>('POST', `/api/workflows/${wid}/edges`, body),
  deleteEdge: (id: string) => request<{ ok: true }>('DELETE', `/api/edges/${id}`),

  startRun: (wid: string, inputs: Record<string, unknown>) =>
    request<Run>('POST', `/api/workflows/${wid}/runs`, { inputs, kind: 'user' }, 'node'),
  rerunFromSnapshot: (rid: string, inputs: Record<string, unknown>) =>
    request<Run>('POST', `/api/runs/${rid}/rerun`, { inputs, kind: 'user' }, 'node'),
  cancelRun: (rid: string) =>
    request<{ cancelled: boolean }>('POST', `/api/runs/${rid}/cancel`),
  deleteRun: (rid: string) => request<{ ok: true }>('DELETE', `/api/runs/${rid}`),
  getRun: (rid: string) => request<Run>('GET', `/api/runs/${rid}`),
  listRuns: (wid: string) => request<Run[]>('GET', `/api/workflows/${wid}/runs`),
  runEventsUrl: (rid: string) => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/api/runs/${rid}/events`;
  },

  // --- file viewer ---------------------------------------------------------
  readFile: (path: string) =>
    request<FsFile>('GET', `/api/files?path=${encodeURIComponent(path)}`),
  /** Open the path in the OS default app, or reveal it in the file manager. */
  openFileExternally: (path: string, reveal = false) =>
    request<{ ok: true }>('POST', '/api/files/open', { path, reveal }),

  // --- orchestrator sessions ----------------------------------------------
  createSession: (wid: string) =>
    request<OrchestratorSession>('POST', `/api/workflows/${wid}/sessions`),
  listSessions: (wid: string) =>
    request<OrchestratorSession[]>('GET', `/api/workflows/${wid}/sessions`),
  getSessionMessages: (sid: string) =>
    request<ChatHistory>('GET', `/api/sessions/${sid}/messages`),
  clearSessionMessages: (sid: string) =>
    request<{ ok: true }>('DELETE', `/api/sessions/${sid}/messages`),
  cancelOrchestratorTurn: (sid: string) =>
    request<{ cancelled: boolean }>('POST', `/api/sessions/${sid}/cancel`),

  /**
   * Send a user message to the orchestrator session and stream back its events.
   * Implemented as fetch → ReadableStream over text/event-stream because
   * EventSource only supports GET.
   */
  streamUserMessage: async (
    sid: string,
    text: string,
    onEvent: (ev: OrchestratorEvent) => void,
    signal?: AbortSignal,
    attachments?: { dataUrl: string; filename: string }[],
  ): Promise<void> => {
    const res = await fetch(`/api/sessions/${sid}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...settingsHeaders('orchestrator') },
      body: JSON.stringify({
        text,
        attachments: (attachments ?? []).map((a) => ({
          data_url: a.dataUrl,
          filename: a.filename,
        })),
      }),
      signal,
    });
    if (!res.ok || !res.body) {
      const body = await res.text().catch(() => '');
      throw new Error(`POST /api/sessions/${sid}/messages → ${res.status}: ${body}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    // SSE frame parser: split on blank line, each frame has lines starting with `data:`.
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const dataLines = frame
          .split('\n')
          .filter((l) => l.startsWith('data:'))
          .map((l) => l.slice(5).replace(/^ /, ''));
        if (dataLines.length === 0) continue;
        const payload = dataLines.join('\n');
        try {
          onEvent(JSON.parse(payload) as OrchestratorEvent);
        } catch {
          // ignore malformed frame
        }
      }
    }
  },
};
