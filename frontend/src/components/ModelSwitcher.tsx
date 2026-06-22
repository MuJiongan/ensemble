import { useState } from 'react';
import { DialogSelectModel, VariantPill } from './ProviderDialogs';
import { AlertDialog } from './ConfirmDialog';
import { loadSettings } from '../localSettings';
import type { Catalog } from '../providerCatalog';
import type { ModelSelection } from '../types';

/**
 * Compact model control for a chat header: shows the current model id (a ▾
 * caret signals it's a picker, not a label) plus a reasoning-variant pill, and
 * opens the same `DialogSelectModel` picker Settings uses. Self-contained — it
 * owns its picker dialog — so the orchestrator chat and a node's continuation
 * chat drop in the same control.
 */
export function ModelSwitcher({
  selection,
  variants,
  catalog,
  fallbackLabel,
  onPick,
  onCycleVariant,
}: {
  selection: ModelSelection | null;
  variants: string[];
  catalog: Catalog | null;
  /** Shown when there's no selection (e.g. the orchestrator's plain label). */
  fallbackLabel?: string;
  onPick: (sel: ModelSelection) => void;
  onCycleVariant?: (next: string | null) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [notReady, setNotReady] = useState(false);

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
      <button
        type="button"
        className="text-btn"
        title="click to switch model"
        onClick={() => (catalog ? setPicking(true) : setNotReady(true))}
        // Drop text-btn's uppercase/letterspacing so the model id reads
        // naturally; the ▾ caret signals it's a picker, not a label.
        style={{
          display: 'inline-flex',
          alignItems: 'baseline',
          gap: 4,
          minWidth: 0,
          textTransform: 'none',
          letterSpacing: 0,
        }}
      >
        <span
          className="mono"
          style={{
            fontSize: 10.5,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            maxWidth: 170,
            display: 'inline-block',
          }}
        >
          {selection?.modelID || fallbackLabel || '(default)'}
        </span>
        <span aria-hidden style={{ fontSize: 9, flexShrink: 0 }}>▾</span>
      </button>
      {onCycleVariant && variants.length > 0 && (
        <VariantPill
          variants={variants}
          selected={selection?.variant ?? null}
          onChange={onCycleVariant}
        />
      )}
      {picking && catalog && (
        <DialogSelectModel
          catalog={catalog}
          settings={loadSettings()}
          onPick={(sel) => {
            onPick({ providerID: sel.providerID, modelID: sel.modelID, variant: sel.variant });
            setPicking(false);
          }}
          onClose={() => setPicking(false)}
        />
      )}
      {notReady && (
        <AlertDialog
          message="Model list is still loading — try again in a moment."
          onClose={() => setNotReady(false)}
        />
      )}
    </span>
  );
}
