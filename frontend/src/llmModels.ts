/**
 * Fetches the list of model ids for the currently active provider preset.
 *
 *  * api-key preset → ``GET {base}/models`` with ``Authorization: Bearer
 *    {key}``. Standard OpenAI-shaped response.
 *  * OAuth preset   → returns the preset's hardcoded ``availableModels`` list.
 *    Their ``/models`` endpoint either doesn't exist (Codex's
 *    ``chatgpt.com/backend-api/codex/responses``) or requires a server-side
 *    token, neither of which fits a browser fetch.
 *
 * Results are cached in memory + sessionStorage, keyed by **preset id** so
 * that two presets sharing a base URL (e.g. xAI api-key vs xAI sign-in)
 * don't collide.
 */
import { activeLlmApiKey, activePreset } from './localSettings';
import type { Settings } from './types';
import { isOAuthPreset, isApiKeyPreset, type ProviderPreset } from './llmProviders';

const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1';
const SESSION_KEY_PREFIX = 'orchestra:llm-models:';

export interface LLMModel {
  id: string;
  name?: string;
}

const memCache = new Map<string, LLMModel[]>();
const inflight = new Map<string, Promise<LLMModel[]>>();

function sessionKey(presetId: string): string {
  return SESSION_KEY_PREFIX + presetId;
}

function readSessionCache(presetId: string): LLMModel[] | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(sessionKey(presetId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((m) => m && typeof m.id === 'string');
  } catch {
    return null;
  }
}

function writeSessionCache(presetId: string, models: LLMModel[]): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(sessionKey(presetId), JSON.stringify(models));
  } catch {
    /* quota — ignore */
  }
}

/** Snapshot of the parts of Settings ``fetchLlmModels`` needs. Lets the
 * Settings panel pass its in-memory state so dropdown changes take effect
 * before Save is clicked. */
export interface ModelsSnapshot {
  settings: Settings;
}

interface ResolvedSource {
  cacheKey: string;
  /** Async loader for the model list. Bypassed when the cache hits. */
  load: () => Promise<LLMModel[]>;
}

function resolveSource(snapshot: Settings): ResolvedSource {
  const preset: ProviderPreset | undefined = activePreset(snapshot);

  if (isOAuthPreset(preset)) {
    const ids = preset.availableModels;
    return {
      cacheKey: preset.id,
      load: async () => ids.map((id) => ({ id })),
    };
  }

  // API-key path (including ``custom`` and unknown presets).
  const presetId = isApiKeyPreset(preset) ? preset.id : 'custom';
  const base = (snapshot.llm_base_url || DEFAULT_BASE_URL).replace(/\/+$/, '');
  const apiKey = activeLlmApiKey(snapshot);
  return {
    cacheKey: presetId,
    load: async () => {
      const headers: Record<string, string> = { Accept: 'application/json' };
      if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
      const res = await fetch(`${base}/models`, { headers });
      if (!res.ok) throw new Error(`${base}/models ${res.status}`);
      const json = (await res.json()) as { data?: Array<{ id?: string; name?: string }> };
      return (json.data ?? [])
        .filter((m): m is { id: string; name?: string } => typeof m?.id === 'string')
        .map((m) => ({ id: m.id, name: m.name }));
    },
  };
}

export async function fetchLlmModels(snapshot: Settings): Promise<LLMModel[]> {
  const { cacheKey, load } = resolveSource(snapshot);
  const cached = memCache.get(cacheKey);
  if (cached) return cached;
  const session = readSessionCache(cacheKey);
  if (session) {
    memCache.set(cacheKey, session);
    return session;
  }
  const existing = inflight.get(cacheKey);
  if (existing) return existing;

  const promise = (async () => {
    const models = await load();
    memCache.set(cacheKey, models);
    writeSessionCache(cacheKey, models);
    return models;
  })();
  inflight.set(cacheKey, promise);
  try {
    return await promise;
  } finally {
    inflight.delete(cacheKey);
  }
}

/** Cache key for a given settings snapshot — useful as a React effect dep
 * so callers re-fetch when the active provider changes. */
export function modelsCacheKey(snapshot: Settings): string {
  return resolveSource(snapshot).cacheKey;
}
