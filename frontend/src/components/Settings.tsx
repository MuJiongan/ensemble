import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Settings } from '../types';
import { loadSettings, saveSettings } from '../localSettings';
import {
  CUSTOM_PRESET_ID,
  PROVIDER_PRESETS,
  isOAuthPreset,
  isApiKeyPreset,
  presetById,
  presetIdForUrl,
} from '../llmProviders';
import { ModelInput } from './ModelInput';
import { OAuthLoginField } from './OAuthLoginField';
import {
  cancelMcpLogin,
  listMcpTools,
  mcpLoginStatus,
  mcpLogout,
  pollMcpLogin,
  probeStatus,
  startMcpLogin,
  type McpLoginStatus,
  type McpProbeResult,
  type McpServerProbe,
  type McpToolInfo,
} from '../mcpApi';

const EMPTY: Settings = {
  llm_api_keys: {},
  llm_provider_preset_id: '',
  llm_base_url: '',
  parallel_api_key: '',
  default_orchestrator_models: {},
  default_node_models: {},
  mcp_servers: '',
};

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<Settings>(EMPTY);
  const [revealKeys, setRevealKeys] = useState(false);
  // Tracks which preset is selected. Derived from the stored base URL on
  // load; kept in component state so the user can switch to "custom" and
  // type a new URL even before they've finished editing it.
  const [presetId, setPresetId] = useState<string>(PROVIDER_PRESETS[0].id);
  // Autosave is gated on `loaded` *state* (not a ref) so the save effect
  // re-runs after `s` is updated to the loaded value — otherwise it would
  // fire once with the pre-load EMPTY closure and wipe stored settings.
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const settings = loadSettings();
    setS(settings);
    setPresetId(settings.llm_provider_preset_id || presetIdForUrl(settings.llm_base_url));
    setLoaded(true);
  }, []);

  useEffect(() => {
    if (loaded) saveSettings(s);
  }, [s, loaded]);

  const onPresetChange = (id: string) => {
    setPresetId(id);
    setS((prev) => ({ ...prev, llm_provider_preset_id: id }));
    if (id === CUSTOM_PRESET_ID) {
      // Leave llm_base_url as-is — the input becomes editable so the user
      // can paste their endpoint.
      return;
    }
    const preset = presetById(id);
    if (isApiKeyPreset(preset)) {
      // Snap llm_base_url to the chosen api-key preset's endpoint.
      setS((prev) => ({ ...prev, llm_base_url: preset.baseUrl }));
    }
    // OAuth presets leave llm_base_url alone — the backend ignores it when
    // X-Llm-Provider-Id is set.
  };

  const currentPreset =
    presetId === CUSTOM_PRESET_ID ? undefined : presetById(presetId);
  const currentApiKeyPreset = isApiKeyPreset(currentPreset) ? currentPreset : null;
  const currentOAuthPreset = isOAuthPreset(currentPreset) ? currentPreset : null;

  return (
    <div style={{ width: '100%', height: '100%', overflow: 'auto', background: 'var(--paper)' }}>
      <div style={{ maxWidth: 640, margin: '0 auto', padding: '40px 32px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 4 }}>
          <span className="smallcaps">settings</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
          <h2
            className="serif"
            style={{
              margin: '4px 0 28px',
              fontSize: 30,
              fontWeight: 400,
              letterSpacing: '-0.01em',
              color: 'var(--ink)',
            }}
          >
            keys & defaults.
          </h2>
          <button className="text-btn" onClick={onClose} title="close settings">
            close
          </button>
        </div>

        <p
          className="serif"
          style={{
            fontStyle: 'italic',
            color: 'var(--ink-3)',
            fontSize: 13.5,
            margin: '0 0 24px',
            lineHeight: 1.55,
          }}
        >
          your keys live in this browser's <span className="mono" style={{ fontStyle: 'normal' }}>localStorage</span>{' '}
          — the backend never persists them. they're sent as headers on each request.
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
          <ProviderField value={presetId} onChange={onPresetChange} />
          {presetId === CUSTOM_PRESET_ID && (
            <Field
              label="llm base url"
              value={s.llm_base_url}
              onChange={(v) => setS({ ...s, llm_base_url: v })}
              placeholder="https://your-endpoint.example.com/v1"
              hint="we POST to {base}/chat/completions. any openai-compatible endpoint works — gateways, self-hosted vllm/llama.cpp, etc."
            />
          )}
          {currentOAuthPreset ? (
            <OAuthLoginField preset={currentOAuthPreset} />
          ) : (
            <Field
              label={`${currentApiKeyPreset?.label.toLowerCase() ?? 'custom'} api key`}
              value={s.llm_api_keys[presetId] ?? ''}
              onChange={(v) =>
                setS({ ...s, llm_api_keys: { ...s.llm_api_keys, [presetId]: v } })
              }
              secret={!revealKeys}
              hint={
                currentApiKeyPreset
                  ? `bearer token for ${currentApiKeyPreset.label}. each provider keeps its own key — switch providers above to manage another.`
                  : 'bearer token for the endpoint above. each provider (and the custom slot) keeps its own key.'
              }
            />
          )}
          <Field
            label="parallel.ai api key"
            value={s.parallel_api_key}
            onChange={(v) => setS({ ...s, parallel_api_key: v })}
            secret={!revealKeys}
            hint="used by the web_search tool."
          />

          <label
            className="serif"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              fontStyle: 'italic',
              fontSize: 13,
              color: 'var(--ink-3)',
              cursor: 'pointer',
              marginTop: -8,
            }}
          >
            <input
              type="checkbox"
              checked={revealKeys}
              onChange={(e) => setRevealKeys(e.target.checked)}
            />
            reveal keys
          </label>

          <ModelField
            label="default orchestrator model"
            value={s.default_orchestrator_models[presetId] ?? ''}
            onChange={(v) =>
              setS({
                ...s,
                default_orchestrator_models: { ...s.default_orchestrator_models, [presetId]: v },
              })
            }
            hint={`used by the orchestrator when the ${
              currentPreset?.label ?? 'active'
            } provider is selected. each provider keeps its own default.`}
            settings={s}
          />
          <ModelField
            label="default node model"
            value={s.default_node_models[presetId] ?? ''}
            onChange={(v) =>
              setS({
                ...s,
                default_node_models: { ...s.default_node_models, [presetId]: v },
              })
            }
            hint="default for ctx.call_llm inside nodes when no model is specified."
            settings={s}
          />

          <McpServersEditor
            value={s.mcp_servers}
            onChange={(v) => setS({ ...s, mcp_servers: v })}
          />
        </div>
      </div>
    </div>
  );
}

