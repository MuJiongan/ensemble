/**
 * Settings live in browser localStorage and are sent to the backend as
 * headers on every request. Source of truth is the browser, so the keys
 * never get persisted server-side.
 */
import type { Settings } from './types';
import {
  isOAuthPreset,
  presetById,
  presetIdForUrl,
  type ProviderPreset,
} from './llmProviders';

const KEY = 'orchestra:settings';
export const SETTINGS_CHANGED_EVENT = 'orchestra:settings-changed';

const EMPTY: Settings = {
  llm_api_keys: {},
  llm_provider_preset_id: '',
  llm_base_url: '',
  parallel_api_key: '',
  default_orchestrator_models: {},
  default_node_models: {},
  mcp_servers: '',
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

function asStringMap(v: unknown): Record<string, string> {
  if (!v || typeof v !== 'object') return {};
  const out: Record<string, string> = {};
  for (const [k, val] of Object.entries(v)) {
    if (typeof val === 'string') out[k] = val;
  }
  return out;
}

export function loadSettings(): Settings {
  if (typeof window === 'undefined') return { ...EMPTY, llm_api_keys: {} };
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { ...EMPTY, llm_api_keys: {} };
    const parsed = JSON.parse(raw);
    let keys: Record<string, string> = {};
    if (parsed.llm_api_keys && typeof parsed.llm_api_keys === 'object') {
      for (const [k, v] of Object.entries(parsed.llm_api_keys)) {
        if (typeof v === 'string') keys[k] = v;
      }
    }
    // One-time migration: an older single `llm_api_key` string is mapped onto
    // whatever provider the saved base URL points at, so users don't lose the
    // key they already entered.
    const legacy = typeof parsed.llm_api_key === 'string' ? parsed.llm_api_key : '';
    const base = parsed.llm_base_url ?? '';
    if (legacy && !keys[presetIdForUrl(base)]) {
      keys = { ...keys, [presetIdForUrl(base)]: legacy };
    }
    // Migrate legacy single-value default_orchestrator_model /
    // default_node_model into per-provider maps, attributed to whichever
    // provider's URL was saved at the time.
    const orchestratorModels = asStringMap(parsed.default_orchestrator_models);
    const nodeModels = asStringMap(parsed.default_node_models);
    const inferredPresetId =
      (typeof parsed.llm_provider_preset_id === 'string' && parsed.llm_provider_preset_id) ||
      presetIdForUrl(base);
    if (
      typeof parsed.default_orchestrator_model === 'string' &&
      parsed.default_orchestrator_model &&
      !orchestratorModels[inferredPresetId]
    ) {
      orchestratorModels[inferredPresetId] = parsed.default_orchestrator_model;
    }
    if (
      typeof parsed.default_node_model === 'string' &&
      parsed.default_node_model &&
      !nodeModels[inferredPresetId]
    ) {
      nodeModels[inferredPresetId] = parsed.default_node_model;
    }
    return {
      llm_api_keys: keys,
      llm_provider_preset_id:
        typeof parsed.llm_provider_preset_id === 'string' ? parsed.llm_provider_preset_id : '',
      llm_base_url: base,
      parallel_api_key: parsed.parallel_api_key ?? '',
      default_orchestrator_models: orchestratorModels,
      default_node_models: nodeModels,
      mcp_servers: typeof parsed.mcp_servers === 'string' ? parsed.mcp_servers : '',
    };
  } catch {
    return { ...EMPTY, llm_api_keys: {} };
  }
}

/** Effective preset id: explicit field if set, otherwise inferred from URL.
 * Treats empty/missing as the OpenRouter default. */
export function activePresetId(s: Settings): string {
  if (s.llm_provider_preset_id) return s.llm_provider_preset_id;
  return presetIdForUrl(s.llm_base_url);
}

export function activePreset(s: Settings): ProviderPreset | undefined {
  return presetById(activePresetId(s));
}

/** API key for the currently active api-key preset (empty for OAuth or unset). */
export function activeLlmApiKey(s: Settings): string {
  const p = activePreset(s);
  if (isOAuthPreset(p)) return '';
  return s.llm_api_keys[activePresetId(s)] ?? '';
}

/** Saved default orchestrator model id for the active preset (empty if none). */
export function activeOrchestratorModel(s: Settings): string {
  return s.default_orchestrator_models[activePresetId(s)] ?? '';
}

/** Saved default node model id for the active preset (empty if none). */
export function activeNodeModel(s: Settings): string {
  return s.default_node_models[activePresetId(s)] ?? '';
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

/** Headers to include on every request that may end up calling an LLM. */
export function settingsHeaders(): Record<string, string> {
  const s = loadSettings();
  const h: Record<string, string> = {};
  const preset = activePreset(s);
  // Always send X-Llm-Provider-Id on LLM-bound requests so the backend has
  // an authoritative signal: a non-empty value selects an OAuth provider,
  // and the empty string explicitly resets back to the API-key path. (Other
  // requests — auth-status polls — omit this header entirely so they don't
  // race with a streaming orchestrator turn.)
  if (isOAuthPreset(preset)) {
    h['X-Llm-Provider-Id'] = preset.authProviderId;
  } else {
    h['X-Llm-Provider-Id'] = '';
    const apiKey = activeLlmApiKey(s);
    if (apiKey) h['X-Llm-Api-Key'] = apiKey;
    if (s.llm_base_url) h['X-Llm-Base-Url'] = s.llm_base_url;
  }
  if (s.parallel_api_key) h['X-Parallel-Key'] = s.parallel_api_key;
  // MCP config travels as a single JSON header. Always send it (even empty) so
  // clearing all servers actually clears the backend env. Minify to one line —
  // header values can't contain raw newlines — and send empty if it isn't
  // valid JSON (the backend treats that as "no servers" anyway).
  h['X-Mcp-Servers'] = minifyJson(s.mcp_servers);
  const orch = activeOrchestratorModel(s);
  if (orch) h['X-Orchestrator-Model'] = orch;
  const node = activeNodeModel(s);
  if (node) h['X-Node-Model'] = node;
  return h;
}
