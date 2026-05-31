/**
 * Preset registry of popular OpenAI-compatible providers. Selecting one
 * auto-fills `llm_base_url`; "custom" lets the user paste any base URL.
 *
 * Curated list: providers whose chat-completions endpoint speaks the OpenAI
 * dialect with nothing more than a bearer token (so a preset is enough — no
 * region prefixes, OAuth, or per-account base URLs required).
 */
export interface ProviderPreset {
  id: string;
  label: string;
  baseUrl: string;
  /** Where the user can grab an API key — surfaced as a small hint link. */
  keysUrl?: string;
}

export const CUSTOM_PRESET_ID = 'custom';

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: 'openrouter',
    label: 'OpenRouter',
    baseUrl: 'https://openrouter.ai/api/v1',
    keysUrl: 'https://openrouter.ai/keys',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    keysUrl: 'https://platform.openai.com/api-keys',
  },
  {
    id: 'groq',
    label: 'Groq',
    baseUrl: 'https://api.groq.com/openai/v1',
    keysUrl: 'https://console.groq.com/keys',
  },
  {
    id: 'together',
    label: 'Together',
    baseUrl: 'https://api.together.xyz/v1',
    keysUrl: 'https://api.together.xyz/settings/api-keys',
  },
  {
    id: 'deepseek',
    label: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com/v1',
    keysUrl: 'https://platform.deepseek.com/api_keys',
  },
  {
    id: 'cerebras',
    label: 'Cerebras',
    baseUrl: 'https://api.cerebras.ai/v1',
    keysUrl: 'https://cloud.cerebras.ai/platform',
  },
  {
    id: 'fireworks',
    label: 'Fireworks',
    baseUrl: 'https://api.fireworks.ai/inference/v1',
    keysUrl: 'https://fireworks.ai/account/api-keys',
  },
  {
    id: 'xai',
    label: 'xAI',
    baseUrl: 'https://api.x.ai/v1',
    keysUrl: 'https://console.x.ai/',
  },
  {
    id: 'mistral',
    label: 'Mistral',
    baseUrl: 'https://api.mistral.ai/v1',
    keysUrl: 'https://console.mistral.ai/api-keys/',
  },
];

const DEFAULT_PRESET = PROVIDER_PRESETS[0]; // OpenRouter

function normalize(url: string): string {
  return url.trim().replace(/\/+$/, '');
}

/** Match a base URL to a known preset; returns 'custom' if no match. */
export function presetIdForUrl(url: string): string {
  if (!url) return DEFAULT_PRESET.id;
  const n = normalize(url);
  const match = PROVIDER_PRESETS.find((p) => normalize(p.baseUrl) === n);
  return match ? match.id : CUSTOM_PRESET_ID;
}

export function presetById(id: string): ProviderPreset | undefined {
  return PROVIDER_PRESETS.find((p) => p.id === id);
}
