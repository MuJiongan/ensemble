/** Chat attachments: document-level drag-drop + paste capture for images,
 * PDFs, and text files, a pending-attachment store, and the thumbnail-chip
 * strip both composers (Hero and ChatPanel) render. */
import { useEffect, useRef, useState } from 'react';

const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
const EXT_MIMES: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  pdf: 'application/pdf',
};
const TEXT_MIMES = new Set([
  'application/json',
  'application/ld+json',
  'application/toml',
  'application/x-toml',
  'application/x-yaml',
  'application/xml',
  'application/yaml',
]);
const SAMPLE = 4096;

function textMime(type: string): boolean {
  if (!type) return false;
  if (type.startsWith('text/')) return true;
  if (TEXT_MIMES.has(type)) return true;
  if (type.endsWith('+json')) return true;
  return type.endsWith('+xml');
}

/** Mostly-printable heuristic for type-less files: no NUL bytes, and under
 * 30% non-whitespace control characters in the sample. */
function textBytes(bytes: Uint8Array): boolean {
  if (bytes.length === 0) return true;
  let count = 0;
  for (const byte of bytes) {
    if (byte === 0) return false;
    if (byte < 9 || (byte > 13 && byte < 32)) count += 1;
  }
  return count / bytes.length <= 0.3;
}

/** Detect a usable attachment MIME: known images and PDF pass through,
 * anything text-like — declared text mime, JSON/YAML/TOML/XML, +json/+xml
 * suffixes, or a byte-sniffed printable sample — normalizes to text/plain.
 * Returns null for binary files we can't represent. */
export async function attachmentMime(file: File): Promise<string | null> {
  const type = (file.type || '').split(';')[0].trim().toLowerCase();
  if (IMAGE_MIMES.has(type) || type === 'application/pdf') return type;

  const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
  const fallback = EXT_MIMES[ext];
  if ((!type || type === 'application/octet-stream') && fallback) return fallback;

  if (textMime(type)) return 'text/plain';
  const bytes = new Uint8Array(await file.slice(0, SAMPLE).arrayBuffer());
  if (!textBytes(bytes)) return null;
  return 'text/plain';
}

/** Read a file as a normalized `data:<mime>;base64,...` URL ('' on failure). */
function readDataUrl(file: File, mime: string): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.addEventListener('error', () => resolve(''));
    reader.addEventListener('load', () => {
      const value = typeof reader.result === 'string' ? reader.result : '';
      const idx = value.indexOf(',');
      resolve(idx === -1 ? value : `data:${mime};base64,${value.slice(idx + 1)}`);
    });
    reader.readAsDataURL(file);
  });
}

export interface PendingAttachment {
  id: string;
  filename: string;
  mime: string;
  dataUrl: string;
}

const NOTICE_MS = 4000;

/** Owns the pending-attachment list and, while `enabled`, captures image/PDF
 * files dropped or pasted anywhere in the document. `onAdded` fires after at
 * least one attachment lands (e.g. to reveal the composer that shows the
 * chips). `notice` carries a transient "unsupported file" message when a
 * drop/paste contained files but none were attachable. */
