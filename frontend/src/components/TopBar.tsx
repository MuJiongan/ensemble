import { useEffect, useMemo, useRef, useState } from 'react';
import type { Workflow } from '../types';
import { loadTheme, saveTheme, THEME_CHANGED_EVENT, type Theme } from '../theme';
import { ThemeToggle } from './ThemeToggle';
import { ConfirmDialog } from './ConfirmDialog';

interface Props {
  workflows: Workflow[];
  activeWorkflow: Workflow | null;
  onSelect: (id: string) => void;
  onNew: () => void;
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
  onRename,
  onDelete,
  onOpenSettings,
  onOpenRun,
  runDisabled,
  status = 'idle',
}: Props) {
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState('');
  const pickerRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const sync = () => setTheme(loadTheme());
    window.addEventListener(THEME_CHANGED_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(THEME_CHANGED_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  useEffect(() => {
    if (!pickerOpen) return;
    setQuery('');
    const id = requestAnimationFrame(() => searchRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [pickerOpen]);

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

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return workflows;
    return workflows.filter((w) => w.name.toLowerCase().includes(needle));
  }, [workflows, query]);

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

      <div
        ref={pickerRef}
        style={{ position: 'relative', display: 'flex', alignItems: 'center', flex: 1, minWidth: 0 }}
      >
        <button
          type="button"
          onClick={() => setPickerOpen((v) => !v)}
          aria-haspopup="listbox"
          aria-expanded={pickerOpen}
          className={`project-switcher${pickerOpen ? ' project-switcher--open' : ''}`}
          title="switch project"
        >
          <span className="project-switcher__kicker smallcaps">project</span>
          <span className="project-switcher__name">{activeWorkflow?.name || 'untitled'}</span>
          <span className="project-switcher__caret" aria-hidden="true">▾</span>
        </button>

        {pickerOpen && (
          <div
            className="shadow-card fade-in project-menu"
            role="listbox"
            style={{
              position: 'absolute',
              top: 'calc(100% + 6px)',
              left: 0,
              minWidth: '100%',
              width: 'max-content',
              maxWidth: 420,
              maxHeight: 320,
              overflow: 'auto',
              border: '1px solid var(--rule)',
              borderRadius: 4,
              zIndex: 100,
            }}
          >
            {workflows.length > 0 && (
              <div className="project-menu__search">
                <input
                  ref={searchRef}
                  className="field field--mono"
                  placeholder="filter projects…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      if (query) setQuery('');
                      else setPickerOpen(false);
                    }
                  }}
                />
              </div>
            )}

            {workflows.length === 0 ? (
              <div className="project-menu__empty">no projects yet.</div>
            ) : filtered.length === 0 ? (
              <div className="project-menu__empty">no matches.</div>
            ) : (
              filtered.map((w) => {
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
              })
            )}
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
      className={`project-menu__item${isActive ? ' project-menu__item--active' : ''}${isEditing ? ' project-menu__item--editing' : ''}`}
      onClick={() => {
        if (isEditing) return;
        onSelect();
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
          className="field field--plain project-menu__item-name"
          style={{ fontFamily: 'var(--serif)', fontSize: 13 }}
        />
      ) : (
        <span className="project-menu__item-name" title={workflow.name}>
          {workflow.name}
        </span>
      )}

      <div className="project-menu__item-actions">
        {!isEditing && (
          <button
            type="button"
            className="project-menu__action"
            title="rename"
            aria-label="rename"
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onStartRename();
            }}
            onClick={(e) => e.stopPropagation()}
          >
            ✎
          </button>
        )}
        <button
          type="button"
          className="project-menu__action project-menu__action--danger"
          title="delete"
          aria-label="delete"
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => {
            e.stopPropagation();
            onDelete();
          }}
        >
          ×
        </button>
      </div>
    </div>
  );
}