interface ProviderOption {
  id: string;
  label: string;
  /** What to show as the second line under the label.
   *  ``url`` — a base URL (api-key presets).
   *  ``info`` — a description string (OAuth presets + the Custom slot). */
  secondaryKind: 'url' | 'info';
  secondary: string;
  /** External "get a key" link shown only when this option is selected and
   *  the option is an api-key preset. */
  keysUrl?: string;
}

function ProviderField({
  value,
  onChange,
}: {
  value: string;
  onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const options: ProviderOption[] = [
    ...PROVIDER_PRESETS.map((p): ProviderOption =>
      p.auth === 'api_key'
        ? {
            id: p.id,
            label: p.label,
            secondaryKind: 'url',
            secondary: p.baseUrl,
            keysUrl: p.keysUrl,
          }
        : {
            id: p.id,
            label: p.label,
            secondaryKind: 'info',
            secondary: p.description,
          },
    ),
    {
      id: CUSTOM_PRESET_ID,
      label: 'Custom',
      secondaryKind: 'info',
      secondary: 'bring your own openai-compatible base url',
    },
  ];
  const selectedIdx = Math.max(options.findIndex((o) => o.id === value), 0);
  const selected = options[selectedIdx];

  // Outside-click closes the popup.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  // Keep highlight scrolled into view as user arrows through.
  useEffect(() => {
    if (!open || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[data-idx="${highlight}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [highlight, open]);

  const commit = (id: string) => {
    onChange(id);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const toggle = () => {
    if (!open) setHighlight(selectedIdx);
    setOpen((v) => !v);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!open) {
        setHighlight(selectedIdx);
        setOpen(true);
      } else {
        setHighlight((h) => Math.min(h + 1, options.length - 1));
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) {
        setHighlight(selectedIdx);
        setOpen(true);
      } else {
        setHighlight((h) => Math.max(h - 1, 0));
      }
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (open) commit(options[highlight].id);
      else toggle();
    } else if (e.key === 'Escape' && open) {
      e.preventDefault();
      setOpen(false);
    }
  };

  return (
    <div ref={containerRef} style={{ position: 'relative' }}>
      <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
        provider
      </label>
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        onKeyDown={onKeyDown}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="provider preset"
        style={{
          width: '100%',
          background: 'transparent',
          border: 0,
          borderBottom: '1px solid var(--rule)',
          padding: '8px 0',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          cursor: 'pointer',
          color: 'var(--ink)',
          textAlign: 'left',
          outline: 'none',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, gap: 2 }}>
          <span
            className="serif"
            style={{ fontStyle: 'italic', fontSize: 14, color: 'var(--ink)' }}
          >
            {selected.label}
          </span>
          <span
            className={selected.secondaryKind === 'url' ? 'mono' : 'serif'}
            style={{
              fontSize: selected.secondaryKind === 'url' ? 11.5 : 12,
              fontStyle: selected.secondaryKind === 'info' ? 'italic' : 'normal',
              color: 'var(--ink-4)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {selected.secondary}
          </span>
        </div>
        <Chevron open={open} />
      </button>

      {selected.keysUrl && !open && (
        <div
          className="serif"
          style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--ink-4)', marginTop: 6 }}
        >
          <a
            href={selected.keysUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: 'var(--ink-3)', textDecoration: 'underline' }}
          >
            get an api key ↗
          </a>
        </div>
      )}

      {open && (
        <div
          ref={listRef}
          role="listbox"
          aria-label="select provider"
          className="shadow-card fade-in"
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            marginTop: 4,
            maxHeight: 320,
            overflowY: 'auto',
            background: 'var(--paper)',
            border: '1px solid var(--rule)',
            borderRadius: 4,
            padding: 4,
            zIndex: 100,
          }}
        >
          {options.map((o, i) => {
            const active = i === highlight;
            const isSelected = o.id === value;
            return (
              <div
                key={o.id}
                role="option"
                aria-selected={isSelected}
                data-idx={i}
                onMouseDown={(e) => {
                  e.preventDefault();
                  commit(o.id);
                }}
                onMouseEnter={() => setHighlight(i)}
                style={{
                  padding: '8px 10px',
                  borderRadius: 3,
                  cursor: 'pointer',
                  background: active ? 'var(--paper-2)' : 'transparent',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                }}
              >
                <span
                  aria-hidden
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 6,
                    background: isSelected ? 'var(--ink)' : 'transparent',
                    flexShrink: 0,
                  }}
                />
                <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, gap: 1 }}>
                  <span
                    className="serif"
                    style={{
                      fontStyle: 'italic',
                      fontSize: 13.5,
                      color: 'var(--ink)',
                    }}
                  >
                    {o.label}
                  </span>
                  <span
                    className={o.secondaryKind === 'url' ? 'mono' : 'serif'}
                    style={{
                      fontSize: o.secondaryKind === 'url' ? 11 : 11.5,
                      fontStyle: o.secondaryKind === 'info' ? 'italic' : 'normal',
                      color: 'var(--ink-4)',
                      whiteSpace: o.secondaryKind === 'url' ? 'nowrap' : 'normal',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      lineHeight: 1.4,
                    }}
                  >
                    {o.secondary}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      aria-hidden
      style={{
        color: 'var(--ink-4)',
        transition: 'transform 120ms ease',
        transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
        flexShrink: 0,
      }}
    >
      <path d="M2 4 L5 7 L8 4" stroke="currentColor" strokeWidth="1.2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Field({
  label, value, onChange, hint, secret, placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint?: string;
  secret?: boolean;
  placeholder?: string;
}) {
  return (
    <div>
      <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
        {label}
      </label>
      <input
        type={secret ? 'password' : 'text'}
        className="mono"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        style={{
          width: '100%',
          background: 'transparent',
          border: 0,
          borderBottom: '1px solid var(--rule)',
          padding: '8px 0',
          fontSize: 13,
          color: 'var(--ink)',
          outline: 'none',
        }}
      />
      {hint && (
        <div
          className="serif"
          style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--ink-4)', marginTop: 6 }}
        >
          {hint}
        </div>
      )}
    </div>
  );
}

// --- MCP servers editor ----------------------------------------------------
// A structured form over the `mcp_servers` JSON string (opencode's shape: a
// map of name → {type:"local", command:[...]} | {type:"remote", url, headers}).
// We hold rows in local state and serialize back to JSON on every edit, so the
// stored format — and the backend contract — is unchanged. Unknown per-server
// keys are preserved verbatim through edits via `rest`.

type McpType = 'local' | 'remote';

interface HeaderRow {
  uid: number;
  key: string;
  value: string;
}

interface McpRow {
  uid: number;
  name: string;
  type: McpType;
  command: string; // space-separated; split into a list on serialize
  url: string;
  headers: HeaderRow[];
  env: HeaderRow[]; // local `environment` key/value pairs
  enabled: boolean;
  // Per-tool opt-outs (raw MCP tool names). Driven by the "view tools" dialog;
  // the backend uses the same set to filter out tools before they reach the
  // orchestrator and the runtime registry.
  disabledTools: string[];
  // Remote servers are OAuth-capable by default (opencode's `oauth !== false`).
  // `oauthOn` is false only when the JSON sets `"oauth": false` to opt out.
  oauthOn: boolean;
  // Pre-registered OAuth client. Empty strings = use Dynamic Client
  // Registration (RFC 7591). Set these for servers that don't support DCR
  // (Slack et al.). `oauthRedirectUri` overrides the loopback callback when a
  // provider rejects the default `http://127.0.0.1:19876/mcp/oauth/callback`.
  oauthClientId: string;
  oauthClientSecret: string;
  oauthScope: string;
  oauthRedirectUri: string;
  rest: Record<string, unknown>; // any other keys we don't surface, preserved verbatim
}

let _mcpUid = 0;
const nextUid = () => ++_mcpUid;

function asStr(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function kvRows(v: unknown): HeaderRow[] {
  if (!v || typeof v !== 'object') return [];
  return Object.entries(v as Record<string, unknown>).map(([k, val]) => ({
    uid: nextUid(),
    key: k,
    value: asStr(val),
  }));
}

function rowsToKv(rows: HeaderRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    if (r.key.trim()) out[r.key.trim()] = r.value;
  }
  return out;
}

function parseServers(raw: string): McpRow[] {
  if (!raw.trim()) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  // Tolerate the `{"mcp": {...}}` wrapper the backend also accepts.
  const map =
    parsed && typeof parsed === 'object' && 'mcp' in (parsed as object)
      ? (parsed as { mcp: unknown }).mcp
      : parsed;
  if (!map || typeof map !== 'object') return [];
  const rows: McpRow[] = [];
  for (const [name, cfgRaw] of Object.entries(map as Record<string, unknown>)) {
    const cfg = (cfgRaw && typeof cfgRaw === 'object' ? cfgRaw : {}) as Record<string, unknown>;
    // `timeout` rides along in `rest` (not surfaced in the UI but preserved).
    // `oauth` is split: `false` flips oauthOn off; a dict is unpacked into
    // first-class fields so the user can edit pre-registered creds in the UI.
    const { type, command, url, headers, environment, enabled, disabled_tools, oauth, ...rest } =
      cfg;
    const t: McpType = type === 'remote' ? 'remote' : 'local';
    const cmd = Array.isArray(command)
      ? command.map(asStr).filter(Boolean).join(' ')
      : asStr(command);
    // Remote = OAuth-capable unless the JSON explicitly opts out.
    const oauthOn = t === 'remote' && oauth !== false;
    const oauthObj = oauth && typeof oauth === 'object' ? (oauth as Record<string, unknown>) : {};
    const disabledTools = Array.isArray(disabled_tools)
      ? (disabled_tools.filter((t) => typeof t === 'string') as string[])
      : [];
    rows.push({
      uid: nextUid(),
      name,
      type: t,
      command: cmd,
      url: asStr(url),
      headers: kvRows(headers),
      env: kvRows(environment),
      enabled: enabled !== false,
      disabledTools,
      oauthOn,
      oauthClientId: asStr(oauthObj.clientId ?? oauthObj.client_id),
      oauthClientSecret: asStr(oauthObj.clientSecret ?? oauthObj.client_secret),
      oauthScope: asStr(oauthObj.scope),
      oauthRedirectUri: asStr(oauthObj.redirectUri ?? oauthObj.redirect_uri),
      rest,
    });
  }
  return rows;
}

function serializeServers(rows: McpRow[]): string {
  const out: Record<string, unknown> = {};
  for (const r of rows) {
    const name = r.name.trim();
    if (!name) continue; // unnamed rows stay in the UI but aren't persisted yet
    const entry: Record<string, unknown> = { ...r.rest, type: r.type };
    if (!r.enabled) entry.enabled = false;
    if (r.type === 'local') {
      entry.command = r.command.trim().split(/\s+/).filter(Boolean);
      const env = rowsToKv(r.env);
      if (Object.keys(env).length) entry.environment = env;
    } else {
      entry.url = r.url.trim();
      const headers = rowsToKv(r.headers);
      if (Object.keys(headers).length) entry.headers = headers;
      // `oauth: false` opts out entirely; otherwise emit a dict only when the
      // user filled in at least one pre-reg field, so DCR-capable servers
      // (the default) stay terse in the JSON.
      if (!r.oauthOn) {
        entry.oauth = false;
      } else {
        const oauth: Record<string, unknown> = {};
        if (r.oauthClientId.trim()) oauth.clientId = r.oauthClientId.trim();
        if (r.oauthClientSecret.trim()) oauth.clientSecret = r.oauthClientSecret.trim();
        if (r.oauthScope.trim()) oauth.scope = r.oauthScope.trim();
        if (r.oauthRedirectUri.trim()) oauth.redirectUri = r.oauthRedirectUri.trim();
        if (Object.keys(oauth).length) entry.oauth = oauth;
      }
    }
    if (r.disabledTools.length) entry.disabled_tools = r.disabledTools;
    out[name] = entry;
  }
  return Object.keys(out).length ? JSON.stringify(out, null, 2) : '';
}

function newRow(): McpRow {
  return {
    uid: nextUid(),
    name: '',
    type: 'local',
    command: '',
    url: '',
    headers: [],
    env: [],
    enabled: true,
    disabledTools: [],
    oauthOn: true,
    oauthClientId: '',
    oauthClientSecret: '',
    oauthScope: '',
    oauthRedirectUri: '',
    rest: {},
  };
}

function McpServersEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const [rows, setRows] = useState<McpRow[]>(() => parseServers(value));
  const [status, setStatus] = useState<McpProbeResult>({});
  // The in-progress new server. It is NOT part of `rows` and is never
  // serialized, so a half-typed server can't be mistaken for a saved one.
  // Clicking "add server" is the explicit commit moment.
  const [draft, setDraft] = useState<McpRow | null>(null);
  // Re-parse only when `value` changes to something we didn't ourselves emit
  // (e.g. the async settings load on mount), so in-progress edits aren't
  // clobbered by the round-trip back through the parent.
  const lastEmitted = useRef(value);

  const runProbeAll = useCallback(async (configJson: string) => {
    if (!configJson.trim()) {
      setStatus({});
      return;
    }
    try {
      setStatus(await probeStatus(configJson));
    } catch {
      /* leave prior status intact */
    }
  }, []);

  useEffect(() => {
    if (value !== lastEmitted.current) {
      setRows(parseServers(value));
      lastEmitted.current = value;
      // Auto-probe once when the panel hydrates with stored config (the async
      // settings load), but not on our own keystroke-driven emits.
      void runProbeAll(value);
    }
  }, [value, runProbeAll]);

  const commit = (next: McpRow[]) => {
    setRows(next);
    const json = serializeServers(next);
    lastEmitted.current = json;
    onChange(json);
  };

  const patch = (uid: number, fields: Partial<McpRow>) => {
    const next = rows.map((r) => (r.uid === uid ? { ...r, ...fields } : r));
    commit(next);
    // Enabling/disabling is a discrete action (like opencode's toggle), so
    // refresh status against the new config. Keystroke edits don't re-probe —
    // local servers spawn a child process per connect, so probing is tied to
    // explicit moments (panel open, toggle, login), never every edit.
    if ('enabled' in fields) void runProbeAll(serializeServers(next));
  };

  // Draft is valid (and thus committable) once it has a unique name and the
  // field its type requires. This gates the "add server" button so the user
  // gets a clear, deterministic signal for when the server will actually save.
  const draftError = ((): string | null => {
    if (!draft) return null;
    const name = draft.name.trim();
    if (!name) return 'name required';
    if (rows.some((r) => r.name.trim() === name)) return 'name already used';
    if (draft.type === 'local' && !draft.command.trim()) return 'command required';
    if (draft.type === 'remote' && !draft.url.trim()) return 'url required';
    return null;
  })();

  const addDraft = () => {
    if (draftError || !draft) return;
    const next = [...rows, draft];
    commit(next);
    setDraft(null);
    // Probe right away so the freshly-added server shows connected / needs-auth
    // / failed — an unambiguous confirmation that it landed.
    void runProbeAll(serializeServers(next));
  };

  const removeServer = (uid: number) => {
    const row = rows.find((r) => r.uid === uid);
    commit(rows.filter((r) => r.uid !== uid));
    // Best-effort: drop any stored OAuth credentials for this server so a
    // re-added server with the same name doesn't inherit stale tokens. Only
    // remote servers ever have credentials, but the backend tolerates a miss.
    if (row && row.name.trim() && row.type === 'remote') {
      void mcpLogout(row.name.trim());
    }
  };

  // Re-probe after a successful login so the just-authorized server flips to
  // connected without the user doing anything. ``rows`` is stable across a
  // login flow, so serializing the current closure value is correct.
  const reprobeAfterAuth = useCallback(
    () => void runProbeAll(serializeServers(rows)),
    [rows, runProbeAll],
  );

  // Server whose tool list is being viewed in the popout (null = closed).
  const [toolsFor, setToolsFor] = useState<string | null>(null);
  // uid of the saved row whose editor popout is open (null = closed).
  const [editingUid, setEditingUid] = useState<number | null>(null);
  const editingRow = editingUid === null ? null : rows.find((r) => r.uid === editingUid) ?? null;
  const configJson = serializeServers(rows);

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          marginBottom: 6,
        }}
      >
        <label className="smallcaps">mcp servers</label>
        <button
          className="text-btn"
          type="button"
          onClick={() => setDraft(newRow())}
          title="add an MCP server"
        >
          + add server
        </button>
      </div>

      {rows.length === 0 ? (
        <div
          className="serif"
          style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--ink-4)', lineHeight: 1.5 }}
        >
          connect model context protocol servers — their tools become available to nodes and the
          orchestrator. add a local command (e.g. <span className="mono" style={{ fontStyle: 'normal' }}>npx -y @playwright/mcp@latest</span>) or a remote url.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {rows.map((r) => (
            <McpServerRow
              key={r.uid}
              row={r}
              probe={status[r.name]}
              onPatch={(fields) => patch(r.uid, fields)}
              onEdit={() => setEditingUid(r.uid)}
              onViewTools={() => setToolsFor(r.name.trim())}
              onReprobe={reprobeAfterAuth}
              onRemove={() => removeServer(r.uid)}
            />
          ))}
        </div>
      )}

      {draft && (
        <McpModal title="add MCP server" onClose={() => setDraft(null)}>
          <McpServerCard
            row={draft}
            onReprobe={reprobeAfterAuth}
            onPatch={(fields) => setDraft({ ...draft, ...fields })}
            draft={{ error: draftError, onAdd: addDraft, onCancel: () => setDraft(null) }}
          />
        </McpModal>
      )}

      {editingRow && (
        <McpModal
          title={`edit ${editingRow.name || 'server'}`}
          onClose={() => setEditingUid(null)}
        >
          <McpServerCard
            row={editingRow}
            probe={status[editingRow.name]}
            onReprobe={reprobeAfterAuth}
            onPatch={(fields) => patch(editingRow.uid, fields)}
          />
        </McpModal>
      )}

      {(() => {
        const row = toolsFor ? rows.find((r) => r.name === toolsFor) : null;
        if (!row) return null;
        return (
          <McpToolsDialog
            server={row.name}
            configJson={configJson}
            disabledTools={row.disabledTools}
            onToggleTool={(toolName, disabled) => {
              const next = disabled
                ? Array.from(new Set([...row.disabledTools, toolName]))
                : row.disabledTools.filter((t) => t !== toolName);
              patch(row.uid, { disabledTools: next });
            }}
            onClose={() => setToolsFor(null)}
          />
        );
      })()}
    </div>
  );
}