export function useImageAttachments(enabled: boolean, onAdded?: () => void) {
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const onAddedRef = useRef(onAdded);
  onAddedRef.current = onAdded;
  const noticeTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!enabled) return;

    const warn = () => {
      setNotice('this file type can’t be attached (images, pdfs, and text files only)');
      window.clearTimeout(noticeTimer.current);
      noticeTimer.current = window.setTimeout(() => setNotice(null), NOTICE_MS);
    };

    const addFiles = async (files: File[]) => {
      let added = false;
      for (const file of files) {
        const mime = await attachmentMime(file);
        if (!mime) continue;
        const dataUrl = await readDataUrl(file, mime);
        if (!dataUrl) continue;
        added = true;
        setAttachments((prev) => [
          ...prev,
          { id: crypto.randomUUID(), filename: file.name, mime, dataUrl },
        ]);
      }
      if (added) onAddedRef.current?.();
      else if (files.length > 0) warn();
    };

    // A drop target must cancel BOTH dragenter and dragover — Safari rejects
    // the drop (and never fires the drop event) when dragenter isn't cancelled.
    const onDragEnter = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes('Files')) return;
      e.preventDefault();
      setDragging(true);
    };
    const onDragOver = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes('Files')) return;
      e.preventDefault();
      setDragging(true);
    };
    const onDragLeave = (e: DragEvent) => {
      if (!e.relatedTarget) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      setDragging(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length === 0) return;
      e.preventDefault();
      void addFiles(files);
    };
    const onPaste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.items ?? []).flatMap((item) => {
        if (item.kind !== 'file') return [];
        const file = item.getAsFile();
        return file ? [file] : [];
      });
      if (files.length === 0) return;
      e.preventDefault();
      void addFiles(files);
    };

    document.addEventListener('dragenter', onDragEnter);
    document.addEventListener('dragover', onDragOver);
    document.addEventListener('dragleave', onDragLeave);
    document.addEventListener('drop', onDrop);
    document.addEventListener('paste', onPaste);
    return () => {
      document.removeEventListener('dragenter', onDragEnter);
      document.removeEventListener('dragover', onDragOver);
      document.removeEventListener('dragleave', onDragLeave);
      document.removeEventListener('drop', onDrop);
      document.removeEventListener('paste', onPaste);
      window.clearTimeout(noticeTimer.current);
      setDragging(false);
      setNotice(null);
    };
  }, [enabled]);

  const remove = (id: string) =>
    setAttachments((prev) => prev.filter((p) => p.id !== id));
  const clear = () => setAttachments([]);

  return { attachments, dragging, notice, remove, clear };
}

/** Kind label shown on a non-image tile: "pdf" or "txt". */
export function fileLabel(mime?: string): string {
  return mime === 'application/pdf' ? 'pdf' : 'txt';
}

/** A non-image attachment tile (used for pending file chips and for sent
 * files in user bubbles). */
export function FileTile({
  filename,
  label = 'pdf',
  title,
}: {
  filename: string;
  label?: string;
  title?: string;
}) {
  return (
    <span
      title={title ?? filename}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        maxWidth: 180,
        padding: '4px 8px',
        borderRadius: 3,
        border: '1px solid var(--rule)',
        fontSize: 11,
        color: 'var(--ink-3)',
      }}
    >
      <span className="smallcaps" style={{ fontSize: 9, color: 'var(--ink-4)', flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {filename}
      </span>
    </span>
  );
}

/** Pending-attachment chips: image thumbnails or PDF tiles, each with a
 * remove button. */
export function AttachmentChips({
  attachments,
  onRemove,
  style,
}: {
  attachments: PendingAttachment[];
  onRemove: (id: string) => void;
  style?: React.CSSProperties;
}) {
  if (attachments.length === 0) return null;
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', ...style }}>
      {attachments.map((a) => (
        <div key={a.id} style={{ position: 'relative' }}>
          {a.mime.startsWith('image/') ? (
            <img
              src={a.dataUrl}
              alt={a.filename}
              title={a.filename}
              style={{
                width: 44,
                height: 44,
                objectFit: 'cover',
                borderRadius: 3,
                border: '1px solid var(--rule)',
                display: 'block',
              }}
            />
          ) : (
            <FileTile filename={a.filename} label={fileLabel(a.mime)} />
          )}
          <button
            type="button"
            onClick={() => onRemove(a.id)}
            aria-label={`remove ${a.filename}`}
            title="remove attachment"
            style={{
              position: 'absolute',
              top: -6,
              right: -6,
              width: 16,
              height: 16,
              lineHeight: '14px',
              padding: 0,
              borderRadius: '50%',
              border: '1px solid var(--rule)',
              background: 'var(--paper)',
              color: 'var(--ink-3)',
              cursor: 'pointer',
              fontSize: 11,
            }}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
