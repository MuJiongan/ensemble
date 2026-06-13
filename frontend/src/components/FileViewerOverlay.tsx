import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, ApiError } from '../api';
import type { FsFile } from '../types';
import { Markdown } from './Markdown';
import { CloseButton } from './CloseButton';

/**
 * Full-height side panel that resolves a file *path* to its contents and
 * renders it by type — text/code, markdown, html, image, pdf, directory.
 * Markdown and HTML get a rendered/source toggle; videos and opaque binaries
 * are reported rather than previewed.
 *
 * Distinct from ValueViewer's `ViewerOverlay`, which renders an in-memory value
 * the frontend already holds. Here the browser has only a path string, so the
 * backend (GET /api/files) reads the bytes — see app/api/files.py.
 */

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, '');
  const i = trimmed.lastIndexOf('/');
  return i >= 0 ? trimmed.slice(i + 1) || trimmed : trimmed;
}

function parentDir(path: string): string {
  const trimmed = path.replace(/\/+$/, '');
  const i = trimmed.lastIndexOf('/');
  if (i <= 0) return '/';
  return trimmed.slice(0, i);
}

function joinPath(dir: string, name: string): string {
  return `${dir.replace(/\/+$/, '')}/${name.replace(/\/+$/, '')}`;
}

const preStyle: React.CSSProperties = {
  fontSize: 12.5,
  lineHeight: 1.6,
  color: 'var(--ink-2)',
  margin: 0,
  padding: '16px 18px',
  background: 'var(--surface-raised)',
  border: '1px solid var(--rule)',
  borderRadius: 4,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

function NotPreviewable({ file, lead }: { file: FsFile; lead: string }) {
  return (
    <div
      className="serif"
      style={{ color: 'var(--ink-3)', fontStyle: 'italic', fontSize: 14, lineHeight: 1.6 }}
    >
      <p style={{ margin: '0 0 8px' }}>{lead}</p>
      <p className="mono" style={{ fontStyle: 'normal', fontSize: 11.5, color: 'var(--ink-4)' }}>
        {file.mime || 'unknown type'}
        {typeof file.size === 'number' ? ` · ${formatBytes(file.size)}` : ''}
      </p>
      {file.note && <p style={{ margin: '8px 0 0', fontSize: 12.5 }}>{file.note}</p>}
    </div>
  );
}

function DirectoryView({ file, onNavigate }: { file: FsFile; onNavigate: (p: string) => void }) {
  const entries = file.entries ?? [];
  return (
    <div>
      <button
        type="button"
        className="file-view__entry"
        onClick={() => onNavigate(parentDir(file.path))}
      >
        <span className="ed-btn__mark" aria-hidden>↑</span>
        <span className="mono">..</span>
      </button>
      {entries.map((e) => (
        <button
          key={e.name}
          type="button"
          className="file-view__entry"
          onClick={() => onNavigate(joinPath(file.path, e.name))}
        >
          <span className="ed-btn__mark" aria-hidden>{e.is_dir ? '📁' : '▤'}</span>
          <span className="mono">{e.name}{e.is_dir ? '/' : ''}</span>
        </button>
      ))}
      {entries.length === 0 && (
        <p className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)' }}>
          empty directory
        </p>
      )}
    </div>
  );
}

function FileBody({
  file,
  view,
  onNavigate,
}: {
  file: FsFile;
  view: 'rendered' | 'source';
  onNavigate: (p: string) => void;
}) {
  switch (file.kind) {
    case 'directory':
      return <DirectoryView file={file} onNavigate={onNavigate} />;

    case 'image':
      return (
        <img
          src={file.data_url}
          alt={file.name}
          style={{ maxWidth: '100%', height: 'auto', borderRadius: 4, display: 'block' }}
        />
      );

    case 'pdf':
      return (
        <iframe
          title={file.name}
          src={file.data_url}
          style={{ width: '100%', height: '78vh', border: '1px solid var(--rule)', borderRadius: 4 }}
        />
      );

    case 'video':
      return <NotPreviewable file={file} lead="Video files aren't previewable here." />;

    case 'binary':
      return <NotPreviewable file={file} lead="This file isn't a previewable text or image type." />;

    case 'markdown':
      return view === 'rendered'
        ? <Markdown large>{file.content ?? ''}</Markdown>
        : <pre className="mono" style={preStyle}>{file.content}</pre>;

    case 'html':
      return view === 'rendered'
        ? (
          <iframe
            title={file.name}
            sandbox=""
            srcDoc={file.content ?? ''}
            style={{ width: '100%', height: '78vh', border: '1px solid var(--rule)', borderRadius: 4, background: '#fff' }}
          />
        )
        : <pre className="mono" style={preStyle}>{file.content}</pre>;

    case 'text':
    default:
      return <pre className="mono" style={preStyle}>{file.content}</pre>;
  }
}