function McpServerRow({
  row,
  probe,
  onPatch,
  onEdit,
  onViewTools,
  onReprobe,
  onRemove,
}: {
  row: McpRow;
  probe?: McpServerProbe;
  onPatch: (fields: Partial<McpRow>) => void;
  onEdit: () => void;
  onViewTools: () => void;
  onReprobe: () => void;
  onRemove: () => void;
}) {
  const canViewTools = probe?.status === 'connected' && (probe?.tool_count ?? 0) > 0;
  return (
    <div
      style={{
        border: '1px solid var(--rule)',
        borderRadius: 4,
        padding: '12px 14px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        opacity: row.enabled ? 1 : 0.6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0 }}>
            <span
              className="mono"
              style={{
                fontSize: 13,
                fontWeight: 500,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={row.name || '(unnamed)'}
            >
              {row.name || '(unnamed)'}
            </span>
            <span
              className="serif"
              style={{ fontSize: 11, fontStyle: 'italic', color: 'var(--ink-4)' }}
            >
              {row.type}
            </span>
          </div>
          <McpStatusRow probe={probe} disabledCount={row.disabledTools.length} />
        </div>
        <EnabledToggle value={row.enabled} onChange={(v) => onPatch({ enabled: v })} />
        {canViewTools && (
          <button className="text-btn" type="button" onClick={onViewTools}>
            view tools
          </button>
        )}
        <button className="text-btn" type="button" onClick={onEdit}>
          edit
        </button>
        <button
          className="text-btn text-btn--danger"
          type="button"
          onClick={onRemove}
          title="remove server"
        >
          remove
        </button>
      </div>
      {row.type === 'remote' && row.oauthOn && (
        <McpOAuthControl row={row} probe={probe} onReprobe={onReprobe} />
      )}
    </div>
  );
}

function McpModal({
  title,
  onClose,
  children,
  width = 760,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  width?: number;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  return createPortal(
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(26, 23, 20, 0.45)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        alignItems: 'stretch',
        justifyContent: 'center',
        padding: '6vh 4vw',
        zIndex: 1000,
      }}
      className="fade-in"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="shadow-card"
        style={{
          flex: 1,
          maxWidth: width,
          background: 'var(--paper)',
          border: '1px solid var(--rule)',
          borderRadius: 4,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            gap: 10,
            padding: '14px 18px',
            borderBottom: '1px solid var(--rule)',
            background: 'var(--paper-2)',
          }}
        >
          <span className="smallcaps">{title}</span>
          <span style={{ flex: 1 }} />
          <button className="text-btn" onClick={onClose} title="close">
            close
          </button>
        </div>
        <div className="scroll" style={{ flex: 1, overflow: 'auto', padding: 18 }}>
          {children}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function McpToolsDialog({
  server,
  configJson,
  disabledTools,
  onToggleTool,
  onClose,
}: {
  server: string;
  configJson: string;
  disabledTools: string[];
  onToggleTool: (tool: string, disabled: boolean) => void;
  onClose: () => void;
}) {
  const [tools, setTools] = useState<McpToolInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Capture the config we open with; toggling a tool re-serializes config but
  // mustn't trigger a refetch — the server's tool list hasn't changed, only our
  // local opt-out state has. Reopening the dialog remounts and re-captures.
  const initialConfigRef = useRef(configJson);

  useEffect(() => {
    let cancelled = false;
    setTools(null);
    setError(null);
    (async () => {
      try {
        const list = await listMcpTools(initialConfigRef.current, server);
        if (!cancelled) setTools(list);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [server]);

  const disabledSet = new Set(disabledTools);
  const total = tools?.length ?? 0;
  const enabled = tools ? tools.filter((t) => !disabledSet.has(t.tool)).length : 0;
  const subtitle =
    tools === null && !error
      ? 'loading…'
      : error
      ? error
      : `${enabled}/${total} tool${total === 1 ? '' : 's'} enabled on ${server}`;

  return (
    <McpModal title={`${server} · tools`} onClose={onClose} width={720}>
      <div
        className="serif"
        style={{ fontSize: 12, fontStyle: 'italic', color: 'var(--ink-4)', marginBottom: 14 }}
      >
        {subtitle}
      </div>
      {error && (
        <div
          className="serif"
          style={{ fontSize: 12.5, color: 'var(--state-err, #b04030)' }}
        >
          could not load tools: {error}
        </div>
      )}
      {tools && tools.length === 0 && !error && (
        <div className="serif" style={{ fontSize: 12.5, color: 'var(--ink-4)' }}>
          this server didn't advertise any tools.
        </div>
      )}
      {tools && tools.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {tools.map((t) => {
            const isEnabled = !disabledSet.has(t.tool);
            return (
              <div
                key={t.qualified}
                style={{
                  border: '1px solid var(--rule)',
                  borderRadius: 4,
                  padding: '10px 12px',
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 12,
                  opacity: isEnabled ? 1 : 0.55,
                }}
              >
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
                  <span className="mono" style={{ fontSize: 13 }}>
                    {t.tool}
                  </span>
                  {t.description && (
                    <div
                      className="serif"
                      style={{ fontSize: 12.5, color: 'var(--ink-3)', whiteSpace: 'pre-wrap' }}
                    >
                      {_oneLineSummary(t.description)}
                    </div>
                  )}
                </div>
                <EnabledToggle
                  value={isEnabled}
                  onChange={(v) => onToggleTool(t.tool, !v)}
                />
              </div>
            );
          })}
        </div>
      )}
    </McpModal>
  );
}

function _oneLineSummary(text: string): string {
  const head = text.trim().split(/\n/)[0].trim();
  return head.length > 200 ? head.slice(0, 197) + '…' : head;
}

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  connected: { text: 'connected', color: 'var(--state-ok, #3a7d44)' },
  needs_auth: { text: 'needs authorization', color: 'var(--state-warn, #b5852a)' },
  failed: { text: 'failed', color: 'var(--state-err, #b04030)' },
  disabled: { text: 'disabled', color: 'var(--ink-4)' },
  untested: { text: 'testing…', color: 'var(--ink-4)' },
};

function McpStatusRow({
  probe,
  disabledCount = 0,
}: {
  probe?: McpServerProbe;
  disabledCount?: number;
}) {
  const meta = probe ? STATUS_LABEL[probe.status] ?? STATUS_LABEL.failed : null;
  const total = probe?.tool_count;
  const enabled = typeof total === 'number' ? Math.max(0, total - disabledCount) : null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
      <span
        className="serif"
        style={{ fontSize: 12.5, color: meta ? meta.color : 'var(--ink-4)', fontStyle: meta ? 'normal' : 'italic' }}
      >
        {meta ? meta.text : 'untested'}
        {probe?.status === 'connected' && typeof total === 'number' && enabled !== null && (
          <span style={{ color: 'var(--ink-4)' }}>
            {' · '}
            {enabled}/{total} tool{total === 1 ? '' : 's'}
          </span>
        )}
      </span>
      {probe?.error && (
        <span
          className="mono"
          style={{ fontSize: 11, color: 'var(--ink-4)', maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          title={probe.error}
        >
          {probe.error}
        </span>
      )}
    </div>
  );
}

const mcpInputStyle: React.CSSProperties = {
  width: '100%',
  background: 'transparent',
  border: 0,
  borderBottom: '1px solid var(--rule)',
  padding: '6px 0',
  fontSize: 13,
  color: 'var(--ink)',
  outline: 'none',
  boxSizing: 'border-box',
};

function KeyValueRows({
  rows,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
  addLabel,
}: {
  rows: HeaderRow[];
  onChange: (rows: HeaderRow[]) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
  addLabel: string;
}) {
  const add = () => onChange([...rows, { uid: nextUid(), key: '', value: '' }]);
  const patch = (uid: number, fields: Partial<HeaderRow>) =>
    onChange(rows.map((h) => (h.uid === uid ? { ...h, ...fields } : h)));
  const remove = (uid: number) => onChange(rows.filter((h) => h.uid !== uid));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {rows.map((h) => (
        <div key={h.uid} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            className="mono"
            value={h.key}
            placeholder={keyPlaceholder}
            onChange={(e) => patch(h.uid, { key: e.target.value })}
            autoComplete="off"
            spellCheck={false}
            style={{ ...mcpInputStyle, flex: 1 }}
            aria-label="key"
          />
          <input
            className="mono"
            value={h.value}
            placeholder={valuePlaceholder}
            onChange={(e) => patch(h.uid, { value: e.target.value })}
            autoComplete="off"
            spellCheck={false}
            style={{ ...mcpInputStyle, flex: 1 }}
            aria-label="value"
          />
          <button className="text-btn" type="button" onClick={() => remove(h.uid)} title="remove">
            ×
          </button>
        </div>
      ))}
      <button className="text-btn" type="button" onClick={add} style={{ alignSelf: 'flex-start' }}>
        {addLabel}
      </button>
    </div>
  );
}

function McpOAuthClientFields({
  row,
  onPatch,
}: {
  row: McpRow;
  onPatch: (fields: Partial<McpRow>) => void;
}) {
  const [open, setOpen] = useState(
    !!(row.oauthClientId || row.oauthClientSecret || row.oauthScope || row.oauthRedirectUri),
  );
  return (
    <div
      style={{
        border: '1px dashed var(--rule)',
        borderRadius: 4,
        padding: '8px 10px',
        display: 'flex',
        flexDirection: 'column',
        gap: open ? 10 : 0,
      }}
    >
      <button
        type="button"
        className="text-btn"
        onClick={() => setOpen((v) => !v)}
        style={{ alignSelf: 'flex-start', fontSize: 11 }}
        title="set a pre-registered OAuth client for servers that don't support dynamic client registration"
      >
        {open ? '▾' : '▸'} oauth client (optional — required for servers without dynamic client registration)
      </button>
      {open && (
        <>
          <SubField label="client id">
            <input
              className="mono"
              value={row.oauthClientId}
              placeholder="from your registered OAuth app"
              onChange={(e) => onPatch({ oauthClientId: e.target.value })}
              autoComplete="off"
              spellCheck={false}
              style={mcpInputStyle}
              aria-label="oauth client id"
            />
          </SubField>
          <SubField label="client secret">
            <input
              className="mono"
              type="password"
              value={row.oauthClientSecret}
              placeholder="leave empty for public clients (PKCE only)"
              onChange={(e) => onPatch({ oauthClientSecret: e.target.value })}
              autoComplete="off"
              spellCheck={false}
              style={mcpInputStyle}
              aria-label="oauth client secret"
            />
          </SubField>
          <SubField label="scope">
            <input
              className="mono"
              value={row.oauthScope}
              placeholder="space-separated, e.g. read:org write:messages"
              onChange={(e) => onPatch({ oauthScope: e.target.value })}
              autoComplete="off"
              spellCheck={false}
              style={mcpInputStyle}
              aria-label="oauth scope"
            />
          </SubField>
          <SubField label="redirect uri (override)">
            <input
              className="mono"
              value={row.oauthRedirectUri}
              placeholder="http://127.0.0.1:19876/mcp/oauth/callback (default)"
              onChange={(e) => onPatch({ oauthRedirectUri: e.target.value })}
              autoComplete="off"
              spellCheck={false}
              style={mcpInputStyle}
              aria-label="oauth redirect uri"
            />
          </SubField>
        </>
      )}
    </div>
  );
}

function McpOAuthControl({
  row,
  probe,
  onReprobe,
}: {
  row: McpRow;
  probe?: McpServerProbe;
  onReprobe: () => void;
}) {
  const [status, setStatus] = useState<McpLoginStatus>('signed_out');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const popupRef = useRef<Window | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const name = row.name.trim();

  useEffect(() => {
    if (!name) return;
    let cancelled = false;
    (async () => {
      try {
        const s = await mcpLoginStatus(name);
        if (!cancelled) {
          setStatus(s.status);
          setError(s.error ?? null);
        }
      } catch {
        /* leave default */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [name]);

  // Pre-registered OAuth client (Slack et al. that don't implement RFC 7591).
  // The row carries these as first-class fields edited under "oauth client" in
  // the card; forward the populated ones to the login flow as the oauth dict.
  const oauthArgs = () => {
    const o: Record<string, unknown> = {};
    if (row.oauthClientId.trim()) o.clientId = row.oauthClientId.trim();
    if (row.oauthClientSecret.trim()) o.clientSecret = row.oauthClientSecret.trim();
    if (row.oauthScope.trim()) o.scope = row.oauthScope.trim();
    if (row.oauthRedirectUri.trim()) o.redirectUri = row.oauthRedirectUri.trim();
    return Object.keys(o).length ? o : null;
  };

  const onSignIn = async () => {
    if (!name || !row.url.trim()) {
      setError('set a server name and url first');
      setStatus('error');
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const { authorizeUrl } = await startMcpLogin(name, row.url.trim(), oauthArgs());
      if (authorizeUrl) popupRef.current = window.open(authorizeUrl, '_blank', 'noopener,noreferrer');
      setStatus('pending');
      abortRef.current = new AbortController();
      const result = await pollMcpLogin(name, abortRef.current.signal);
      setStatus(result.status);
      setError(result.error ?? null);
      if (result.status === 'signed_in') onReprobe();
    } catch (e) {
      setStatus('error');
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      abortRef.current = null;
      try {
        popupRef.current?.close();
      } catch {
        /* cross-origin or already closed */
      }
      popupRef.current = null;
    }
  };

  const onCancel = async () => {
    abortRef.current?.abort();
    await cancelMcpLogin(name);
    setStatus('signed_out');
    setError(null);
    setBusy(false);
  };

  const onSignOut = async () => {
    setBusy(true);
    try {
      await mcpLogout(name);
      setStatus('signed_out');
      setError(null);
      onReprobe();
    } finally {
      setBusy(false);
    }
  };

  // The probe is the source of truth for whether sign-in is actually required.
  // When the server answered 401 and we hold no usable token, surface a loud,
  // unmissable prompt instead of the quiet "not authorized" line.
  const needsAuth = probe?.status === 'needs_auth' && status !== 'signed_in' && status !== 'pending';

  if (needsAuth) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
          padding: '10px 12px',
          border: '1px solid var(--state-warn, #b5852a)',
          borderRadius: 4,
          background: 'color-mix(in srgb, var(--state-warn, #b5852a) 10%, transparent)',
        }}
      >
        <span className="serif" style={{ fontSize: 12.5, color: 'var(--ink-2)' }}>
          this server requires sign-in to use its tools.
          {status === 'error' && error && (
            <span style={{ color: 'var(--state-err, #b04030)', fontStyle: 'italic' }}> {error}</span>
          )}
        </span>
        <button
          type="button"
          onClick={onSignIn}
          disabled={busy}
          className="serif"
          style={{
            marginLeft: 'auto',
            background: 'var(--ink)',
            color: 'var(--paper)',
            border: 0,
            padding: '6px 16px',
            borderRadius: 3,
            fontSize: 12.5,
            cursor: busy ? 'default' : 'pointer',
            opacity: busy ? 0.6 : 1,
          }}
        >
          {status === 'error' ? 're-authenticate' : 'log in'} →
        </button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
      <span className="serif" style={{ fontSize: 12.5, color: 'var(--ink-3)' }}>
        {status === 'signed_in' && <span style={{ color: 'var(--state-ok, #3a7d44)' }}>authorized.</span>}
        {status === 'pending' && <span style={{ fontStyle: 'italic' }}>waiting for browser authorization…</span>}
        {status === 'error' && (
          <span style={{ fontStyle: 'italic', color: 'var(--state-err, #b04030)' }}>{error || 'authorization failed.'}</span>
        )}
        {status === 'signed_out' && <span style={{ fontStyle: 'italic' }}>not authorized.</span>}
      </span>
      {status === 'signed_in' ? (
        <button className="text-btn" type="button" onClick={onSignOut} disabled={busy} style={{ marginLeft: 'auto' }}>
          log out
        </button>
      ) : status === 'pending' ? (
        <button className="text-btn" type="button" onClick={onCancel} style={{ marginLeft: 'auto' }}>
          cancel
        </button>
      ) : (
        <button className="text-btn" type="button" onClick={onSignIn} disabled={busy} style={{ marginLeft: 'auto' }}>
          {status === 'error' ? 're-authenticate' : 'log in'} →
        </button>
      )}
    </div>
  );
}

function McpServerCard({
  row,
  probe,
  onReprobe,
  onPatch,
  onRemove,
  draft,
}: {
  row: McpRow;
  probe?: McpServerProbe;
  onReprobe: () => void;
  onPatch: (fields: Partial<McpRow>) => void;
  onRemove?: () => void;
  draft?: { error: string | null; onAdd: () => void; onCancel: () => void };
}) {
  return (
    <div
      style={{
        border: draft ? '1px solid var(--ink-3)' : '1px solid var(--rule)',
        borderRadius: 4,
        padding: '14px 14px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        opacity: !draft && !row.enabled ? 0.6 : 1,
      }}
    >
      {draft && (
        <div className="smallcaps" style={{ color: 'var(--ink-4)' }}>
          new server
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <input
          className="mono"
          value={row.name}
          placeholder="server name"
          onChange={(e) => onPatch({ name: e.target.value })}
          autoComplete="off"
          spellCheck={false}
          style={{ ...mcpInputStyle, flex: 1 }}
          aria-label="mcp server name"
        />
        <TypeToggle value={row.type} onChange={(type) => onPatch({ type })} />
        {!draft && onRemove && (
          <button
            className="text-btn text-btn--danger"
            type="button"
            onClick={onRemove}
            title="remove server"
          >
            remove
          </button>
        )}
      </div>

      {!draft && <McpStatusRow probe={probe} />}

      {row.type === 'local' ? (
        <>
          <SubField label="command">
            <input
              className="mono"
              value={row.command}
              placeholder="npx -y @playwright/mcp@latest"
              onChange={(e) => onPatch({ command: e.target.value })}
              autoComplete="off"
              spellCheck={false}
              style={mcpInputStyle}
              aria-label="mcp command"
            />
          </SubField>
          <SubField label="environment">
            <KeyValueRows
              rows={row.env}
              onChange={(env) => onPatch({ env })}
              keyPlaceholder="API_KEY"
              valuePlaceholder="value"
              addLabel="+ add variable"
            />
          </SubField>
        </>
      ) : (
        <>
          <SubField label="url">
            <input
              className="mono"
              value={row.url}
              placeholder="https://example.com/mcp"
              onChange={(e) => onPatch({ url: e.target.value })}
              autoComplete="off"
              spellCheck={false}
              style={mcpInputStyle}
              aria-label="mcp url"
            />
          </SubField>
          <SubField label="headers">
            <KeyValueRows
              rows={row.headers}
              onChange={(headers) => onPatch({ headers })}
              keyPlaceholder="Authorization"
              valuePlaceholder="Bearer ..."
              addLabel="+ add header"
            />
          </SubField>
          {row.oauthOn && <McpOAuthClientFields row={row} onPatch={onPatch} />}
          {!draft && row.oauthOn && <McpOAuthControl row={row} probe={probe} onReprobe={onReprobe} />}
        </>
      )}

      {draft ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
          <button
            className="text-btn"
            type="button"
            onClick={draft.onAdd}
            disabled={!!draft.error}
            title={draft.error ?? 'add this server'}
            style={{ fontWeight: 600 }}
          >
            add server
          </button>
          <button className="text-btn" type="button" onClick={draft.onCancel}>
            cancel
          </button>
          {draft.error && (
            <span className="serif" style={{ fontSize: 12, fontStyle: 'italic', color: 'var(--ink-4)' }}>
              {draft.error}
            </span>
          )}
        </div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', gap: 18, flexWrap: 'wrap' }}>
          <EnabledToggle value={row.enabled} onChange={(v) => onPatch({ enabled: v })} />
        </div>
      )}
    </div>
  );
}

function EnabledToggle({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  const trackOn = 'var(--state-ok, #3a7d44)';
  const trackOff = 'var(--ink-5, #b8b3a8)';
  return (
    <button
      type="button"
      role="switch"
      aria-checked={value}
      onClick={() => onChange(!value)}
      className="serif"
      title={value ? 'click to disable this server' : 'click to enable this server'}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 10,
        padding: 0,
        border: 0,
        background: 'transparent',
        color: value ? 'var(--ink-2)' : 'var(--ink-4)',
        cursor: 'pointer',
        fontStyle: 'italic',
        fontSize: 12.5,
      }}
    >
      <span
        aria-hidden
        style={{
          position: 'relative',
          width: 32,
          height: 18,
          borderRadius: 999,
          background: value ? trackOn : trackOff,
          transition: 'background 120ms ease',
          flex: '0 0 auto',
        }}
      >
        <span
          style={{
            position: 'absolute',
            top: 2,
            left: value ? 16 : 2,
            width: 14,
            height: 14,
            borderRadius: '50%',
            background: 'var(--paper, #fff)',
            boxShadow: '0 1px 2px rgba(0,0,0,0.25)',
            transition: 'left 120ms ease',
          }}
        />
      </span>
    </button>
  );
}

function TypeToggle({ value, onChange }: { value: McpType; onChange: (t: McpType) => void }) {
  const opt = (t: McpType, label: string) => (
    <button
      type="button"
      onClick={() => onChange(t)}
      aria-pressed={value === t}
      className="serif"
      style={{
        background: value === t ? 'var(--ink)' : 'transparent',
        color: value === t ? 'var(--paper)' : 'var(--ink-3)',
        border: 0,
        padding: '4px 12px',
        fontSize: 12.5,
        fontStyle: 'italic',
        cursor: 'pointer',
        borderRadius: 3,
      }}
    >
      {label}
    </button>
  );
  return (
    <div
      style={{
        display: 'inline-flex',
        gap: 2,
        padding: 2,
        border: '1px solid var(--rule)',
        borderRadius: 4,
        flexShrink: 0,
      }}
    >
      {opt('local', 'local')}
      {opt('remote', 'remote')}
    </div>
  );
}

function SubField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label
        className="smallcaps"
        style={{ display: 'block', marginBottom: 4, fontSize: 10.5, color: 'var(--ink-4)' }}
      >
        {label}
      </label>
      {children}
    </div>
  );
}

function ModelField({
  label, value, onChange, hint, settings,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint?: string;
  /** Settings snapshot forwarded to ModelInput so the autocomplete list
   *  refreshes when the active provider changes in the panel — before the
   *  user clicks Save. */
  settings?: Settings;
}) {
  return (
    <div>
      <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
        {label}
      </label>
      <ModelInput
        value={value}
        onChange={onChange}
        ariaLabel={label}
        settings={settings}
      />
      {hint && (
        <div
          className="serif"
          style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--ink-4)', marginTop: 6 }}
        >
          {hint}
        </div>
      )}
    </div>
  );
}
