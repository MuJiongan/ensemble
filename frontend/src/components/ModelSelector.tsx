import { useEffect, useMemo, useRef, useState } from 'react';
import { isConnected, loadSettings } from '../localSettings';
import { variantLabel } from '../modelVariant';
import {
  CUSTOM_PROVIDER_ID,
  findModel,
  findProvider,
  type Catalog,
  type CatalogModel,
  type CatalogProvider,
} from '../providerCatalog';
import type { ModelSelection, Settings } from '../types';

type Align = 'left' | 'right';

export function ModelSelector({
  catalog,
  settings,
  selection,
  onChange,
  disabled,
  compact,
  align = 'left',
}: {
  catalog: Catalog | null;
  settings?: Settings;
  selection: ModelSelection | null;
  onChange: (sel: ModelSelection) => void;
  disabled?: boolean;
  compact?: boolean;
  align?: Align;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [customModel, setCustomModel] = useState('');
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const currentSettings = settings ?? loadSettings();

  const provider = catalog && selection ? findProvider(catalog, selection.providerID) : undefined;
  const model = catalog && selection ? findModel(catalog, selection.providerID, selection.modelID) : undefined;
  const variants = model?.variants ?? [];
  const selectedVariant =
    selection?.variant && variants.includes(selection.variant) ? selection.variant : null;
  const triggerModel = model?.name ?? selection?.modelID ?? 'select model';
  const triggerProvider = provider?.name ?? selection?.providerID ?? '';
  const triggerVariant = variants.length > 0 ? variantLabel(selectedVariant) : null;

  useEffect(() => {
    if (!open) return;
    const id = requestAnimationFrame(() => searchRef.current?.focus());
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    window.addEventListener('keydown', onKey);
    return () => {
      cancelAnimationFrame(id);
      document.removeEventListener('mousedown', onDoc);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const connectedProviders = useMemo(
    () => catalog?.providers.filter((p) => isConnected(currentSettings, p.id)) ?? [],
    [catalog, currentSettings],
  );

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return connectedProviders
      .map((p) => ({
        provider: p,
        models: p.models.filter(
          (m) =>
            !needle ||
            m.name.toLowerCase().includes(needle) ||
            m.id.toLowerCase().includes(needle),
        ),
      }))
      .filter((g) => g.models.length > 0);
  }, [connectedProviders, query]);

  const pick = (p: CatalogProvider, m: CatalogModel) => {
    onChange({ providerID: p.id, modelID: m.id, variant: m.default_variant });
    setOpen(false);
  };

  const pickCustom = () => {
    const modelID = customModel.trim();
    if (!modelID) return;
    onChange({ providerID: CUSTOM_PROVIDER_ID, modelID, variant: null });
    setOpen(false);
  };

  const setVariant = (variant: string | null) => {
    if (!selection) return;
    onChange({ ...selection, variant });
  };

  return (
    <span
      ref={rootRef}
      style={{
        position: 'relative',
        display: 'inline-flex',
        minWidth: 0,
        width: compact ? 'auto' : '100%',
      }}
    >
      <button
        type="button"
        className={compact ? 'text-btn' : undefined}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        title="select model and thinking effort"
        style={compact ? {
          display: 'inline-flex',
          alignItems: 'baseline',
          gap: 7,
          minWidth: 0,
          maxWidth: 280,
          padding: '2px 0',
          textTransform: 'none',
          letterSpacing: 0,
        } : {
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 16,
          width: '100%',
          minWidth: 0,
          padding: '9px 0 10px',
          background: 'transparent',
          border: 0,
          borderBottom: '1px solid var(--rule)',
          borderRadius: 0,
          boxShadow: 'none',
          cursor: 'pointer',
          textAlign: 'left',
          color: 'var(--ink)',
        }}
      >
        {compact ? (
          <>
            <span
              className="mono"
              style={{
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontSize: 11.5,
                color: selection ? 'var(--ink-3)' : 'var(--ink-4)',
              }}
            >
              {triggerModel}
            </span>
            {triggerVariant && (
              <>
                <span aria-hidden style={{ color: 'var(--ink-5)', flexShrink: 0 }}>
                  /
                </span>
                <span
                  className="serif"
                  style={{
                    flexShrink: 0,
                    color: 'var(--ink-2)',
                    fontSize: 13.5,
                    fontStyle: 'italic',
                  }}
                >
                  {triggerVariant}
                </span>
              </>
            )}
            <span aria-hidden style={{ color: 'var(--ink-4)', flexShrink: 0, fontSize: 9 }}>
              ▾
            </span>
          </>
        ) : (
          <>
            <span
              style={{
                minWidth: 0,
                display: 'flex',
                flexDirection: 'column',
                gap: 2,
              }}
            >
              {triggerProvider && (
                <span className="smallcaps" style={{ color: 'var(--ink-4)', fontSize: 9 }}>
                  {triggerProvider}
                </span>
              )}
              <span
                className={selection ? 'mono' : 'serif'}
                style={{
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  fontSize: selection ? 13 : 13.5,
                  fontStyle: selection ? undefined : 'italic',
                  color: selection ? 'var(--ink-2)' : 'var(--ink-4)',
                }}
              >
                {triggerModel}
              </span>
            </span>
            <span
              style={{
                flexShrink: 0,
                display: 'inline-flex',
                alignItems: 'center',
                gap: 10,
                color: 'var(--ink-4)',
              }}
            >
              {triggerVariant && (
                <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 6 }}>
                  <span className="smallcaps" style={{ color: 'var(--ink-4)', fontSize: 9 }}>
                    effort
                  </span>
                  <span className="serif" style={{ color: 'var(--ink-3)', fontStyle: 'italic', fontSize: 14 }}>
                    {triggerVariant}
                  </span>
                </span>
              )}
              <span className="smallcaps" style={{ color: 'var(--accent-ink)', fontSize: 9 }}>
                change
              </span>
              <span aria-hidden style={{ color: 'var(--ink-4)', fontSize: 9 }}>
                ▾
              </span>
            </span>
          </>
        )}
      </button>

      {open && (
        <div
          className="shadow-card fade-in"
          style={{
            position: 'absolute',
            top: 'calc(100% + 7px)',
            [align]: 0,
            width: 390,
            maxWidth: 'min(390px, calc(100vw - 28px))',
            maxHeight: 470,
            overflow: 'hidden',
            border: '1px solid var(--rule)',
            borderRadius: 4,
            background: 'var(--paper)',
            zIndex: 500,
          }}
        >
          <div style={{ padding: 12, borderBottom: '1px solid var(--rule)' }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 10,
                marginBottom: selection && variants.length > 0 ? 9 : 0,
              }}
            >
              <span className="mono" style={{ minWidth: 0, color: 'var(--ink-3)', fontSize: 11, flex: 1 }}>
                {provider?.name ?? selection?.providerID ?? 'no provider'}
                {selection ? ` / ${model?.name ?? selection.modelID}` : ''}
              </span>
            </div>
            {selection && variants.length > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span className="smallcaps" style={{ color: 'var(--ink-4)', fontSize: 9, flexShrink: 0 }}>
                  effort
                </span>
                <span
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 3,
                    minWidth: 0,
                    flexWrap: 'wrap',
                  }}
                >
                  {[null, ...variants].map((variant) => {
                    const active = selectedVariant === variant;
                    return (
                      <button
                        key={variant ?? 'off'}
                        type="button"
                        onClick={() => setVariant(variant)}
                        className="text-btn"
                        style={{
                          width: 48,
                          height: 24,
                          padding: '1px 6px',
                          border: '1px solid var(--rule)',
                          borderRadius: 3,
                          background: active ? 'var(--surface-hover)' : 'transparent',
                          color: active ? 'var(--ink-2)' : 'var(--ink-4)',
                          letterSpacing: 0,
                          textTransform: 'none',
                          fontFamily: 'var(--serif)',
                          fontStyle: 'italic',
                          fontSize: 11.5,
                          display: 'inline-flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                        }}
                      >
                      {variantLabel(variant)}
                      </button>
                    );
                  })}
                </span>
              </div>
            )}
          </div>

          <div className="scroll" style={{ maxHeight: 365, overflow: 'auto', padding: 12 }}>
            {!catalog ? (
              <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)', padding: 8 }}>
                loading model catalog...
              </div>
            ) : connectedProviders.length === 0 && !isConnected(currentSettings, CUSTOM_PROVIDER_ID) ? (
              <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)', padding: 8 }}>
                connect a provider first.
              </div>
            ) : (
              <>
                <input
                  ref={searchRef}
                  className="field field--mono"
                  placeholder="search models..."
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  style={{ marginBottom: 12 }}
                />
                {isConnected(currentSettings, CUSTOM_PROVIDER_ID) && (
                  <div style={{ marginBottom: 14 }}>
                    <div className="smallcaps" style={{ color: 'var(--ink-4)', marginBottom: 5 }}>
                      custom endpoint
                    </div>
                    <div style={{ display: 'flex', gap: 8 }}>
                      <input
                        className="field field--mono"
                        placeholder="model id"
                        value={customModel}
                        onChange={(e) => setCustomModel(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') pickCustom();
                        }}
                        style={{ flex: 1 }}
                      />
                      <button
                        type="button"
                        className="text-btn text-btn--accent"
                        disabled={!customModel.trim()}
                        onClick={pickCustom}
                      >
                        use
                      </button>
                    </div>
                  </div>
                )}
                {groups.map((g) => (
                  <div key={g.provider.id} style={{ marginBottom: 12 }}>
                    <div className="smallcaps" style={{ color: 'var(--ink-4)', margin: '8px 0 2px' }}>
                      {g.provider.name}
                    </div>
                    {g.models.slice(0, 60).map((m) => {
                      const active = selection?.providerID === g.provider.id && selection.modelID === m.id;
                      return (
                        <button
                          key={m.id}
                          type="button"
                          onClick={() => pick(g.provider, m)}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 9,
                            width: '100%',
                            textAlign: 'left',
                            background: active ? 'var(--surface-hover)' : 'transparent',
                            border: 0,
                            borderBottom: '1px solid var(--rule)',
                            padding: '9px 2px',
                            cursor: 'pointer',
                            color: 'var(--ink)',
                          }}
                        >
                          <span className="serif" style={{ fontSize: 14, flex: 1, minWidth: 0 }}>
                            {m.name}
                          </span>
                          <span className="mono" style={{ fontSize: 10.5, color: 'var(--ink-4)' }}>
                            {m.id}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                ))}
                {groups.length === 0 && (
                  <div className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)', padding: 8 }}>
                    no models match.
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </span>
  );
}
