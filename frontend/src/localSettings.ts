/**
 * Settings live in browser localStorage and are sent to the backend as
 * headers on every request. Source of truth is the browser, so the keys
 * never get persisted server-side.
 *
 * The orchestrator and node each have their own {provider, model, variant}
 * selection. Because the orchestrator stream and a workflow run are *separate*
 * HTTP requests, we send target-specific provider headers per endpoint — so
 * the two targets may even use different providers.
 */
import type { ModelSelection, ProviderConnection, Settings } from './types';

export const SETTINGS_STORAGE_KEY = 'orchestra:settings';
const KEY = SETTINGS_STORAGE_KEY;
export const SETTINGS_CHANGED_EVENT = 'orchestra:settings-changed';

/** Non-empty sentinel for explicitly cleared custom instructions. Some HTTP
 * clients omit headers whose value is `''`, which would leave a stale value in
 * the backend's process env. */
const EMPTY_CUSTOM_INSTRUCTIONS_HEADER = '.';

/** Which LLM target a request is for. `base` sends no LLM provider headers. */
export type LlmTarget = 'orchestrator' | 'node' | 'base';

const EMPTY: Settings = {
  connections: {},
  parallel_api_key: '',
  orchestrator: null,
  node: null,
  mcp_servers: '',
  custom_instructions: '',
};

/** Collapse a JSON string to one line (header values can't hold newlines).
 * Returns '' when the input isn't valid JSON — the backend treats that as
 * "no MCP servers". */
function minifyJson(raw: string): string {
  if (!raw.trim()) return '';
  try {
    return JSON.stringify(JSON.parse(raw));
  } catch {
    return '';
  }
}

function asConnections(v: unknown): Record<string, ProviderConnection> {
  if (!v || typeof v !== 'object') return {};
  const out: Record<string, ProviderConnection> = {};
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    if (val && typeof val === 'object') {
      const c = val as Record<string, unknown>;
      const method = c.method === 'oauth' ? 'oauth' : 'api';
      out[k] = {
        method,
        apiKey: typeof c.apiKey === 'string' ? c.apiKey : undefined,
        baseURL: typeof c.baseURL === 'string' ? c.baseURL : undefined,
      };
    }
  }
  return out;
}

function asSelection(v: unknown): ModelSelection | null {
  if (!v || typeof v !== 'object') return null;
  const c = v as Record<string, unknown>;
  if (typeof c.providerID !== 'string' || typeof c.modelID !== 'string') return null;
  if (!c.providerID || !c.modelID) return null;
  return {
    providerID: c.providerID,
    modelID: c.modelID,
    variant: typeof c.variant === 'string' ? c.variant : null,
  };
}

export function loadSettings(): Settings {
  if (typeof window === 'undefined') return { ...EMPTY, connections: {} };
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { ...EMPTY, connections: {} };
    const parsed = JSON.parse(raw);
    // Migrate the pre-catalog shape (preset-keyed api keys + per-preset model
    // maps) to the new connections + selections shape, once.
    if (parsed.connections === undefined && (parsed.llm_api_keys || parsed.default_orchestrator_models)) {
      const migrated = migrateLegacy(parsed);
      saveSettings(migrated);
      return migrated;
    }
    const custom_instructions =
      'custom_instructions' in parsed && typeof parsed.custom_instructions === 'string'
        ? parsed.custom_instructions
        : typeof parsed.orchestrator_preferences === 'string'
          ? parsed.orchestrator_preferences
          : '';
    const settings: Settings = {
      connections: asConnections(parsed.connections),
      parallel_api_key: typeof parsed.parallel_api_key === 'string' ? parsed.parallel_api_key : '',
      orchestrator: asSelection(parsed.orchestrator),
      node: asSelection(parsed.node),
      mcp_servers: typeof parsed.mcp_servers === 'string' ? parsed.mcp_servers : '',
      custom_instructions,
    };
    // One-time cleanup: drop the pre-rename `orchestrator_preferences` key so
    // a cleared textarea can't resurrect stale text from the legacy field.
    if ('orchestrator_preferences' in parsed) {
      saveSettings(settings);
    }
    return settings;
  } catch {
    return { ...EMPTY, connections: {} };
  }
}

// Maps legacy preset ids → catalog provider id + base url (or oauth marker).
const LEGACY_PRESETS: Record<string, { provider: string; base?: string; oauth?: boolean }> = {
  openrouter: { provider: 'openrouter', base: 'https://openrouter.ai/api/v1' },
  openai: { provider: 'openai', base: 'https://api.openai.com/v1' },
  groq: { provider: 'groq', base: 'https://api.groq.com/openai/v1' },
  together: { provider: 'togetherai', base: 'https://api.together.xyz/v1' },
  deepseek: { provider: 'deepseek', base: 'https://api.deepseek.com/v1' },
  cerebras: { provider: 'cerebras', base: 'https://api.cerebras.ai/v1' },
  fireworks: { provider: 'fireworks-ai', base: 'https://api.fireworks.ai/inference/v1' },
  xai: { provider: 'xai', base: 'https://api.x.ai/v1' },
  mistral: { provider: 'mistral', base: 'https://api.mistral.ai/v1' },
  codex: { provider: 'codex', oauth: true },
  'xai-oauth': { provider: 'xai', oauth: true },
};

