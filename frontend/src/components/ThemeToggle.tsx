import type { Theme } from '../theme';

function SunIcon() {
  return (
    <svg className="theme-toggle__icon" width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <circle cx="7" cy="7" r="2.5" fill="currentColor" />
      <g stroke="currentColor" strokeWidth="1.1" strokeLinecap="round">
        <line x1="7" y1="1.2" x2="7" y2="2.8" />
        <line x1="7" y1="11.2" x2="7" y2="12.8" />
        <line x1="1.2" y1="7" x2="2.8" y2="7" />
        <line x1="11.2" y1="7" x2="12.8" y2="7" />
        <line x1="2.9" y1="2.9" x2="4.1" y2="4.1" />
        <line x1="9.9" y1="9.9" x2="11.1" y2="11.1" />
        <line x1="9.9" y1="4.1" x2="11.1" y2="2.9" />
        <line x1="2.9" y1="11.1" x2="4.1" y2="9.9" />
      </g>
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="theme-toggle__icon" width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <path
        d="M7.2 2.1a3.6 3.6 0 0 0 5.4 5.4a5.4 5.4 0 1 1-5.4-5.4Z"
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
      className="theme-toggle"
      role="switch"
      aria-checked={isDark}
      aria-label={isDark ? 'switch to light mode' : 'switch to dark mode'}
      title={isDark ? 'light mode' : 'dark mode'}
      onClick={() => onChange(isDark ? 'light' : 'dark')}
    >
      <span className="theme-toggle__track">
        <span className={`theme-toggle__thumb ${isDark ? 'theme-toggle__thumb--dark' : ''}`} />
        <span className={`theme-toggle__option ${!isDark ? 'is-active' : ''}`}>
          <SunIcon />
        </span>
        <span className={`theme-toggle__option ${isDark ? 'is-active' : ''}`}>
          <MoonIcon />
        </span>
      </span>
    </button>
  );
}
