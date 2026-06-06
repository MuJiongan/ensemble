/**
 * Provider connection + model selection dialogs.
 *
 * React port of opencode's connect/select UX:
 *   - DialogSelectProvider  ← dialog-select-provider.tsx
 *   - DialogConnectProvider ← dialog-connect-provider.tsx (api key | oauth)
 *   - DialogSelectModel     ← dialog-select-model.tsx (+ variant cycling)
 *   - VariantPill           ← local model-variant pill
 *
 * The provider/model catalog comes from the backend (`providerCatalog.ts`);
 * connection state (api keys / oauth markers) lives in localStorage Settings.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ProviderConnection, Settings } from '../types';
import type { Catalog, CatalogProvider, CatalogModel } from '../providerCatalog';
import { CUSTOM_PROVIDER, CUSTOM_PROVIDER_ID } from '../providerCatalog';
import { isConnected } from '../localSettings';
import { cycleVariant, variantLabel } from '../modelVariant';
import { startLogin, pollUntilDone, logout as oauthLogout } from '../auth';

// --- shared modal shell ----------------------------------------------------

function Modal({
  title,
  onClose,
  children,
  width = 560,
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
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return createPortal(
    <div
      onClick={onClose}
      className="fade-in"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(26, 23, 20, 0.45)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        alignItems: 'stretch',
        justifyContent: 'center',
        padding: '8vh 4vw',
        zIndex: 1100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="shadow-card"
        style={{
          flex: 1,
          maxWidth: width,
          maxHeight: '84vh',
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

const searchInputStyle: React.CSSProperties = {
  width: '100%',
  background: 'transparent',
  border: 0,
  borderBottom: '1px solid var(--rule)',
  padding: '8px 0',
  fontSize: 13,
  color: 'var(--ink)',
  outline: 'none',
  marginBottom: 12,
};

const rowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  width: '100%',
  textAlign: 'left',
  background: 'transparent',
  border: 0,
  borderBottom: '1px solid var(--rule)',
  padding: '11px 2px',
  cursor: 'pointer',
  color: 'var(--ink)',
};

// --- DialogSelectProvider --------------------------------------------------

export function DialogSelectProvider({
  catalog,
  settings,
  onPick,
  onClose,
}: {
  catalog: Catalog;
  settings: Settings;
  onPick: (provider: CatalogProvider) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => ref.current?.focus(), []);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = catalog.providers.filter(
      (p) => !needle || p.name.toLowerCase().includes(needle) || p.id.includes(needle),
    );
    const popular = list.filter((p) => p.popular);
    const rest = list.filter((p) => !p.popular);
    return { popular, rest };
  }, [catalog, q]);

  const renderRow = (p: CatalogProvider) => {
    const connected = isConnected(settings, p.id);
    return (
      <button key={p.id} style={rowStyle} onClick={() => onPick(p)}>
        <span className="serif" style={{ fontSize: 14, flex: 1 }}>
          {p.name}
        </span>
        {connected && (
          <span className="smallcaps" style={{ color: 'var(--accent, #3a7)' }}>
            connected
          </span>
        )}
        <span className="mono" style={{ fontSize: 11, color: 'var(--ink-4)' }}>
          {p.models.length} models
        </span>
      </button>
    );
  };

  return (
    <Modal title="connect a provider" onClose={onClose}>
      <input
        ref={ref}
        className="mono"
        placeholder="search providers…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        style={searchInputStyle}
      />
      {filtered.popular.length > 0 && (
        <>
          <div className="smallcaps" style={{ color: 'var(--ink-4)', margin: '4px 0 2px' }}>
            popular
          </div>
          {filtered.popular.map(renderRow)}
        </>
      )}
      {filtered.rest.length > 0 && (
        <>
          <div className="smallcaps" style={{ color: 'var(--ink-4)', margin: '14px 0 2px' }}>
            all providers
          </div>
          {filtered.rest.map(renderRow)}
        </>
      )}
      {filtered.popular.length === 0 && filtered.rest.length === 0 && (
        <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)', padding: 12 }}>
          no providers match.
        </div>
      )}
      <button
        style={{ ...rowStyle, marginTop: 14, borderBottom: 0 }}
        onClick={() => onPick(CUSTOM_PROVIDER)}
      >
        <span className="serif" style={{ fontSize: 14, flex: 1 }}>
          + Custom OpenAI-compatible endpoint
        </span>
        {isConnected(settings, CUSTOM_PROVIDER_ID) && (
          <span className="smallcaps" style={{ color: 'var(--accent, #3a7)' }}>
            connected
          </span>
        )}
      </button>
    </Modal>
  );
}

// --- DialogConnectProvider -------------------------------------------------

export function DialogConnectProvider({
  provider,
  settings,
  onConnect,
  onDisconnect,
  onClose,
}: {
  provider: CatalogProvider;
  settings: Settings;
  onConnect: (providerID: string, conn: ProviderConnection) => void;
  onDisconnect: (providerID: string) => void;
  onClose: () => void;
}) {
  const methods = provider.auth.length ? provider.auth : [{ type: 'api' as const, label: 'API Key' }];
  const [methodIdx, setMethodIdx] = useState<number>(methods.length === 1 ? 0 : -1);
  const [apiKey, setApiKey] = useState(settings.connections[provider.id]?.apiKey ?? '');
  const isCustom = provider.id === CUSTOM_PROVIDER_ID;
  const [baseUrl, setBaseUrl] = useState(
    settings.connections[provider.id]?.baseURL ?? provider.base_url ?? '',
  );
  const [oauthState, setOauthState] = useState<'idle' | 'pending' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const connected = isConnected(settings, provider.id);
  const method = methodIdx >= 0 ? methods[methodIdx] : null;

  const doApiConnect = () => {
    if (!apiKey.trim()) {
      setError('enter an API key');
      return;
    }
    if (isCustom && !baseUrl.trim()) {
      setError('enter a base URL');
      return;
    }
    onConnect(provider.id, {
      method: 'api',
      apiKey: apiKey.trim(),
      baseURL: (isCustom ? baseUrl.trim() : provider.base_url) ?? undefined,
    });
    onClose();
  };

  const doOAuth = async (authProviderId: string) => {
    setError(null);
    setOauthState('pending');
    abortRef.current = new AbortController();
    try {
      const { authorizeUrl } = await startLogin(authProviderId);
      window.open(authorizeUrl, '_blank', 'noopener,noreferrer,width=520,height=720');
      const res = await pollUntilDone(authProviderId, abortRef.current.signal);
      if (res.status === 'signed_in') {
        onConnect(provider.id, { method: 'oauth' });
        onClose();
      } else {
        setOauthState('error');
        setError(res.error || 'sign-in failed');
      }
    } catch (e) {
      setOauthState('error');
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  return (
    <Modal title={`connect ${provider.name.toLowerCase()}`} onClose={onClose}>
      {connected && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            marginBottom: 16,
            paddingBottom: 14,
            borderBottom: '1px solid var(--rule)',
          }}
        >
          <span className="smallcaps" style={{ color: 'var(--accent, #3a7)', flex: 1 }}>
            connected
          </span>
          <button
            className="text-btn"
            onClick={async () => {
              const conn = settings.connections[provider.id];
              if (conn?.method === 'oauth') {
                const oauth = provider.auth.find((m) => m.type === 'oauth');
                if (oauth?.provider) await oauthLogout(oauth.provider);
              }
              onDisconnect(provider.id);
              onClose();
            }}
          >
            disconnect
          </button>
        </div>
      )}

      {methods.length > 1 && method === null && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div className="smallcaps" style={{ color: 'var(--ink-4)', marginBottom: 4 }}>
            choose a sign-in method
          </div>
          {methods.map((m, i) => (
            <button key={i} style={rowStyle} onClick={() => setMethodIdx(i)}>
              <span className="serif" style={{ fontSize: 14, flex: 1 }}>
                {m.label}
              </span>
              <span style={{ color: 'var(--ink-4)' }}>→</span>
            </button>
          ))}
        </div>
      )}

      {method?.type === 'api' && (
        <div>
          {isCustom && (
            <>
              <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
                base url
              </label>
              <input
                className="mono"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-endpoint.example.com/v1"
                autoComplete="off"
                spellCheck={false}
                style={{ ...searchInputStyle, marginBottom: 14 }}
              />
            </>
          )}
          <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
            {provider.name.toLowerCase()} api key
          </label>
          <input
            className="mono"
            type="password"
            autoFocus
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && doApiConnect()}
            placeholder="paste your bearer token"
            autoComplete="off"
            spellCheck={false}
            style={{ ...searchInputStyle, marginBottom: 6 }}
          />
          {provider.base_url && (
            <div
              className="serif"
              style={{ fontStyle: 'italic', fontSize: 12, color: 'var(--ink-4)', marginBottom: 14 }}
            >
              requests go to <span className="mono">{provider.base_url}</span>
            </div>
          )}
          {!provider.executable && (
            <div
              className="serif"
              style={{ fontStyle: 'italic', fontSize: 12, color: '#b5651d', marginBottom: 14 }}
            >
              note: {provider.name} isn't reachable over an OpenAI-compatible endpoint yet — use it
              via OpenRouter for now.
            </div>
          )}
          <button className="text-btn" onClick={doApiConnect}>
            connect
          </button>
        </div>
      )}

      {method?.type === 'oauth' && (
        <div>
          {oauthState === 'pending' ? (
            <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-3)' }}>
              waiting for sign-in in the popup…{' '}
              <button
                className="text-btn"
                onClick={() => {
                  abortRef.current?.abort();
                  setOauthState('idle');
                }}
              >
                cancel
              </button>
            </div>
          ) : (
            <button className="text-btn" onClick={() => doOAuth(method.provider || provider.id)}>
              {method.label.toLowerCase()}
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="serif" style={{ color: '#b00', fontSize: 12, marginTop: 12 }}>
          {error}
        </div>
      )}
    </Modal>
  );
}

// --- DialogSelectModel -----------------------------------------------------

export function DialogSelectModel({
  catalog,
  settings,
  onPick,
  onClose,
}: {
  catalog: Catalog;
  settings: Settings;
  onPick: (sel: { providerID: string; modelID: string; variant: string | null }) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const [customModel, setCustomModel] = useState('');
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => ref.current?.focus(), []);

  const customConnected = isConnected(settings, CUSTOM_PROVIDER_ID);
  const connectedProviders = useMemo(
    () => catalog.providers.filter((p) => isConnected(settings, p.id)),
    [catalog, settings],
  );

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return connectedProviders
      .map((p) => ({
        provider: p,
        models: p.models.filter(
          (m) => !needle || m.name.toLowerCase().includes(needle) || m.id.toLowerCase().includes(needle),
        ),
      }))
      .filter((g) => g.models.length > 0);
  }, [connectedProviders, q]);

  const pick = (p: CatalogProvider, m: CatalogModel) =>
    onPick({ providerID: p.id, modelID: m.id, variant: m.default_variant });

  const customRow = customConnected && (
    <div style={{ marginBottom: 12 }}>
      <div className="smallcaps" style={{ color: 'var(--ink-4)', margin: '8px 0 4px' }}>
        Custom endpoint
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          className="mono"
          placeholder="model id (e.g. my-model)"
          value={customModel}
          onChange={(e) => setCustomModel(e.target.value)}
          onKeyDown={(e) =>
            e.key === 'Enter' &&
            customModel.trim() &&
            onPick({ providerID: CUSTOM_PROVIDER_ID, modelID: customModel.trim(), variant: null })
          }
          style={{ ...searchInputStyle, marginBottom: 0, flex: 1 }}
        />
        <button
          className="text-btn"
          disabled={!customModel.trim()}
          onClick={() =>
            onPick({ providerID: CUSTOM_PROVIDER_ID, modelID: customModel.trim(), variant: null })
          }
        >
          use
        </button>
      </div>
    </div>
  );

  return (
    <Modal title="select a model" onClose={onClose}>
      {connectedProviders.length === 0 && !customConnected ? (
        <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)', padding: 12 }}>
          connect a provider first.
        </div>
      ) : (
        <>
          <input
            ref={ref}
            className="mono"
            placeholder="search models…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={searchInputStyle}
          />
          {customRow}
          {groups.map((g) => (
            <div key={g.provider.id} style={{ marginBottom: 12 }}>
              <div className="smallcaps" style={{ color: 'var(--ink-4)', margin: '8px 0 2px' }}>
                {g.provider.name}
              </div>
              {g.models.slice(0, 60).map((m) => (
                <button key={m.id} style={rowStyle} onClick={() => pick(g.provider, m)}>
                  <span className="serif" style={{ fontSize: 14, flex: 1 }}>
                    {m.name}
                  </span>
                  {m.reasoning && m.variants.length > 0 && (
                    <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>
                      reasoning
                    </span>
                  )}
                  <span className="mono" style={{ fontSize: 11, color: 'var(--ink-4)' }}>
                    {m.id}
                  </span>
                </button>
              ))}
            </div>
          ))}
          {groups.length === 0 && (
            <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)', padding: 12 }}>
              no models match.
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

// --- VariantPill -----------------------------------------------------------

export function VariantPill({
  variants,
  selected,
  onChange,
}: {
  variants: string[];
  selected: string | null;
  onChange: (next: string | null) => void;
}) {
  if (variants.length === 0) return null;
  return (
    <button
      className="mono"
      title="cycle reasoning effort"
      onClick={() => onChange(cycleVariant(variants, selected))}
      style={{
        fontSize: 11,
        padding: '2px 8px',
        borderRadius: 999,
        border: '1px solid var(--rule)',
        background: selected ? 'var(--paper-2)' : 'transparent',
        color: selected ? 'var(--ink)' : 'var(--ink-4)',
        cursor: 'pointer',
      }}
    >
      reasoning: {variantLabel(selected)}
    </button>
  );
}
