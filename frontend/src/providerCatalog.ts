/**
 * Provider/model catalog, served by the backend (`/api/catalog/*`).
 *
 * Replaces the old hardcoded `PROVIDER_PRESETS` + direct `{base}/models` fetch.
 * The provider list, per-model capabilities, and reasoning variants all come
 * from the backend's models.dev catalog. Cached in-memory + sessionStorage;
 * `refreshCatalog()` busts the cache (call it after connecting/disconnecting a
 * provider so freshly-available providers show up).
 */

export interface CatalogModel {
  id: string;
  name: string;
  reasoning: boolean;
  variants: string[];
  default_variant: string | null;
  release_date: string;
  limit: { context: number; output: number };
  cost: { input?: number; output?: number } | null;
}

export interface AuthMethod {
  type: 'api' | 'oauth';
  label: string;
  provider?: string;
}

export interface CatalogProvider {
  id: string;
  name: string;
  popular: boolean;
  env: string[];
  base_url: string | null;
  executable: boolean;
  oauth_connected: boolean;
  auth: AuthMethod[];
  models: CatalogModel[];
}

export interface Catalog {
  providers: CatalogProvider[];
  stale: boolean;
}

/** Pseudo-provider for a bring-your-own OpenAI-compatible endpoint (self-hosted
 * vLLM/llama.cpp, gateways, or any provider not in the catalog). Models are
 * entered by hand since there's no catalog to enumerate. */
export const CUSTOM_PROVIDER_ID = 'custom';
export const CUSTOM_PROVIDER: CatalogProvider = {
  id: CUSTOM_PROVIDER_ID,
  name: 'Custom endpoint',
  popular: false,
  env: [],
  base_url: null,
  executable: true,
  oauth_connected: false,
  auth: [{ type: 'api', label: 'API Key' }],
  models: [],
};

const SESSION_KEY = 'orchestra:catalog';
export const CATALOG_CHANGED_EVENT = 'orchestra:catalog-changed';

let memo: Promise<Catalog> | null = null;

function readSession(): Catalog | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as Catalog) : null;
  } catch {
    return null;
  }
}

function writeSession(c: Catalog): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(c));
  } catch {
    /* quota / disabled storage — fine, mem cache still holds it */
  }
}

async function fetchCatalog(): Promise<Catalog> {
  const r = await fetch('/api/catalog/providers');
  if (!r.ok) throw new Error(`GET /api/catalog/providers → ${r.status}`);
  const data = (await r.json()) as Catalog;
  writeSession(data);
  return data;
}

/** The catalog, cached. Resolves from sessionStorage instantly on a warm tab,
 * otherwise fetches once and shares the promise across callers. */
export function getCatalog(): Promise<Catalog> {
  if (memo) return memo;
  const cached = readSession();
  if (cached) {
    memo = Promise.resolve(cached);
    // Revalidate in the background so a long-lived tab picks up new models.
    void fetchCatalog()
      .then((fresh) => {
        memo = Promise.resolve(fresh);
        emitChanged();
      })
      .catch(() => {});
    return memo;
  }
  memo = fetchCatalog();
  return memo;
}

/** Force a server round-trip (POSTs /refresh) and replace the cache. */
export async function refreshCatalog(): Promise<Catalog> {
  await fetch('/api/catalog/refresh', { method: 'POST' }).catch(() => {});
  memo = fetchCatalog();
  const c = await memo;
  emitChanged();
  return c;
}

function emitChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(CATALOG_CHANGED_EVENT));
  }
}

export function findProvider(c: Catalog, id: string): CatalogProvider | undefined {
  return c.providers.find((p) => p.id === id);
}

export function findModel(
  c: Catalog,
  providerID: string,
  modelID: string,
): CatalogModel | undefined {
  return findProvider(c, providerID)?.models.find((m) => m.id === modelID);
}
