/**
 * Frontend client for the backend ``/api/mcp`` endpoints.
 *
 * Two concerns:
 *   - **Status probing** (`probeStatus` / `probeServer`): POST the current MCP
 *     config JSON and get back per-server reachability + tool counts. Driven by
 *     the Settings panel opening and the per-card "test" button — never on a
 *     timer, since local servers spawn child processes on each probe.
 *   - **OAuth login** (`startMcpLogin` / `pollMcpLogin` / ...): mirrors the LLM
 *     provider flow in `auth.ts`, keyed by server name. The popup + poll shape
 *     is identical; only the endpoints differ.
 */

export type McpServerStatus =
  | 'connected'
  | 'needs_auth'
  | 'failed'
  | 'disabled'
  | 'untested';

export interface McpServerProbe {
  status: McpServerStatus;
  tool_count?: number;
  error?: string;
}

export type McpProbeResult = Record<string, McpServerProbe>;

export type McpLoginStatus = 'signed_in' | 'signed_out' | 'pending' | 'error';

export interface McpLoginStatusResponse {
  status: McpLoginStatus;
  error?: string | null;
}

const STATUS_POLL_INTERVAL_MS = 1500;
const STATUS_POLL_TIMEOUT_MS = 5 * 60 * 1000;

/** Probe every server in the config. ``configJson`` is the raw mcp_servers
 * string (same shape the backend expects in the X-Mcp-Servers header). */
export async function probeStatus(configJson: string): Promise<McpProbeResult> {
  const res = await fetch('/api/mcp/status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mcp_servers: configJson }),
  });
  if (!res.ok) throw new Error(`mcp status failed (${res.status})`);
  const body = await res.json();
  return (body.servers ?? {}) as McpProbeResult;
}

/** Probe only the *remote* servers in a config. Local servers spawn a child
 * process per probe, so unsolicited checks (app startup, intervals) must skip
 * them; remote probes are one HTTP request each. Returns {} when the config
 * has no enabled remote servers. */
export async function probeRemoteStatus(configJson: string): Promise<McpProbeResult> {
  if (!configJson.trim()) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(configJson);
  } catch {
    return {};
  }
  const map =
    parsed && typeof parsed === 'object' && 'mcp' in (parsed as object)
      ? (parsed as { mcp: unknown }).mcp
      : parsed;
  if (!map || typeof map !== 'object') return {};
  const remote: Record<string, unknown> = {};
  for (const [name, cfg] of Object.entries(map as Record<string, unknown>)) {
    if (!cfg || typeof cfg !== 'object') continue;
    const entry = cfg as { type?: unknown; enabled?: unknown };
    if (entry.type === 'remote' && entry.enabled !== false) remote[name] = cfg;
  }
  if (Object.keys(remote).length === 0) return {};
  return probeStatus(JSON.stringify(remote));
}

export interface McpToolInfo {
  server: string;
  server_attr: string;
  tool: string;
  tool_attr: string;
  qualified: string;
  description: string;
  input_schema: Record<string, unknown>;
}

/** List every tool the configured MCP servers expose. Backs the
 * Settings "view tools" popout. */
export async function listMcpTools(
  configJson: string,
  server?: string,
): Promise<McpToolInfo[]> {
  const res = await fetch('/api/mcp/tools', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mcp_servers: configJson, server: server ?? null }),
  });
  if (!res.ok) throw new Error(`mcp tools failed (${res.status})`);
  const body = await res.json();
  return (body.tools ?? []) as McpToolInfo[];
}

/** Probe a single server (the per-card "test" button). */
export async function probeServer(
  name: string,
  configJson: string,
): Promise<McpServerProbe> {
  const res = await fetch('/api/mcp/status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mcp_servers: configJson, server: name }),
  });
  if (!res.ok) throw new Error(`mcp status failed (${res.status})`);
  const body = await res.json();
  return (body.servers?.[name] ?? { status: 'failed', error: 'no result' }) as McpServerProbe;
}

export async function startMcpLogin(
  name: string,
  url: string,
  oauth?: Record<string, unknown> | null,
): Promise<{ authorizeUrl: string; status: string }> {
  const res = await fetch(`/api/mcp/${encodeURIComponent(name)}/login/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, oauth: oauth ?? null }),
  });
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON body */
    }
    throw new Error(detail || `start failed (${res.status})`);
  }
  const body = await res.json();
  return { authorizeUrl: body.authorize_url, status: body.status };
}

export async function mcpLoginStatus(name: string): Promise<McpLoginStatusResponse> {
  const res = await fetch(`/api/mcp/${encodeURIComponent(name)}/login/status`);
  if (!res.ok) throw new Error(`status failed (${res.status})`);
  return res.json();
}

export async function mcpLogout(name: string): Promise<void> {
  await fetch(`/api/mcp/${encodeURIComponent(name)}/logout`, { method: 'POST' });
}

export async function cancelMcpLogin(name: string): Promise<void> {
  try {
    await fetch(`/api/mcp/${encodeURIComponent(name)}/login/cancel`, { method: 'POST' });
  } catch {
    /* best-effort */
  }
}

/** Poll login status until signed_in / error / timeout. Caller opens the popup. */
export async function pollMcpLogin(
  name: string,
  signal?: AbortSignal,
): Promise<McpLoginStatusResponse> {
  const started = Date.now();
  while (true) {
    if (signal?.aborted) throw new Error('cancelled');
    const status = await mcpLoginStatus(name).catch(
      (e): McpLoginStatusResponse => ({ status: 'error', error: String(e) }),
    );
    if (status.status === 'signed_in') return status;
    if (status.status === 'error') return status;
    if (Date.now() - started > STATUS_POLL_TIMEOUT_MS) {
      return { status: 'error', error: 'login timed out' };
    }
    await new Promise((r) => setTimeout(r, STATUS_POLL_INTERVAL_MS));
  }
}
