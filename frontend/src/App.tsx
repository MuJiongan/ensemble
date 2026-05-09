import { useEffect, useRef, useState } from 'react';
import { TopBar } from './components/TopBar';
import { Canvas } from './components/Canvas';
import { ChatPanel, type ChatMessage } from './components/ChatPanel';
import { NodePanel } from './components/NodePanel';
import { RunPanel } from './components/RunPanel';
import { SettingsPanel } from './components/Settings';
import { Hero } from './components/Hero';
import { SnapshotBanner } from './components/SnapshotBanner';
import { SnapshotRunPanel } from './components/SnapshotRunPanel';
import { api } from './api';
import { loadSettings, SETTINGS_CHANGED_EVENT } from './localSettings';
import {
  DEFAULT_WORKFLOW_NAME,
  deriveSessionName,
  historyToChatMessages,
  snapshotToDetail,
} from './appHelpers';
import { useOrchestratorStream } from './orchestratorStream';
import { useRunWebSocket } from './runWebSocket';
import type {
  Workflow, WorkflowDetail, NodeRunStatus, Run,
} from './types';

type View = 'workflow' | 'settings';

export default function App() {
  const [view, setView] = useState<View>('workflow');
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(true);

  // Mirror of `activeId` so async callbacks (SSE handlers, refreshDetail)
  // can read the latest value without being trapped by render-time closures.
  const activeIdRef = useRef<string | null>(null);
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  // Workflow id → session id (one session per workflow for v0).
  const [sessionByWorkflow, setSessionByWorkflow] = useState<Record<string, string>>({});
  const [chatByWorkflow, setChatByWorkflow] = useState<Record<string, ChatMessage[]>>({});
  // Workflows whose orchestrator is currently streaming.
  const [orchestratingIds, setOrchestratingIds] = useState<Set<string>>(new Set());

  // When non-null, the canvas renders this run's frozen `workflow_snapshot`
  // instead of the live graph. NodePanel still works (read-only) so the user
  // can drill into a snapshot node's code and that node's run trace; the
  // live RunPanel is hidden.
  const [viewingRun, setViewingRun] = useState<Run | null>(null);
  // Selection is tracked separately for snapshot view so it doesn't leak
  // into the live canvas's selection state when the user toggles back.
  const [selectedSnapshotNodeId, setSelectedSnapshotNodeId] = useState<string | null>(null);

  const refreshWorkflows = async () => {
    const list = await api.listWorkflows();
    setWorkflows(list);
    setActiveId((cur) => cur ?? (list.length ? list[0].id : null));
    return list;
  };

  const refreshDetail = async (wid?: string) => {
    const target = wid ?? activeIdRef.current;
    if (!target) {
      if (activeIdRef.current === null) setDetail(null);
      return;
    }
    try {
      const d = await api.getWorkflow(target);
      // Race guard: if the user switched workflows while we were fetching,
      // drop the result so we don't clobber the new workflow's detail.
      if (activeIdRef.current === target) setDetail(d);
    } catch {
      if (activeIdRef.current === target) setDetail(null);
    }
  };

  const { currentRun, setCurrentRun, attachToRun, closeWs } = useRunWebSocket(workflows);

  const attachToRunRef = useRef(attachToRun);
  useEffect(() => { attachToRunRef.current = attachToRun; });

  const { streamToOrchestrator, abortStream, dropWorkflow } = useOrchestratorStream({
    setChatByWorkflow,
    setOrchestratingIds,
    refreshDetail,
    attachToRunRef,
  });

  const enterSnapshotView = async (runId: string) => {
    try {
      const run = await api.getRun(runId);
      if (!run.workflow_snapshot) return;
      setViewingRun(run);
      setSelectedSnapshotNodeId(null);
      setSelectedNodeId(null);
      // For runs still executing, attach to the live WS so node-state dots
      // animate on the snapshot canvas (snapshotNodeStates prefers
      // currentRun.nodeStates when its id matches viewingRun.id) and the
      // node panel's trace tab streams events. For terminal runs, the
      // historical NodeRun rows are enough — no WS needed.
      const isRunning = run.status === 'running' || run.status === 'pending';
      if (isRunning && (!currentRun || currentRun.id !== run.id)) {
        // We don't know whether this run executes on live or on a divergent
        // snapshot (the click came from the recent-runs list, which doesn't
        // distinguish). Mark it `executesOnSnapshot` so leaving snapshot
        // view to live doesn't overlay potentially-mismatched dots there;
        // snapshot view itself overlays correctly via id lookup.
        attachToRunRef.current(run.id, run.workflow_id, run.status, /* executesOnSnapshot */ true);
      }
    } catch {
      /* fetch failure: leave view as-is */
    }
  };
  const exitSnapshotView = () => {
    setViewingRun(null);
    setSelectedSnapshotNodeId(null);
  };
  // Switching workflows must drop snapshot view — the snapshot belongs to
  // whatever workflow's runs the user was browsing before.
  useEffect(() => {
    setViewingRun(null);
    setSelectedSnapshotNodeId(null);
  }, [activeId]);

  // viewingRun is captured at the moment snapshot view opens, so for an
  // in-flight run its outputs/node_runs/total_cost are empty. currentRun
  // streams the live deltas, but the SnapshotRunPanel reads its display
  // fields off viewingRun. When the bound run finishes, refetch it once so
  // the panel picks up the final outputs, completed node_runs, and total
  // cost in a single update.
  useEffect(() => {
    if (!viewingRun || !currentRun || currentRun.id !== viewingRun.id) return;
    const terminal =
      currentRun.status === 'success' ||
      currentRun.status === 'error' ||
      currentRun.status === 'cancelled';
    if (!terminal) return;
    let cancelled = false;
    api.getRun(viewingRun.id)
      .then((fresh) => {
        if (cancelled) return;
        setViewingRun((cur) => (cur && cur.id === fresh.id ? fresh : cur));
      })
      .catch(() => { /* leave viewingRun stale; user can exit and re-enter */ });
    return () => { cancelled = true; };
  }, [viewingRun?.id, currentRun?.id, currentRun?.status]);

  // Mirror localStorage's orchestrator-model setting so the chat header reflects
  // what's actually being sent over the wire. Refreshed on save (custom event)
  // and on cross-tab edits (`storage`).
  const [orchestratorModel, setOrchestratorModel] = useState<string>(
    () => loadSettings().default_orchestrator_model,
  );
  const [hasApiKey, setHasApiKey] = useState<boolean>(
    () => !!loadSettings().openrouter_api_key,
  );
  useEffect(() => {
    const sync = () => {
      const s = loadSettings();
      setOrchestratorModel(s.default_orchestrator_model);
      setHasApiKey(!!s.openrouter_api_key);
    };
    window.addEventListener(SETTINGS_CHANGED_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(SETTINGS_CHANGED_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  // On every workflow switch, hydrate its session + chat history if we have one.
  const hydrateSession = async (wid: string) => {
    if (sessionByWorkflow[wid]) return;
    try {
      const sessions = await api.listSessions(wid);
      if (sessions.length === 0) return;
      const sid = sessions[0].id;
      const history = await api.getSessionMessages(sid);
      const bubbles = historyToChatMessages(history.messages);
      // Race: if the user typed into a brand-new workflow, handleSend may have
      // already created a session and started a stream while we were fetching.
      // Trampling the optimistic [user, placeholder] would orphan the
      // in-flight SSE updates (updateAssistant bails when the last bubble is
      // no longer a streaming assistant) — the user then sees nothing until
      // they refresh. Skip the write when newer in-memory state exists.
      setSessionByWorkflow((prev) => (prev[wid] ? prev : { ...prev, [wid]: sid }));
      setChatByWorkflow((prev) =>
        (prev[wid] && prev[wid].length > 0) ? prev : { ...prev, [wid]: bubbles },
      );
    } catch {
      /* ignore — leave panel empty */
    }
  };

  useEffect(() => {
    refreshWorkflows();
  }, []);

  useEffect(() => {
    refreshDetail();
    if (activeId) hydrateSession(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  useEffect(() => {
    return () => closeWs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const activeWorkflow = workflows.find((w) => w.id === activeId) ?? null;
  const messages = activeId ? chatByWorkflow[activeId] ?? [] : [];

  /**
   * Send a user message. Lazily creates a workflow / session if one doesn't
   * exist yet, so the user can land on the workspace and just start typing.
   * The first message also becomes the workflow's name (when it's still the
   * "untitled session" placeholder).
   */
  const handleSend = async (text: string) => {
    let wid = activeId;
    let isFirstMessage = false;

    if (!wid) {
      // No active session — create one named after this message.
      const w = await api.createWorkflow(deriveSessionName(text));
      wid = w.id;
      isFirstMessage = true;
      setWorkflows((prev) => (prev.find((p) => p.id === w.id) ? prev : [w, ...prev]));
      setActiveId(w.id);
      setSelectedNodeId(null);
    }

    let sid = sessionByWorkflow[wid];
    if (!sid) {
      const session = await api.createSession(wid);
      sid = session.id;
      setSessionByWorkflow((prev) => ({ ...prev, [wid!]: session.id }));
      setChatByWorkflow((prev) => (prev[wid!] ? prev : { ...prev, [wid!]: [] }));
    }

    // If the workflow is still the placeholder name, rename it now.
    const cur = workflows.find((w) => w.id === wid);
    if (!isFirstMessage && cur && cur.name === DEFAULT_WORKFLOW_NAME) {
      const nextName = deriveSessionName(text);
      api.patchWorkflow(wid, { name: nextName }).then(() => refreshWorkflows()).catch(() => {});
    }

    streamToOrchestrator(wid, sid, text);
  };

  const cancelOrchestrator = async () => {
    if (!activeId) return;
    const sid = sessionByWorkflow[activeId];
    if (!sid) return;
    try { await api.cancelOrchestratorTurn(sid); } catch { /* ignore */ }
    abortStream(activeId);
  };

  /**
   * "new" button — drop into the Hero empty state without touching the
   * backend. handleSend's `!wid` branch lazily creates the workflow and
   * session when the user sends their first message, so we avoid leaving
   * orphaned "untitled session" rows behind every time someone clicks +.
   */
  const handleNew = () => {
    setActiveId(null);
    setDetail(null);
    setView('workflow');
    setSelectedNodeId(null);
    setCurrentRun(null);
  };

  const handleDelete = async (id: string) => {
    await api.deleteWorkflow(id);
    if (id === activeId) {
      setActiveId(null);
      setDetail(null);
      setCurrentRun(null);
    }
    setChatByWorkflow((prev) => {
      const { [id]: _, ...rest } = prev;
      return rest;
    });
    setSessionByWorkflow((prev) => {
      const { [id]: _, ...rest } = prev;
      return rest;
    });
    dropWorkflow(id);
    await refreshWorkflows();
  };

  const handleRename = async (id: string, name: string) => {
    await api.patchWorkflow(id, { name });
    await refreshWorkflows();
    if (id === activeId) await refreshDetail();
  };

  const startRun = async (inputs: Record<string, unknown>) => {
    if (!detail) return;
    const run = await api.startRun(detail.id, inputs);
    attachToRun(run.id, detail.id, run.status);
  };

  const cancelRun = async () => {
    if (!currentRun) return;
    try { await api.cancelRun(currentRun.id); } catch { /* ignore */ }
  };

  /** Forward an error from a run/node into the orchestrator chat as a user message. */
  const sendErrorToOrchestrator = (message: string) => {
    setChatOpen(true);
    handleSend(message);
  };

  const selectedNode = detail?.nodes.find((n) => n.id === selectedNodeId);
  const isOrchestrating = !!activeId && orchestratingIds.has(activeId);

  const topBarStatus: 'idle' | 'building' | 'running' | 'ready' = isOrchestrating
    ? 'building'
    : currentRun?.status === 'running' || currentRun?.status === 'pending'
      ? 'running'
      : currentRun?.status === 'success'
        ? 'ready'
        : 'idle';

  // Per-node state dots for snapshot view. When the viewed run is the
  // currently-attached one (rerun-from-snapshot, mid-execution), use live
  // states from the WS so the dots animate on the snapshot canvas.
  // Otherwise use the historical NodeRun rows (frozen post-completion).
  // The live canvas has its own overlay below — this one's just for
  // snapshot view.
  const snapshotNodeStates: Record<string, NodeRunStatus> = viewingRun
    ? currentRun && currentRun.id === viewingRun.id
      ? currentRun.nodeStates
      : Object.fromEntries(
          viewingRun.node_runs.map((nr) => [nr.node_id, nr.status]),
        )
    : {};

  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        overflow: 'hidden',
        position: 'relative',
        background: 'var(--paper)',
        color: 'var(--ink)',
      }}
    >
      <TopBar
        workflows={workflows}
        activeWorkflow={activeWorkflow}
        onSelect={(id) => {
          setActiveId(id);
          setView('workflow');
          setSelectedNodeId(null);
          setCurrentRun(null);
        }}
        onNew={handleNew}
        onRename={handleRename}
        onDelete={handleDelete}
        onOpenSettings={() => setView('settings')}
        onOpenRun={() => {
          // RunPanel is the default right-side surface — "Run" in the
          // top bar is now just a deselect shortcut so it returns to view.
          setSelectedNodeId(null);
        }}
        runDisabled={!detail}
        status={topBarStatus}
      />

      <main style={{ height: 'calc(100vh - 54px)', position: 'relative' }}>
        {view === 'settings' && <SettingsPanel onClose={() => setView('workflow')} />}

        {view === 'workflow' &&
          (!detail || (detail.nodes.length === 0 && messages.length === 0)) && (
            <Hero
              hasApiKey={hasApiKey}
              disabled={isOrchestrating}
              onSend={(text) => {
                setChatOpen(true);
                handleSend(text);
              }}
              onOpenSettings={() => setView('settings')}
            />
          )}

        {view === 'workflow' && detail && !(detail.nodes.length === 0 && messages.length === 0) && (
          <>
            <div style={{ display: 'flex', height: '100%' }}>
              {/* left 2/5 — canvas */}
              <div
                style={{
                  flex: 2,
                  display: 'flex',
                  flexDirection: 'column',
                  borderRight: '1px solid var(--rule)',
                  minWidth: 0,
                }}
              >
                {viewingRun && (
                  <SnapshotBanner run={viewingRun} onExit={exitSnapshotView} />
                )}
                {/* Canvas (and the empty-canvas placeholder) need
                 * `position: relative` to host React Flow's absolute layout.
                 * Banner stacks above via flex; this wrapper takes the rest. */}
                <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
                {viewingRun ? (
                  // Viewing a run's frozen snapshot. Selection is enabled so
                  // the user can drill into a node's code + run trace, but
                  // editing is disabled (NodePanel renders read-only).
                  (() => {
                    const snapDetail = snapshotToDetail(viewingRun);
                    return snapDetail ? (
                      <Canvas
                        detail={snapDetail}
                        selectedNodeId={selectedSnapshotNodeId}
                        onSelectNode={(id) => setSelectedSnapshotNodeId(id)}
                        nodeStates={snapshotNodeStates}
                      />
                    ) : null;
                  })()
                ) : detail ? (
                  <Canvas
                    detail={detail}
                    selectedNodeId={selectedNodeId}
                    onSelectNode={(id) => setSelectedNodeId(id)}
                    // Overlay live node states for runs that execute on the
                    // live graph (manual run, orchestrator-triggered run).
                    // Skip for snapshot reruns — their snapshot can diverge
                    // from live, so dots may apply to wrong nodes or miss
                    // entirely. Snapshot view is the right place to watch
                    // those; the rerun handler keeps the user there.
                    nodeStates={
                      currentRun &&
                      currentRun.workflow_id === detail.id &&
                      !currentRun.executesOnSnapshot
                        ? currentRun.nodeStates
                        : undefined
                    }
                  />
                ) : (
                  <div
                    style={{
                      position: 'absolute',
                      inset: 0,
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: 10,
                      color: 'var(--ink-4)',
                      padding: 24,
                    }}
                    className="dotgrid"
                  >
                    <div className="serif" style={{ fontStyle: 'italic', fontSize: 22, color: 'var(--ink-3)' }}>
                      an empty canvas.
                    </div>
                    <div style={{ fontSize: 13, maxWidth: 320, textAlign: 'center', lineHeight: 1.6 }}>
                      open the chat to describe a problem, or click{' '}
                      <span className="italic-em">new</span> to start a session.
                    </div>
                  </div>
                )}
                </div>
              </div>

              {/* right 3/5 — node configuration / inputs / outputs */}
              <div style={{ flex: 3, position: 'relative', minWidth: 0 }}>
                {viewingRun && (() => {
                  const snapDetail = snapshotToDetail(viewingRun);
                  const snapNode = snapDetail?.nodes.find(
                    (n) => n.id === selectedSnapshotNodeId,
                  );
                  if (snapDetail && snapNode) {
                    return (
                      <NodePanel
                        node={snapNode}
                        workflow={snapDetail}
                        onClose={() => setSelectedSnapshotNodeId(null)}
                        onChange={() => {}}
                        readOnly
                        pinnedRun={viewingRun}
                        // Pass currentRun so the trace tab streams live
                        // events when this snapshot view is bound to an
                        // in-flight run (rerun-from-snapshot, or recent-run
                        // click on a running run). Without it, the trace
                        // would fall back to viewingRun.node_runs — empty
                        // for runs that haven't materialised yet.
                        currentRun={currentRun}
                        onSendErrorToOrchestrator={sendErrorToOrchestrator}
                      />
                    );
                  }
                  return (
                    <SnapshotRunPanel
                      run={viewingRun}
                      onExit={exitSnapshotView}
                      // Block rerun while any run on this workflow is in
                      // flight — server has no guard yet, so we hold the
                      // line in the UI to avoid stacking parallel runs.
                      runInProgress={
                        !!currentRun &&
                        currentRun.workflow_id === viewingRun.workflow_id &&
                        (currentRun.status === 'running' ||
                          currentRun.status === 'pending')
                      }
                      onRerun={async (inputs) => {
                        const newRun = await api.rerunFromSnapshot(
                          viewingRun.id,
                          inputs,
                        );
                        // The rerun executes against the snapshot's graph
                        // (which may diverge from live), so the live canvas
                        // can't show its progress reliably. Stay in
                        // snapshot view — swap viewingRun to the new run
                        // and attach the WS so node-state dots animate on
                        // the snapshot canvas in real time. The user exits
                        // via "← live" on the SnapshotBanner whenever
                        // they're done watching.
                        setViewingRun(newRun);
                        setSelectedSnapshotNodeId(null);
                        attachToRunRef.current(
                          newRun.id,
                          newRun.workflow_id,
                          newRun.status,
                          /* executesOnSnapshot */ true,
                        );
                      }}
                    />
                  );
                })()}
                {!viewingRun && detail && selectedNode && (
                  <NodePanel
                    node={selectedNode}
                    workflow={detail}
                    onClose={() => setSelectedNodeId(null)}
                    onChange={refreshDetail}
                    currentRun={currentRun}
                    onSendErrorToOrchestrator={sendErrorToOrchestrator}
                  />
                )}
                {!viewingRun && detail && !selectedNode && (
                  <RunPanel
                    workflow={detail}
                    currentRun={currentRun}
                    onStart={startRun}
                    onCancel={cancelRun}
                    onViewRunOnCanvas={enterSnapshotView}
                    orchestrating={isOrchestrating}
                  />
                )}
                {!viewingRun && !detail && (
                  <div
                    style={{
                      position: 'absolute',
                      inset: 0,
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: 12,
                      color: 'var(--ink-4)',
                      padding: 24,
                    }}
                  >
                    <div className="serif" style={{ fontStyle: 'italic', fontSize: 22, color: 'var(--ink-3)' }}>
                      no session yet.
                    </div>
                    <div style={{ fontSize: 13, maxWidth: 420, textAlign: 'center', lineHeight: 1.6 }}>
                      open the chat at the bottom-right and describe a problem, or click{' '}
                      <span className="italic-em">new</span> to start a fresh session.
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* floating chatbot — launcher when closed, panel when open. */}
            {!chatOpen && (
              <button
                onClick={() => setChatOpen(true)}
                className="chat-tab-enter"
                style={{
                  position: 'fixed',
                  bottom: 80,
                  right: 0,
                  width: 50,
                  height: 50,
                  padding: 0,
                  background: 'var(--paper)',
                  border: '1px solid var(--rule)',
                  borderRight: 'none',
                  borderTopLeftRadius: 4,
                  borderBottomLeftRadius: 4,
                  borderTopRightRadius: 0,
                  borderBottomRightRadius: 0,
                  cursor: 'pointer',
                  zIndex: 50,
                  boxShadow: '-6px 0 22px -10px rgba(26, 23, 20, 0.22), -1px 0 4px -2px rgba(26, 23, 20, 0.14)',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 4,
                  color: 'var(--ink)',
                }}
                title="open the orchestrator"
              >
                <svg
                  width="24"
                  height="24"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.25"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                  style={{ color: 'var(--ink-2)' }}
                >
                  <path d="M6 4h12a4 4 0 0 1 4 4v6a4 4 0 0 1-4 4h-7l-4 3v-3H6a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4z" />
                </svg>
                {isOrchestrating && (
                  <span
                    style={{
                      width: 5,
                      height: 5,
                      borderRadius: 999,
                      background: 'var(--accent, #c08552)',
                      animation: 'pulse 1.4s ease-in-out infinite',
                    }}
                    aria-label="orchestrator is working"
                  />
                )}
              </button>
            )}
            {chatOpen && (
              <div
                className="chat-enter"
                style={{
                  position: 'fixed',
                  bottom: 24,
                  right: 24,
                  width: 'min(440px, calc(100vw - 48px))',
                  height: 'min(640px, calc(100vh - 102px))',
                  background: 'var(--paper)',
                  border: '1px solid var(--rule)',
                  borderRadius: 6,
                  display: 'flex',
                  flexDirection: 'column',
                  zIndex: 50,
                  boxShadow: '0 24px 60px -20px rgba(26, 23, 20, 0.35), 0 8px 20px -8px rgba(26, 23, 20, 0.18)',
                  overflow: 'hidden',
                }}
              >
                <div style={{ flex: 1, minHeight: 0 }}>
                  <ChatPanel
                    messages={messages}
                    onSend={handleSend}
                    onCancel={cancelOrchestrator}
                    disabled={isOrchestrating}
                    sessionTitle={activeWorkflow?.name}
                    modelLabel={orchestratorModel}
                    onClose={() => setChatOpen(false)}
                    onViewRun={(runId) => {
                      void enterSnapshotView(runId);
                    }}
                  />
                </div>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
