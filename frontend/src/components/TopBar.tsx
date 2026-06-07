import { useEffect, useRef, useState } from 'react';
import type { Workflow } from '../types';
import { loadTheme, saveTheme, THEME_CHANGED_EVENT, type Theme } from '../theme';
import { ThemeToggle } from './ThemeToggle';
import { ConfirmDialog } from './ConfirmDialog';

interface Props {
  workflows: Workflow[];
  activeWorkflow: Workflow | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onFork: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onDelete: (id: string) => void;
  onOpenSettings: () => void;
  onOpenRun: () => void;
  runDisabled?: boolean;
  status?: 'idle' | 'building' | 'running' | 'ready';
}

export function TopBar({
  workflows,
  activeWorkflow,
  onSelect,
  onNew,
  onFork,
  onRename,
  onDelete,
  onOpenSettings,
  onOpenRun,
  runDisabled,
  status = 'idle',
}: Props) {
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  // id of the workflow row currently being renamed inline (null = none).
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState('');
  const pickerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const sync = () => setTheme(loadTheme());
    window.addEventListener(THEME_CHANGED_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(THEME_CHANGED_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  // Close on outside-click. Listener only mounts while the picker is open so
  // we don't catch the very click that's about to open it.
  useEffect(() => {
    if (!pickerOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
        setEditingId(null);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [pickerOpen]);

  const startRename = (w: Workflow) => {
    setEditingId(w.id);
    setDraftName(w.name);
  };

  const commitRename = (id: string) => {
    const trimmed = draftName.trim();
    const original = workflows.find((w) => w.id === id);
    if (trimmed && original && trimmed !== original.name) {
      onRename(id, trimmed);
    }
    setEditingId(null);
  };

  const statusLabel =
    status === 'building' ? 'building' : status === 'running' ? 'running' : status === 'ready' ? 'ready' : 'idle';
  const statusDotClass =
    status === 'building' || status === 'running' ? 'running' : status === 'ready' ? 'success' : 'idle';

  return (
    <div
      style={{
        height: 54,
        borderBottom: '1px solid var(--rule)',
        background: 'var(--paper)',
        display: 'flex',
        alignItems: 'center',
        padding: '0 22px',
        gap: 18,
        position: 'relative',
        zIndex: 60,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span
          className="serif"
          style={{
            fontStyle: 'italic',
            fontSize: 22,
            color: 'var(--ink)',
            letterSpacing: '-0.01em',
          }}
        >
          ensemble
        </span>
      </div>

      <span className="smallcaps" style={{ color: 'var(--ink-4)' }}>project</span>

      <div
        ref={pickerRef}
        style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 6, flex: 1, minWidth: 0 }}
      >
        <button
          type="button"
          onClick={() => setPickerOpen((v) => !v)}
          aria-haspopup="listbox"
          aria-expanded={pickerOpen}
          className="serif"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            fontStyle: 'italic',
            fontSize: 14,
            color: 'var(--ink-2)',
            maxWidth: 460,
            background: 'transparent',
            border: 0,
            cursor: 'pointer',
            padding: '2px 0',
            textAlign: 'left',
            minWidth: 0,
          }}
          title="open project library"
        >
          <span
            style={{
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              minWidth: 0,
            }}
          >
            {activeWorkflow?.name || 'untitled'}
          </span>
          <span
            style={{
              color: 'var(--ink-4)',
              fontStyle: 'normal',
              fontSize: 11,
              flex: 'none',
              transition: 'transform .15s',
              transform: pickerOpen ? 'rotate(180deg)' : 'rotate(0deg)',
              display: 'inline-block',
            }}
          >
            ▾
          </span>
        </button>

        {pickerOpen && (
          <div
            className="shadow-card fade-in"
            role="listbox"
            style={{
              position: 'absolute',
              top: 28,
              left: 0,
              minWidth: 320,
              maxHeight: 360,
              overflow: 'auto',
              background: 'var(--paper)',
              border: '1px solid var(--rule)',
              borderRadius: 4,
              padding: 6,
              zIndex: 100,
            }}
          >
            <div
              className="smallcaps"
              style={{
                padding: '7px 10px 6px',
                color: 'var(--ink-4)',
                borderBottom: '1px solid var(--rule-2)',
                marginBottom: 3,
              }}
            >
              project library
            </div>
            {workflows.length === 0 && (
              <div
                className="serif"
                style={{ padding: 12, fontStyle: 'italic', color: 'var(--ink-4)', fontSize: 13 }}
              >
                no projects yet.
              </div>
            )}
            {workflows.map((w) => {
              const isActive = w.id === activeWorkflow?.id;
              const isEditing = editingId === w.id;
              return (
                <WorkflowRow
                  key={w.id}
                  workflow={w}
                  isActive={isActive}
                  isEditing={isEditing}
                  draftName={draftName}
                  onDraftChange={setDraftName}
                  onCommit={() => commitRename(w.id)}
                  onCancelEdit={() => setEditingId(null)}
                  onSelect={() => {
                    onSelect(w.id);
                    setPickerOpen(false);
                    setEditingId(null);
                  }}
                  onStartRename={() => startRename(w)}
                  onDelete={() => setDeleteTarget({ id: w.id, name: w.name })}
                />
              );
            })}
          </div>
        )}
      </div>

      {activeWorkflow && (
        <span
          className="smallcaps"
          style={{
            color: 'var(--ink-3)',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          <span className={`node-state-dot ${statusDotClass}`} />
          {statusLabel}
        </span>
      )}

      <ThemeToggle
        theme={theme}
        onChange={(next) => saveTheme(next)}
      />
      <button className="topbar-btn" onClick={onOpenSettings}>
        settings
      </button>
      <button
        className="topbar-btn"
        onClick={() => activeWorkflow && onFork(activeWorkflow.id)}
        disabled={!activeWorkflow}
        title="copy the current live canvas into a new project"
      >
        fork
      </button>
      <button className="topbar-btn" onClick={onNew}>
        new project
      </button>
      <button className="topbar-btn" onClick={onOpenRun} disabled={runDisabled}>
        runs
      </button>

      {deleteTarget && (
        <ConfirmDialog
          title="delete project"
          message={`delete "${deleteTarget.name}"? this cannot be undone.`}
          confirmLabel="delete"
          variant="danger"
          onConfirm={() => {
            onDelete(deleteTarget.id);
            setDeleteTarget(null);
            setPickerOpen(false);
            setEditingId(null);
          }}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}

function WorkflowRow({
  workflow,
  isActive,
  isEditing,
  draftName,
  onDraftChange,
  onCommit,
  onCancelEdit,
  onSelect,
  onStartRename,
  onDelete,
}: {
  workflow: Workflow;
  isActive: boolean;
  isEditing: boolean;
  draftName: string;
  onDraftChange: (s: string) => void;
  onCommit: () => void;
  onCancelEdit: () => void;
  onSelect: () => void;
  onStartRename: () => void;
  onDelete: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!isEditing) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [isEditing]);

  return (
    <div
      role="option"
      aria-selected={isActive}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={() => {
        if (isEditing) return;
        onSelect();
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '7px 10px',
        borderRadius: 3,
        cursor: isEditing ? 'default' : 'pointer',
        background: isActive ? 'var(--surface-hover)' : 'transparent',
      }}
    >
      {isEditing ? (
        <input
          ref={inputRef}
          value={draftName}
          onChange={(e) => onDraftChange(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          onBlur={onCommit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onCommit();
            else if (e.key === 'Escape') onCancelEdit();
          }}
          className="serif"
          style={{
            flex: 1,
            fontStyle: 'italic',
            fontSize: 13.5,
            color: 'var(--ink)',
            background: 'transparent',
            border: 0,
            outline: 'none',
            borderBottom: '1px solid var(--ink)',
            padding: '1px 0',
            minWidth: 0,
          }}
        />
      ) : (
        <span
          className="serif"
          style={{
            fontStyle: 'italic',
            fontSize: 13.5,
            color: 'var(--ink-2)',
            flex: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
          }}
        >
          {workflow.name}
        </span>
      )}

      {/* Row actions — only revealed on hover or while editing. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 2,
          opacity: hovered || isEditing ? 1 : 0,
          transition: 'opacity .15s',
        }}
      >
        {!isEditing && (
          <button
            type="button"
            title="rename"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onStartRename();
            }}
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'transparent',
              border: 0,
              color: 'var(--ink-4)',
              cursor: 'pointer',
              fontSize: 10,
              padding: '0 5px',
              fontFamily: 'var(--sans)',
              fontWeight: 500,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
            }}
          >
            rename
          </button>
        )}
        <button
          type="button"
          title="delete"
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          style={{
            background: 'transparent',
            border: 0,
            color: 'var(--ink-4)',
            cursor: 'pointer',
            fontSize: 13,
            padding: '0 4px',
          }}
        >
          ×
        </button>
      </div>
    </div>
  );
}