function migrateLegacy(parsed: Record<string, unknown>): Settings {
  const connections: Record<string, ProviderConnection> = {};
  const apiKeys = asStringMapLoose(parsed.llm_api_keys);
  const legacyBase = typeof parsed.llm_base_url === 'string' ? parsed.llm_base_url : '';

  let signedIn: string[] = [];
  try {
    const rawSet = window.localStorage.getItem('orchestra:oauth-signed-in');
    signedIn = rawSet ? (JSON.parse(rawSet) as string[]) : [];
  } catch {
    /* ignore */
  }

  for (const [presetId, key] of Object.entries(apiKeys)) {
    if (!key) continue;
    const m = LEGACY_PRESETS[presetId];
    if (m && !m.oauth) connections[m.provider] = { method: 'api', apiKey: key, baseURL: m.base };
    else if (presetId === 'custom') connections['custom'] = { method: 'api', apiKey: key, baseURL: legacyBase };
  }
  for (const authId of signedIn) {
    // authId is the backend provider id (codex / xai).
    connections[authId] = { method: 'oauth' };
  }

  const orchModels = asStringMapLoose(parsed.default_orchestrator_models);
  const nodeModels = asStringMapLoose(parsed.default_node_models);
  const activePreset =
    (typeof parsed.llm_provider_preset_id === 'string' && parsed.llm_provider_preset_id) || 'openrouter';
  const toSel = (models: Record<string, string>): ModelSelection | null => {
    const modelID = models[activePreset];
    if (!modelID) return null;
    const m = LEGACY_PRESETS[activePreset];
    const providerID = m ? m.provider : activePreset;
    return { providerID, modelID, variant: null };
  };

  return {
    connections,
    parallel_api_key: typeof parsed.parallel_api_key === 'string' ? parsed.parallel_api_key : '',
    orchestrator: toSel(orchModels),
    node: toSel(nodeModels),
    mcp_servers: typeof parsed.mcp_servers === 'string' ? parsed.mcp_servers : '',
    custom_instructions: '',
  };
}

function asStringMapLoose(v: unknown): Record<string, string> {
  if (!v || typeof v !== 'object') return {};
  const out: Record<string, string> = {};
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    if (typeof val === 'string') out[k] = val;
  }
  return out;
}

export function saveSettings(s: Settings): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(KEY, JSON.stringify(s));
  window.dispatchEvent(new CustomEvent(SETTINGS_CHANGED_EVENT));
}

export function clearSettings(): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(KEY);
  window.dispatchEvent(new CustomEvent(SETTINGS_CHANGED_EVENT));
}

/** Is a provider connected (has an api key or an oauth marker)? */
export function isConnected(s: Settings, providerID: string): boolean {
  const c = s.connections[providerID];
  if (!c) return false;
  return c.method === 'oauth' || !!c.apiKey;
}

/** The selection for a target, or null. */
export function selectionFor(s: Settings, target: 'orchestrator' | 'node'): ModelSelection | null {
  return target === 'orchestrator' ? s.orchestrator : s.node;
}

// Header names for each target. The orchestrator's own (in-process) calls read
// the X-Llm-* group; runner-spawned node children read the X-Node-* group. Both
// groups ride on every LLM-bound request so the orchestrator can spawn a node
// run that uses the *node's* provider/model — not the orchestrator's.
const ORCH_HEADERS = {
  provider: 'X-Llm-Provider-Id',
  apiKey: 'X-Llm-Api-Key',
  baseURL: 'X-Llm-Base-Url',
  model: 'X-Orchestrator-Model',
  variant: 'X-Orchestrator-Variant',
} as const;
const NODE_HEADERS = {
  provider: 'X-Node-Provider-Id',
  apiKey: 'X-Node-Api-Key',
  baseURL: 'X-Node-Base-Url',
  model: 'X-Node-Model',
  variant: 'X-Node-Variant',
} as const;

function applyTarget(
  h: Record<string, string>,
  s: Settings,
  sel: ModelSelection | null,
  names: typeof ORCH_HEADERS | typeof NODE_HEADERS,
): void {
  // Always send provider/model/variant (possibly empty) so the backend can
  // set-or-clear and never inherits a stale selection.
  h[names.provider] = sel?.providerID ?? '';
  h[names.model] = sel?.modelID ?? '';
  h[names.variant] = sel?.variant ?? '';
  if (!sel) return;
  const conn = s.connections[sel.providerID];
  // OAuth providers resolve their bearer + base url server-side; api providers
  // forward the key + base url.
  if (conn && conn.method === 'api') {
    if (conn.apiKey) h[names.apiKey] = conn.apiKey;
    if (conn.baseURL) h[names.baseURL] = conn.baseURL;
  }
}

/** UTF-8-safe base64 for header transport. Empty in → explicit clear sentinel. */
function encodeHeaderText(value: string): string {
  if (!value) return EMPTY_CUSTOM_INSTRUCTIONS_HEADER;
  const bytes = new TextEncoder().encode(value);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/** Headers to include on every request that may end up calling an LLM. Carries
 * both the orchestrator and node selections (the `target` arg is no longer
 * needed but kept for call-site compatibility). */
export function settingsHeaders(_target: LlmTarget = 'base'): Record<string, string> {
  const s = loadSettings();
  const h: Record<string, string> = {};

  if (s.parallel_api_key) h['X-Parallel-Key'] = s.parallel_api_key;
  // MCP config travels as a single JSON header. Always send it (even empty) so
  // clearing all servers actually clears the backend env.
  h['X-Mcp-Servers'] = minifyJson(s.mcp_servers);
  // Custom instructions: always send (even empty) so clearing them clears
  // the backend env. Base64-encoded because the raw text is multi-line
  // free-form and HTTP header values reject newlines / non-Latin1 chars —
  // sending it raw would throw when the request headers are constructed.
  h['X-Custom-Instructions'] = encodeHeaderText(s.custom_instructions.trim());

  applyTarget(h, s, s.orchestrator, ORCH_HEADERS);
  applyTarget(h, s, s.node, NODE_HEADERS);
  return h;
}
