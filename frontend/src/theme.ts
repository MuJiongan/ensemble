export type Theme = 'light' | 'dark';

const KEY = 'orchestra:theme';
export const THEME_CHANGED_EVENT = 'orchestra:theme-changed';

export function loadTheme(): Theme {
  if (typeof window === 'undefined') return 'light';
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored === 'dark' || stored === 'light') return stored;
  } catch {
    /* ignore */
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function saveTheme(theme: Theme): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(KEY, theme);
  applyTheme(theme);
  window.dispatchEvent(new CustomEvent(THEME_CHANGED_EVENT));
}

export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  document.documentElement.dataset.theme = theme;
}

export function toggleTheme(): Theme {
  const next: Theme = loadTheme() === 'dark' ? 'light' : 'dark';
  saveTheme(next);
  return next;
}

/** Call once before React mounts to avoid a light flash on dark preference. */
export function initTheme(): void {
  applyTheme(loadTheme());
}
