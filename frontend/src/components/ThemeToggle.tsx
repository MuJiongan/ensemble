import type { Theme } from '../theme';

function SunIcon() {
  return (
    <svg className="theme-toggle__icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      <circle cx="8" cy="8" r="3" fill="currentColor" />
      <g stroke="currentColor" strokeWidth="1.2" strokeLinecap="round">
        <line x1="8" y1="1.4" x2="8" y2="3.2" />
        <line x1="8" y1="12.8" x2="8" y2="14.6" />
        <line x1="1.4" y1="8" x2="3.2" y2="8" />
        <line x1="12.8" y1="8" x2="14.6" y2="8" />
        <line x1="3.4" y1="3.4" x2="4.7" y2="4.7" />
        <line x1="11.3" y1="11.3" x2="12.6" y2="12.6" />
        <line x1="11.3" y1="4.7" x2="12.6" y2="3.4" />
        <line x1="3.4" y1="12.6" x2="4.7" y2="11.3" />
      </g>
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="theme-toggle__icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      <path
        d="M8.2 2.2a4.2 4.2 0 0 0 6.1 6.1A6.2 6.2 0 1 1 8.2 2.2Z"
        fill="currentColor"
      />
    </svg>
  );
}

export function ThemeToggle({
  theme,
  onChange,
}: {
  theme: Theme;
  onChange: (theme: Theme) => void;
}) {
  const isDark = theme === 'dark';

  return (
    <button
      type="button"
      className={`theme-toggle ${isDark ? 'is-dark' : ''}`}
      role="switch"
      aria-checked={isDark}
      aria-label={isDark ? 'switch to light mode' : 'switch to dark mode'}
      title={isDark ? 'light mode' : 'dark mode'}
      onClick={() => onChange(isDark ? 'light' : 'dark')}
    >
      <span className="theme-toggle__glyphs">
        <span className="theme-toggle__glyph theme-toggle__glyph--sun">
          <SunIcon />
        </span>
        <span className="theme-toggle__glyph theme-toggle__glyph--moon">
          <MoonIcon />
        </span>
      </span>
    </button>
  );
}
