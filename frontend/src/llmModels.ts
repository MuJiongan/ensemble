/**
 * Fetches the list of model ids exposed by the configured OpenAI-compatible
 * provider — `{base_url}/models` with `Authorization: Bearer {key}`. Cached
 * per base URL in memory + sessionStorage so the autocomplete dropdown is
 * instant on subsequent focuses inside the same tab.
 */
import { activeLlmApiKey, loadSettings } from './localSettings';

const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1';
const SESSION_KEY_PREFIX = 'orchestra:llm-models:';

export interface LLMModel {
  id: string;
  name?: string;
}

const memCache = new Map<string, LLMModel[]>();
const inflight = new Map<string, Promise<LLMModel[]>>();

function effectiveBaseUrl(): string {
  const s = loadSettings();
  return (s.llm_base_url || DEFAULT_BASE_URL).replace(/\/+$/, '');
}

function sessionKey(base: string): string {
  return SESSION_KEY_PREFIX + base;
}

function readSessionCache(base: string): LLMModel[] | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(sessionKey(base));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((m) => m && typeof m.id === 'string');
  } catch {
    return null;
  }
}

function writeSessionCache(base: string, models: LLMModel[]): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(sessionKey(base), JSON.stringify(models));
  } catch {
    /* quota — ignore */
  }
}

export async function fetchLlmModels(): Promise<LLMModel[]> {
  const base = effectiveBaseUrl();
  const cached = memCache.get(base);
  if (cached) return cached;
  const session = readSessionCache(base);
  if (session) {
    memCache.set(base, session);
    return session;
  }
  const existing = inflight.get(base);
  if (existing) return existing;

  const promise = (async () => {
    const apiKey = activeLlmApiKey(loadSettings());
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
    const res = await fetch(`${base}/models`, { headers });
    if (!res.ok) throw new Error(`${base}/models ${res.status}`);
    const json = (await res.json()) as { data?: Array<{ id?: string; name?: string }> };
    const models: LLMModel[] = (json.data ?? [])
      .filter((m): m is { id: string; name?: string } => typeof m?.id === 'string')
      .map((m) => ({ id: m.id, name: m.name }));
    memCache.set(base, models);
    writeSessionCache(base, models);
    return models;
  })();
  inflight.set(base, promise);
  try {
    return await promise;
  } finally {
    inflight.delete(base);
  }
}