interface FileViewerOverlayProps {
  path: string;
  title?: string;
  subtitle?: string;
  onClose: () => void;
}

export function FileViewerOverlay({ path: initialPath, title, subtitle, onClose }: FileViewerOverlayProps) {
  const [path, setPath] = useState(initialPath);
  const [file, setFile] = useState<FsFile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<'rendered' | 'source'>('rendered');
  const [copied, setCopied] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setView('rendered');
    setActionMsg(null);
    api
      .readFile(path)
      .then((f) => {
        if (!cancelled) setFile(f);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setFile(null);
        setError(
          e instanceof ApiError && e.status === 404
            ? 'No file or directory at this path.'
            : e instanceof Error
              ? e.message
              : 'Could not open this file.',
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  const toggleable = file?.kind === 'markdown' || file?.kind === 'html';
  const copyableText = typeof file?.content === 'string';

  const onCopy = () => {
    const text = copyableText ? file!.content! : path;
    navigator.clipboard?.writeText(text ?? '').catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  const openExternally = (reveal: boolean) => {
    setActionMsg(null);
    api
      .openFileExternally(path, reveal)
      .catch(() => setActionMsg(reveal ? "couldn't reveal" : "couldn't open"));
  };

  const headerName = file ? basename(file.path) : basename(path);

  return createPortal(
    <div onClick={onClose} className="viewer-backdrop fade-in">
      <aside
        onClick={(e) => e.stopPropagation()}
        className="viewer-panel"
        role="dialog"
        aria-labelledby="file-viewer-title"
      >
        <header className="viewer-panel__head">
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="viewer-panel__meta">
                <span className="smallcaps viewer-panel__kind">{subtitle || file?.kind || 'file'}</span>
                <span className="viewer-panel__run" title={path}>{path}</span>
              </div>
              <h2 id="file-viewer-title" className="viewer-panel__title">{title || headerName}</h2>
            </div>
            <CloseButton onClick={onClose} title="close (esc)" />
          </div>
          <div className="viewer-panel__actions">
            {toggleable && (
              <span className="file-view__toggle">
                <button
                  className={`text-btn${view === 'rendered' ? ' text-btn--accent' : ''}`}
                  onClick={() => setView('rendered')}
                >
                  rendered
                </button>
                <button
                  className={`text-btn${view === 'source' ? ' text-btn--accent' : ''}`}
                  onClick={() => setView('source')}
                >
                  source
                </button>
              </span>
            )}
            <button className="text-btn text-btn--accent" onClick={onCopy}>
              {copied ? 'copied' : copyableText ? 'copy' : 'copy path'}
            </button>
            {file && (
              <>
                <button
                  className="text-btn text-btn--accent"
                  onClick={() => openExternally(false)}
                  title="open in the default app"
                >
                  open in app
                </button>
                <button
                  className="text-btn text-btn--accent"
                  onClick={() => openExternally(true)}
                  title="reveal in the file manager"
                >
                  reveal
                </button>
              </>
            )}
            <span style={{ flex: 1 }} />
            {actionMsg && <span className="viewer-panel__hint" style={{ color: 'var(--state-err)' }}>{actionMsg}</span>}
            {file?.truncated && <span className="viewer-panel__hint">truncated</span>}
            <span className="viewer-panel__hint">esc to close</span>
          </div>
        </header>
        <div className="viewer-panel__body scroll">
          {loading && (
            <p className="serif" style={{ fontStyle: 'italic', color: 'var(--ink-4)' }}>loading…</p>
          )}
          {!loading && error && (
            <div className="serif" style={{ color: 'var(--ink-3)', fontStyle: 'italic', fontSize: 14 }}>
              <p style={{ margin: '0 0 8px' }}>{error}</p>
              <p className="mono" style={{ fontStyle: 'normal', fontSize: 11.5, color: 'var(--ink-4)' }}>{path}</p>
            </div>
          )}
          {!loading && !error && file && (
            <FileBody file={file} view={view} onNavigate={setPath} />
          )}
        </div>
      </aside>
    </div>,
    document.body,
  );
}
