import { Children, useState } from 'react';
import { FileViewerOverlay } from './FileViewerOverlay';

/**
 * Shared file-path affordances. A path shown anywhere in the UI — an I/O value,
 * a JSON leaf, prose or inline code in chat — becomes a `FilePathLink` that
 * opens the file in `FileViewerOverlay`. Backend resolution is the arbiter:
 * a path-shaped string that isn't a real file just 404s in the viewer.
 */

/**
 * Whether a *complete* string value is worth offering as a clickable path.
 * Permissive — for whole-value contexts (a port value, a JSON string leaf)
 * where the surroundings already imply "this is a path". Pass `force` when the
 * context guarantees it (e.g. a port whose type_hint is `path`).
 */
export function looksLikePath(value: unknown, force = false): value is string {
  if (typeof value !== 'string') return false;
  const t = value.trim();
  if (force) return t.length > 0;
  if (!t || t.includes('\n') || t.length > 1024) return false;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(t)) return false; // url scheme
  if (/^(\/|~\/|\.\.?\/)/.test(t)) return true; // absolute / home / relative
  return /\/[^/\s]+\.[A-Za-z0-9]{1,8}$/.test(t); // .../file.ext
}

// Stricter matcher for paths *embedded in prose*: must contain a slash and end
// in a file extension, so we don't linkify "and/or" or "1/2" mid-sentence.
const PROSE_PATH_RE = /(?:~|\.\.?)?\/?[^\s'"`<>(){}]*\/[^\s'"`<>(){}]*\.[A-Za-z0-9]{1,8}/g;

/** Split a prose string into text + `FilePathLink` for any path-like tokens. */
export function linkifyText(text: unknown): React.ReactNode {
  if (typeof text !== 'string' || !text.includes('/')) return text as React.ReactNode;
  const out: React.ReactNode[] = [];
  const re = new RegExp(PROSE_PATH_RE.source, 'g');
  let last = 0;
  let key = 0;
  let matched = false;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const start = m.index;
    // Trailing sentence punctuation isn't part of the path.
    const cleaned = m[0].replace(/[.,;:!?)\]}>'"]+$/, '');
    const before = start > 0 ? text[start - 1] : '';
    const isUrl = before === ':' || cleaned.startsWith('//') || /^[a-z][a-z0-9+.-]*:\/\//i.test(cleaned);
    if (!cleaned.includes('/') || isUrl) continue;
    if (start > last) out.push(text.slice(last, start));
    out.push(<FilePathLink key={`fp${key++}`} path={cleaned} className="file-path-link--mono" />);
    last = start + cleaned.length;
    matched = true;
  }
  if (!matched) return text;
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Apply `linkifyText` to the string children of a markdown element. */
export function linkifyNodes(children: React.ReactNode): React.ReactNode {
  return Children.map(children, (c) => (typeof c === 'string' ? linkifyText(c) : c));
}

/** Flatten a markdown element's children to plain text (for inline `code`). */
export function childText(children: React.ReactNode): string {
  return Children.toArray(children)
    .map((c) => (typeof c === 'string' || typeof c === 'number' ? String(c) : ''))
    .join('');
}

interface FilePathLinkProps {
  path: string;
  children?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}

export function FilePathLink({ path, children, className, style }: FilePathLinkProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={`file-path-link${className ? ` ${className}` : ''}`}
        style={style}
        title="view file"
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          setOpen(true);
        }}
      >
        {children ?? path}
      </button>
      {open && <FileViewerOverlay path={path} onClose={() => setOpen(false)} />}
    </>
  );
}
