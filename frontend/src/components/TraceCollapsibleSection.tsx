import type { ReactNode } from 'react';

/** Secondary trace block — collapsed by default so inputs/outputs stay
 * visually primary. Pass `open` to pin expanded (e.g. while streaming). */
export function TraceCollapsibleSection({
  title,
  hint,
  open = false,
  className,
  children,
}: {
  title: string;
  hint?: string;
  open?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <details className={`trace-fold${className ? ` ${className}` : ''}`} open={open}>
      <summary className="trace-fold__summary">
        <span className="trace-fold__lead">
          <span className="trace-fold__chevron" aria-hidden>▸</span>
          <span className="smallcaps trace-fold__title">{title}</span>
        </span>
        {hint && <span className="trace-fold__hint">{hint}</span>}
      </summary>
      <div className="trace-fold__body">{children}</div>
    </details>
  );
}
