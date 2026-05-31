/**
 * Preset registry of LLM providers.
 *
 * Two kinds:
 *   * ``auth: 'api_key'`` — user pastes a bearer token. We POST to
 *     ``{baseUrl}/chat/completions`` with that token. Covers OpenRouter,
 *     OpenAI, Groq, Together, etc.
 *   * ``auth: 'oauth'``   — user signs in through their browser. The backend
 *     drives a PKCE flow against the provider's auth server, stores the
 *     refresh+access tokens server-side, and routes future requests through
 *     the right transport (Codex uses the OpenAI Responses API; xAI uses
 *     standard chat completions with the OAuth token as bearer).
 *
 * Plus a ``custom`` slot for any other OpenAI-compatible base URL.
 */
export type ProviderPreset = ApiKeyPreset | OAuthPreset;

export interface ApiKeyPreset {
  id: string;
  label: string;
  auth: 'api_key';
  baseUrl: string;
  /** Where the user can grab an API key — surfaced as a small hint link. */
  keysUrl?: string;
}

export interface OAuthPreset {
  id: string;
  label: string;
  auth: 'oauth';
  /** Matches the backend ``/api/auth/{authProviderId}/start`` route. */
  authProviderId: string;
  /** One-line description shown next to the Sign in button. */
  description: string;
  /** Hardcoded model allowlist for the model autocomplete dropdown.
   * OAuth providers either don't expose ``/models`` at the endpoint we hit
   * (Codex's ``chatgpt.com/backend-api/codex/responses``) or need a
   * server-side token to read it — neither suits an in-browser fetch, so we
   * ship a known-good list. */
  availableModels: string[];
}

export const CUSTOM_PRESET_ID = 'custom';

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: 'openrouter',
    label: 'OpenRouter',
    auth: 'api_key',
    baseUrl: 'https://openrouter.ai/api/v1',
    keysUrl: 'https://openrouter.ai/keys',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    auth: 'api_key',
    baseUrl: 'https://api.openai.com/v1',
    keysUrl: 'https://platform.openai.com/api-keys',
  },
  {
    id: 'codex',
    label: 'ChatGPT (subscription)',
    auth: 'oauth',
    authProviderId: 'codex',
    description:
      "sign in with your ChatGPT Pro/Plus account. requests bill against your subscription, not the OpenAI API. limited to GPT-5.x models.",
    // Allowlist mirrors what the Codex CLI / opencode accept on the
    // chatgpt.com/backend-api/codex/responses endpoint.
    availableModels: [
      'gpt-5.5',
      'gpt-5.4',
      'gpt-5.4-mini',
      'gpt-5.3-codex',
      'gpt-5.3-codex-spark',
      'gpt-5.2',
    ],
  },
  {
    id: 'groq',
    label: 'Groq',
    auth: 'api_key',
    baseUrl: 'https://api.groq.com/openai/v1',
    keysUrl: 'https://console.groq.com/keys',
  },
  {
    id: 'together',
    label: 'Together',
    auth: 'api_key',
    baseUrl: 'https://api.together.xyz/v1',
    keysUrl: 'https://api.together.xyz/settings/api-keys',
  },
  {
    id: 'deepseek',
    label: 'DeepSeek',
    auth: 'api_key',
    baseUrl: 'https://api.deepseek.com/v1',
    keysUrl: 'https://platform.deepseek.com/api_keys',
  },
  {
    id: 'cerebras',
    label: 'Cerebras',
    auth: 'api_key',
    baseUrl: 'https://api.cerebras.ai/v1',
    keysUrl: 'https://cloud.cerebras.ai/platform',
  },
  {
    id: 'fireworks',
    label: 'Fireworks',
    auth: 'api_key',
    baseUrl: 'https://api.fireworks.ai/inference/v1',
    keysUrl: 'https://fireworks.ai/account/api-keys',
  },
  {
    id: 'xai',
    label: 'xAI',
    auth: 'api_key',
    baseUrl: 'https://api.x.ai/v1',
    keysUrl: 'https://console.x.ai/',
  },
  {
    id: 'xai-oauth',
    label: 'xAI (sign in)',
    auth: 'oauth',
    authProviderId: 'xai',
    description: 'sign in with your xAI account instead of pasting an API key.',
    // A short hardcoded list of currently-available Grok models. Users can
    // still type any model id by hand if xAI ships a new one.
    availableModels: ['grok-4', 'grok-2-1212', 'grok-2-vision-1212', 'grok-2-mini'],
  },
  {
    id: 'mistral',
    label: 'Mistral',
    auth: 'api_key',
    baseUrl: 'https://api.mistral.ai/v1',
    keysUrl: 'https://console.mistral.ai/api-keys/',
  },
];

const DEFAULT_PRESET = PROVIDER_PRESETS[0]; // OpenRouter

function normalize(url: string): string {
  return url.trim().replace(/\/+$/, '');
}

/** Match a stored base URL back to a preset id; returns ``'custom'`` if it
 * doesn't correspond to any known api-key preset. Only consults api-key
 * presets — OAuth presets are identified by an explicit preset-id setting.
 */
export function presetIdForUrl(url: string): string {
  if (!url) return DEFAULT_PRESET.id;
  const n = normalize(url);
  const match = PROVIDER_PRESETS.find(
    (p): p is ApiKeyPreset => p.auth === 'api_key' && normalize(p.baseUrl) === n,
  );
  return match ? match.id : CUSTOM_PRESET_ID;
}

export function presetById(id: string): ProviderPreset | undefined {
  return PROVIDER_PRESETS.find((p) => p.id === id);
}

export function isOAuthPreset(p: ProviderPreset | undefined): p is OAuthPreset {
  return !!p && p.auth === 'oauth';
}

export function isApiKeyPreset(p: ProviderPreset | undefined): p is ApiKeyPreset {
  return !!p && p.auth === 'api_key';
}
