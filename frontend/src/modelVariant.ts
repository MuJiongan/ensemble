/**
 * Reasoning-variant resolution + cycling.
 *
 * Ported from opencode's `packages/app/src/context/model-variant.ts`. A model
 * exposes an ordered list of variant names (weakest→strongest, e.g.
 * low/medium/high/max). `null` means "off" (no reasoning toggle). The variant
 * pill cycles off → first → … → last → off.
 */

/** Resolve the effective variant: the explicit selection if valid, else the
 * model's configured/default variant if valid, else off. */
export function resolveVariant(
  variants: string[],
  selected: string | null | undefined,
  configured?: string | null,
): string | null {
  if (selected === null) return null;
  if (selected && variants.includes(selected)) return selected;
  if (configured && variants.includes(configured)) return configured;
  return null;
}

/** Next variant when the pill is clicked. off → first → … → last → off. */
export function cycleVariant(variants: string[], selected: string | null): string | null {
  if (variants.length === 0) return null;
  if (selected === null || selected === undefined) return variants[0];
  const i = variants.indexOf(selected);
  if (i === -1) return variants[0];
  if (i === variants.length - 1) return null;
  return variants[i + 1];
}

/** Human label for a variant value, including the off state. */
export function variantLabel(selected: string | null): string {
  return selected ?? 'off';
}
