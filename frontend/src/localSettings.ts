/**
 * Settings live in browser localStorage and are sent to the backend as
 * headers on every request. Source of truth is the browser, so the keys
 * never get persisted server-side.
 */
import type { Settings } from './types';
import { presetIdForUrl } from './llmProviders';

const KEY = 'orchestra:settings';
export const SETTINGS_CHANGED_EVENT = 'orchestra:settings-changed';

const EMPTY: Settings = {
  llm_api_keys: {},
  llm_base_url: '',
  parallel_api_key: '',
  default_orchestrator_model: '',
  default_node_model: '',
};

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
    return {
      llm_api_keys: keys,
      llm_base_url: base,
      parallel_api_key: parsed.parallel_api_key ?? '',
      default_orchestrator_model: parsed.default_orchestrator_model ?? '',
      default_node_model: parsed.default_node_model ?? '',
    };
  } catch {
    return { ...EMPTY, llm_api_keys: {} };
  }
}

/** API key for the provider implied by the current `llm_base_url`. */
export function activeLlmApiKey(s: Settings): string {
  return s.llm_api_keys[presetIdForUrl(s.llm_base_url)] ?? '';
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
  const apiKey = activeLlmApiKey(s);
  if (apiKey) h['X-Llm-Api-Key'] = apiKey;
  if (s.llm_base_url) h['X-Llm-Base-Url'] = s.llm_base_url;
  if (s.parallel_api_key) h['X-Parallel-Key'] = s.parallel_api_key;
  if (s.default_orchestrator_model) h['X-Orchestrator-Model'] = s.default_orchestrator_model;
  if (s.default_node_model) h['X-Node-Model'] = s.default_node_model;
  return h;
}
