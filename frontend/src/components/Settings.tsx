import { useEffect, useRef, useState } from 'react';
import type { Settings } from '../types';
import { loadSettings, saveSettings } from '../localSettings';
import {
  CUSTOM_PRESET_ID,
  PROVIDER_PRESETS,
  presetById,
  presetIdForUrl,
} from '../llmProviders';
import { ModelInput } from './ModelInput';

const EMPTY: Settings = {
  llm_api_keys: {},
  llm_base_url: '',
  parallel_api_key: '',
  default_orchestrator_model: '',
  default_node_model: '',
};

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<Settings>(EMPTY);
  const [saved, setSaved] = useState(false);
  const [revealKeys, setRevealKeys] = useState(false);
  // Tracks which preset is selected. Derived from the stored base URL on
  // load; kept in component state so the user can switch to "custom" and
  // type a new URL even before they've finished editing it.
  const [presetId, setPresetId] = useState<string>(PROVIDER_PRESETS[0].id);

  useEffect(() => {
    const loaded = loadSettings();
    setS(loaded);
    setPresetId(presetIdForUrl(loaded.llm_base_url));
  }, []);

  const onPresetChange = (id: string) => {
    setPresetId(id);
    if (id === CUSTOM_PRESET_ID) {
      // Leave llm_base_url as-is — the input becomes editable so the user
      // can paste their endpoint.
      return;
    }
    const preset = presetById(id);
    if (preset) setS((prev) => ({ ...prev, llm_base_url: preset.baseUrl }));
  };

  const currentPreset = presetId === CUSTOM_PRESET_ID ? null : presetById(presetId);

  const save = () => {
    saveSettings(s);
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

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
          <Field
            label={`${currentPreset?.label.toLowerCase() ?? 'custom'} api key`}
            value={s.llm_api_keys[presetId] ?? ''}
            onChange={(v) =>
              setS({ ...s, llm_api_keys: { ...s.llm_api_keys, [presetId]: v } })
            }
            secret={!revealKeys}
            hint={
              currentPreset
                ? `bearer token for ${currentPreset.label}. each provider keeps its own key — switch providers above to manage another.`
                : 'bearer token for the endpoint above. each provider (and the custom slot) keeps its own key.'
            }
          />
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
            value={s.default_orchestrator_model}
            onChange={(v) => setS({ ...s, default_orchestrator_model: v })}
            hint="used by the orchestrator. model id as the configured provider expects it (e.g. anthropic/claude-sonnet-4.5 on openrouter, gpt-4o on openai)."
          />
          <ModelField
            label="default node model"
            value={s.default_node_model}
            onChange={(v) => setS({ ...s, default_node_model: v })}
            hint="default for ctx.call_llm inside nodes when no model is specified."
          />

          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 8 }}>
            <button onClick={save} className="btn-ink">
              save <span className="italic-em">→</span>
            </button>
            {saved && (
              <span
                className="serif"
                style={{ fontStyle: 'italic', fontSize: 13, color: 'var(--state-ok)' }}
              >
                saved to this browser.
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

interface ProviderOption {
  id: string;
  label: string;
  /** Inline URL preview shown under the label in the row; null for custom. */
  baseUrl: string | null;
  /** External "get a key" link shown only when this option is selected. */
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
    ...PROVIDER_PRESETS.map((p) => ({
      id: p.id,
      label: p.label,
      baseUrl: p.baseUrl,
      keysUrl: p.keysUrl,
    })),
    { id: CUSTOM_PRESET_ID, label: 'Custom', baseUrl: null },
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
            className="mono"
            style={{
              fontSize: 11.5,
              color: 'var(--ink-4)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {selected.baseUrl ?? 'bring your own openai-compatible base url'}
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
                    className="mono"
                    style={{
                      fontSize: 11,
                      color: 'var(--ink-4)',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {o.baseUrl ?? 'bring your own openai-compatible base url'}
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

function ModelField({
  label, value, onChange, hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint?: string;
}) {
  return (
    <div>
      <label className="smallcaps" style={{ display: 'block', marginBottom: 6 }}>
        {label}
      </label>
      <ModelInput value={value} onChange={onChange} ariaLabel={label} />
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
