import { useEffect, useMemo, useRef, useState } from 'react';
import { TopBar } from './components/TopBar';
import { Canvas } from './components/Canvas';
import { ChatPanel, type ChatMessage } from './components/ChatPanel';
import { NodePanel } from './components/NodePanel';
import { aggregateEvents } from './components/NodeTraceCard';
import { RunPanel } from './components/RunPanel';
import { SettingsPanel } from './components/Settings';
import { Hero } from './components/Hero';
import { SnapshotBanner } from './components/SnapshotBanner';
import { SnapshotRunPanel } from './components/SnapshotRunPanel';
import { AlertDialog, ConfirmDialog } from './components/ConfirmDialog';
import { ProjectTransferPanel } from './components/ProjectTransferPanel';
import { LlmAuthToast, McpAuthDialog } from './components/ReAuthDialogs';
import { fetchStatus as fetchAuthStatus } from './auth';
import { probeRemoteStatus } from './mcpApi';
import { api, ApiError } from './api';
import {
  loadSettings,
  saveSettings,
  isConnected,
  SETTINGS_CHANGED_EVENT,
} from './localSettings';
import {
  DEFAULT_WORKFLOW_NAME,
  deriveWorkflowName,
  formatWorkflowExport,
  historyToChatMessages,
  messagesToChat,
  liveCallToChat,
  parseWorkflowExport,
  snapshotToDetail,
  snapshotToExport,
} from './appHelpers';
import { useOrchestratorStream } from './orchestratorStream';
import { useCallChatStream } from './callChatStream';
import { useImageAttachments } from './components/ImageAttachments';
import { useRunWebSocket } from './runWebSocket';
import { getCatalog, findModel, CATALOG_CHANGED_EVENT, type Catalog } from './providerCatalog';
import type {
  Workflow, WorkflowDetail, NodeRunStatus, Run, CurrentRun, ModelSelection, CallChat,
} from './types';

type View = 'workflow' | 'settings';

// How often to re-verify that OAuth LLM provider sessions are still alive
// server-side. The status check also refreshes a near-expiry token, so this
// doubles as proactive keep-alive.
const LLM_AUTH_RECHECK_INTERVAL_MS = 5 * 60_000;

/** True when the orchestrator has a usable config — a selected model whose
 * provider is connected (api key pasted, or oauth signed in). */
function hasCredsForPreset(s: ReturnType<typeof loadSettings>): boolean {
  const sel = s.orchestrator;
  return !!sel && isConnected(s, sel.providerID);
}

export default function App() {
  const [view, setView] = useState<View>('workflow');
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  // The right column toggles between two surfaces — the project's workspace
  // (run details / node config / snapshot view) and the orchestrator chat.
  // The chat used to float as an overlay; tabbing replaces it cleanly so the
  // run/execute footer is never blocked.
  const [rightPanelMode, setRightPanelMode] = useState<'workspace' | 'chat'>('workspace');

  // Image attachments live at the App level so dropping/pasting an image
  // works anywhere in the workflow view — Hero, canvas, or either right-panel
  // tab — not just while the chat composer happens to be mounted. A drop
  // flips the right panel to chat so the attachment chips are visible.
  const imageAttachments = useImageAttachments(view === 'workflow', () =>
    setRightPanelMode('chat'),
  );

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

  // Call-llm continuations, keyed by CallChat id. One continuation per call;
  // they're reached only from a node's chat tab (not the orchestrator chat).
  // State lives here so a streaming turn survives switching nodes/tabs.
  const [callChatMessages, setCallChatMessages] = useState<Record<string, ChatMessage[]>>({});
  // Per-continuation model selection — defaults to the model the source call
  // ran with, overridable via the chat tab's model switcher.
  const [callChatModelById, setCallChatModelById] = useState<Record<string, ModelSelection>>({});
  const [streamingChatIds, setStreamingChatIds] = useState<Set<string>>(new Set());
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  // The continuation currently shown in the chat pane (null = orchestrator).
  // Set by a node's "continue →"; cleared by the chat header's "← orchestrator".
  const [activeContinuation, setActiveContinuation] = useState<CallChat | null>(null);
  // An in-flight call streaming into the chat pane (no persisted row yet). When
  // its run finishes + persists, it transitions to a continuation (composer
  // enabled). Mutually exclusive with activeContinuation.
  const [activeLiveCall, setActiveLiveCall] = useState<
    { runId: string; nodeId: string; callId: string; label: string } | null
  >(null);

  type AppDialog =
    | { kind: 'none' }
    | { kind: 'alert'; message: string; variant?: 'default' | 'error' }
    | { kind: 'confirm-clear-context' }
    | { kind: 'import-project' }
    | { kind: 'export-project' };
  const [dialog, setDialog] = useState<AppDialog>({ kind: 'none' });
  const [importDraft, setImportDraft] = useState('');
  const [exportDraft, setExportDraft] = useState('');

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

  const { runs: liveRuns, attachToRun, dropRun, dropWorkflowRuns, closeAllSockets } =
    useRunWebSocket(workflows);

  // Several runs can be attached at once (across workflows, or stacked on
  // one). Most surfaces care about "the latest run on this workflow" —
  // canvas dots, the run panel's status chip, the top-bar state.
  const latestRunFor = (
    wid: string | null | undefined,
    opts?: { liveGraphOnly?: boolean },
  ): CurrentRun | null => {
    if (!wid) return null;
    let best: CurrentRun | null = null;
    for (const r of Object.values(liveRuns)) {
      if (r.workflow_id !== wid) continue;
      if (opts?.liveGraphOnly && r.executesOnSnapshot) continue;
      if (!best || r.startedAt > best.startedAt) best = r;
    }
    return best;
  };
  const currentRun = latestRunFor(activeId);
  // The live stream for the run being viewed in snapshot view, if attached.
  const viewingRunLive = viewingRun ? liveRuns[viewingRun.id] ?? null : null;

  // Servers a run reported as needing authentication (null = no popup). Set
  // at most once per run id so dismissing the dialog isn't immediately undone
  // by the next streamed event.
  const [mcpAuthServers, setMcpAuthServers] = useState<string[] | null>(null);
  const mcpAuthPromptedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!currentRun || mcpAuthPromptedRef.current.has(currentRun.id)) return;
    const needing = new Set<string>();
    for (const ev of currentRun.events) {
      if (ev.type === 'mcp_status') {
        for (const [name, st] of Object.entries(ev.servers)) {
          if (st.status === 'needs_auth') needing.add(name);
        }
      } else if (ev.type === 'tool_call_finished') {
        // An MCP tool call that failed with an auth error mid-run (e.g. a
        // token that expired after connect) carries this marker.
        const r = ev.result as { error_type?: string; server?: string } | null | undefined;
        if (r && r.error_type === 'needs_auth' && r.server) needing.add(r.server);
      }
    }
    if (needing.size > 0) {
      mcpAuthPromptedRef.current.add(currentRun.id);
      // Merge into an already-open dialog (a second run can report more
      // servers while the first popup is still up) instead of replacing it.
      setMcpAuthServers((prev) =>
        prev ? Array.from(new Set([...prev, ...needing])) : Array.from(needing),
      );
    }
  }, [currentRun]);

  // An MCP server can be enabled but signed out before any run surfaces it
  // (fresh browser profile, revoked token). Probe the remote servers once at
  // startup — they fail fast and cost one HTTP request each — and prompt if
  // any need sign-in. Local servers are skipped: probing them spawns child
  // processes, and they have no auth to check.
  useEffect(() => {
    let disposed = false;
    (async () => {
      try {
        const res = await probeRemoteStatus(loadSettings().mcp_servers);
        const needing = Object.entries(res)
          .filter(([, st]) => st.status === 'needs_auth')
          .map(([name]) => name);
        if (disposed || needing.length === 0) return;
        setMcpAuthServers((prev) =>
          prev ? Array.from(new Set([...prev, ...needing])) : needing,
        );
      } catch {
        /* backend unreachable — the run-driven prompt still covers it */
      }
    })();
    return () => {
      disposed = true;
    };
  }, []);

  // OAuth LLM providers store their tokens server-side; the localStorage
  // "connected" marker can go stale (token expired with a dead refresh, or
  // signed out elsewhere). Verify the providers the orchestrator/node
  // selections actually use — on load and periodically — and prompt for
  // re-login when the backend says the session is gone. Each provider is
  // prompted at most once per app session so a dismissal sticks.
  const [staleLlmProviders, setStaleLlmProviders] = useState<string[] | null>(null);
  const llmAuthPromptedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let disposed = false;
    const check = async () => {
      const s = loadSettings();
      const ids = new Set<string>();
      for (const sel of [s.orchestrator, s.node]) {
        if (sel && s.connections[sel.providerID]?.method === 'oauth') ids.add(sel.providerID);
      }
      const candidates = Array.from(ids).filter((id) => !llmAuthPromptedRef.current.has(id));
      if (candidates.length === 0) return;
      const stale: string[] = [];
      await Promise.all(
        candidates.map(async (id) => {
          try {
            const st = await fetchAuthStatus(id);
            if (st.status === 'signed_out' || st.status === 'error') stale.push(id);
          } catch {
            /* backend unreachable or unknown provider — don't false-alarm */
          }
        }),
      );
      if (disposed || stale.length === 0) return;
      stale.forEach((id) => llmAuthPromptedRef.current.add(id));
      setStaleLlmProviders((prev) =>
        prev ? Array.from(new Set([...prev, ...stale])) : stale,
      );
    };
    void check();
    const timer = window.setInterval(() => void check(), LLM_AUTH_RECHECK_INTERVAL_MS);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

  const attachToRunRef = useRef(attachToRun);
  useEffect(() => { attachToRunRef.current = attachToRun; });

  const { streamToOrchestrator, abortStream, dropWorkflow } = useOrchestratorStream({
    setChatByWorkflow,
    setOrchestratingIds,
    refreshDetail,
    refreshWorkflows,
    attachToRunRef,
  });

  const { streamToCallChat, cancelCallChat, dropAllStreams } = useCallChatStream({
    setCallChatMessages,
    setStreamingChatIds,
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
      if (isRunning) {
        // We don't know whether this run executes on live or on a divergent
        // snapshot (the click came from the recent-runs list, which doesn't
        // distinguish). Mark it `executesOnSnapshot` so leaving snapshot
        // view to live doesn't overlay potentially-mismatched dots there;
        // snapshot view itself overlays correctly via id lookup. Attaching
        // is a no-op if this run's stream is already open.
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
  // whatever workflow's runs the user was browsing before. The active
  // continuation belongs to a run too, so drop it back to the orchestrator.
  useEffect(() => {
    setViewingRun(null);
    setSelectedSnapshotNodeId(null);
    setActiveContinuation(null);
    setActiveLiveCall(null);
    // Continuations belong to the previous workflow's runs — tear down any
    // in-flight turn sockets and drop their cached transcripts/model picks so
    // a stale stream can't mutate state under the newly-selected workflow.
    dropAllStreams();
    setCallChatMessages({});
    setCallChatModelById({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // viewingRun is captured at the moment snapshot view opens, so for an
  // in-flight run its outputs/node_runs/total_cost are empty. viewingRunLive
  // streams the live deltas, but the SnapshotRunPanel reads its display
  // fields off viewingRun. When the bound run finishes, refetch it so the
  // panel picks up the final outputs, completed node_runs, and total cost.
  // The runner broadcasts `run_finished` over the WS *before* it commits the
  // final Run/NodeRun rows (the persist waits for the subprocess to exit),
  // so the first refetch can race the persist and read a row still marked
  // running — keep retrying until the row turns terminal.
  useEffect(() => {
    if (!viewingRun || !viewingRunLive) return;
    const terminal =
      viewingRunLive.status === 'success' ||
      viewingRunLive.status === 'error' ||
      viewingRunLive.status === 'cancelled';
    if (!terminal) return;
    let cancelled = false;
    let timer: number | undefined;
    const refetch = (attemptsLeft: number) => {
      api.getRun(viewingRun.id)
        .then((fresh) => {
          if (cancelled) return;
          const persisted = fresh.status !== 'running' && fresh.status !== 'pending';
          if (persisted) {
            setViewingRun((cur) => (cur && cur.id === fresh.id ? fresh : cur));
          } else if (attemptsLeft > 0) {
            timer = window.setTimeout(() => refetch(attemptsLeft - 1), 500);
          }
          // Retries exhausted: leave viewingRun stale; user can exit and
          // re-enter to pick up the persisted row.
        })
        .catch(() => { /* leave viewingRun stale; user can exit and re-enter */ });
    };
    refetch(20);
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [viewingRun?.id, viewingRunLive?.status]);

  // Mirror localStorage's orchestrator-model setting so the chat header reflects
  // what's actually being sent over the wire. Refreshed on save (custom event)
  // and on cross-tab edits (`storage`).
  const [orchestratorSelection, setOrchestratorSelection] = useState<ModelSelection | null>(
    () => loadSettings().orchestrator,
  );
  const [hasApiKey, setHasApiKey] = useState<boolean>(() => hasCredsForPreset(loadSettings()));
  useEffect(() => {
    const sync = () => {
      const s = loadSettings();
      setOrchestratorSelection(s.orchestrator);
      setHasApiKey(hasCredsForPreset(s));
    };
    window.addEventListener(SETTINGS_CHANGED_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(SETTINGS_CHANGED_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);
  // Derived — always the orchestrator selection's model id (no separate state).
  const orchestratorModel = orchestratorSelection?.modelID ?? '';

  // Provider/model catalog for the chat model switcher (same source Settings
  // uses). Loaded once; refreshed when the catalog changes (e.g. a provider
  // connect/disconnect triggers a catalog refresh elsewhere).
  useEffect(() => {
    let alive = true;
    const load = () => { void getCatalog().then((c) => { if (alive) setCatalog(c); }).catch(() => {}); };
    load();
    window.addEventListener(CATALOG_CHANGED_EVENT, load);
    return () => { alive = false; window.removeEventListener(CATALOG_CHANGED_EVENT, load); };
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

  // Live-run sockets stay open across workflow switches so background runs
  // keep streaming; only tear them down when the app unmounts.
  useEffect(() => {
    return () => closeAllSockets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeWorkflow = workflows.find((w) => w.id === activeId) ?? null;
  const messages = activeId ? chatByWorkflow[activeId] ?? [] : [];

  /**
   * Send a user message. Lazily creates a workflow + chat context if one
   * doesn't exist yet, so the user can land on the workspace and just start
   * typing. The first message also becomes the workflow's name when it's still
   * the placeholder.
   */
  const handleSend = async (text: string) => {
    const attachments = imageAttachments.attachments.map((a) => ({
      dataUrl: a.dataUrl,
      filename: a.filename,
      mime: a.mime,
    }));
    if (attachments.length > 0) imageAttachments.clear();
    let wid = activeId;
    let isFirstMessage = false;

    if (!wid) {
      // No active workflow — create one named after this message.
      const w = await api.createWorkflow(deriveWorkflowName(text));
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
    if (
      !isFirstMessage &&
      cur &&
      (cur.name === DEFAULT_WORKFLOW_NAME || cur.name === 'untitled session')
    ) {
      const nextName = deriveWorkflowName(text);
      api.patchWorkflow(wid, { name: nextName }).then(() => refreshWorkflows()).catch(() => {});
    }

    streamToOrchestrator(wid, sid, text, attachments);
  };

  const cancelOrchestrator = async () => {
    if (!activeId) return;
    const sid = sessionByWorkflow[activeId];
    if (!sid) return;
    try { await api.cancelOrchestratorTurn(sid); } catch { /* ignore */ }
    abortStream(activeId);
  };

  /**
   * "new workflow" drops into the Hero empty state without touching the
   * backend. handleSend's `!wid` branch lazily creates the workflow and chat
   * context when the user sends their first message, so we avoid leaving
   * orphaned placeholder rows behind every time someone clicks +.
   */
  const handleNew = () => {
    setActiveId(null);
    setDetail(null);
    setView('workflow');
    setSelectedNodeId(null);
  };

  const handleDelete = async (id: string) => {
    await api.deleteWorkflow(id);
    if (id === activeId) {
      setActiveId(null);
      setDetail(null);
    }
    dropWorkflowRuns(id);
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

  const activateImportedProject = async (workflow: Workflow) => {
    setWorkflows((prev) => [workflow, ...prev.filter((w) => w.id !== workflow.id)]);
    activeIdRef.current = workflow.id;
    setActiveId(workflow.id);
    setView('workflow');
    setSelectedNodeId(null);
    setSelectedSnapshotNodeId(null);
    setViewingRun(null);
    setChatByWorkflow((prev) => ({ ...prev, [workflow.id]: [] }));
    try {
      const d = await api.getWorkflow(workflow.id);
      if (activeIdRef.current === workflow.id) setDetail(d);
    } catch {
      if (activeIdRef.current === workflow.id) setDetail(null);
    }
  };

  const handleOpenImport = () => {
    setImportDraft('');
    setDialog({ kind: 'import-project' });
  };

  const handleConfirmImport = async () => {
    try {
      const parsed = parseWorkflowExport(importDraft);
      const imported = await api.importWorkflow(parsed);
      setDialog({ kind: 'none' });
      setImportDraft('');
      await activateImportedProject(imported);
    } catch (e) {
      setDialog({
        kind: 'alert',
        message: `couldn't import project: ${e instanceof Error ? e.message : String(e)}`,
        variant: 'error',
      });
    }
  };

  const handleExportCanvas = async () => {
    try {
      let exported;
      if (viewingRun) {
        exported = snapshotToExport(viewingRun, activeWorkflow?.name);
        if (!exported) throw new Error('this run has no graph snapshot to export');
      } else {
        if (!activeId) return;
        exported = await api.exportWorkflow(activeId);
      }
      setExportDraft(formatWorkflowExport(exported));
      setDialog({ kind: 'export-project' });
    } catch (e) {
      setDialog({
        kind: 'alert',
        message: `couldn't export project: ${e instanceof Error ? e.message : String(e)}`,
        variant: 'error',
      });
    }
  };

  const startRun = async (inputs: Record<string, unknown>) => {
    if (!detail) return;
    const run = await api.startRun(detail.id, inputs);
    attachToRun(run.id, detail.id, run.status);
    // Drop the user on the run's detail page so they can watch this specific
    // run's progress and see its inputs/outputs as they land. The run carries
    // its own workflow_snapshot from the start_run response, so we can enter
    // snapshot view immediately without a follow-up fetch.
    if (run.workflow_snapshot) {
      setSelectedNodeId(null);
      setSelectedSnapshotNodeId(null);
      setViewingRun(run);
    }
  };

  /** Forward an error from a run/node into the orchestrator chat as a user message. */
  const sendErrorToOrchestrator = (message: string) => {
    setRightPanelMode('chat');
    handleSend(message);
  };

  const selectedNode = detail?.nodes.find((n) => n.id === selectedNodeId);
  const isOrchestrating = !!activeId && orchestratingIds.has(activeId);

  const clearChatContext = () => {
    if (!activeId || isOrchestrating) return;
    const sid = sessionByWorkflow[activeId];
    if (!sid) {
      setChatByWorkflow((prev) => ({ ...prev, [activeId]: [] }));
      return;
    }
    setDialog({ kind: 'confirm-clear-context' });
  };

  const doClearChatContext = async () => {
    if (!activeId) return;
    const sid = sessionByWorkflow[activeId];
    if (!sid) return;
    setDialog({ kind: 'none' });
    try {
      await api.clearSessionMessages(sid);
      setChatByWorkflow((prev) => ({ ...prev, [activeId]: [] }));
    } catch (e) {
      setDialog({
        kind: 'alert',
        message: `couldn't clear context: ${e instanceof Error ? e.message : String(e)}`,
        variant: 'error',
      });
    }
  };

  // --- call_llm in the chat pane ----------------------------------------
  // The shared chat pane shows one of: the orchestrator (default), a finished
  // call's continuation (composer enabled), or an in-flight call streaming live
  // (composer disabled until it lands). Reached only from a node's "llm calls"
  // tab. A live call transitions to a continuation when its run persists.
  const variantsFor = (sel: ModelSelection | null): string[] =>
    catalog && sel
      ? (findModel(catalog, sel.providerID, sel.modelID)?.variants ?? [])
      : [];

  // "continue →" on a finished call: create-or-get its continuation, seed the
  // transcript on first open (re-opening must not clobber a live in-memory
  // one), and show it in the chat pane.
  const openContinuation = async (nodeRunId: string, callId: string) => {
    try {
      const chat = await api.openCallChat(nodeRunId, callId);
      setCallChatMessages((prev) =>
        prev[chat.id] ? prev : { ...prev, [chat.id]: messagesToChat(chat.messages) });
      setCallChatModelById((prev) =>
        prev[chat.id]
          ? prev
          : {
              ...prev,
              [chat.id]: {
                providerID: chat.provider_id,
                modelID: chat.model,
                variant: chat.variant || null,
              },
            });
      setActiveLiveCall(null);
      setActiveContinuation(chat);
      setRightPanelMode('chat');
    } catch (e) {
      setDialog({
        kind: 'alert',
        message: `couldn't continue this call: ${e instanceof Error ? e.message : String(e)}`,
        variant: 'error',
      });
    }
  };

  // "continue →" on an in-flight call: stream it into the chat pane (read-only)
  // from the run's live events.
  const openLiveCall = (runId: string, nodeId: string, callId: string, label: string) => {
    setActiveContinuation(null);
    setActiveLiveCall({ runId, nodeId, callId, label });
    setRightPanelMode('chat');
  };

  // The live call's bubbles, derived from its run's event stream — recomputed as
  // events arrive, so the chat pane streams. Keyed on the *active* run's events
  // only, so an event on some other concurrent run doesn't re-aggregate here.
  const activeRun = activeLiveCall ? liveRuns[activeLiveCall.runId] ?? null : null;
  const liveCall = useMemo(() => {
    if (!activeLiveCall || !activeRun) return null;
    const t = aggregateEvents(activeRun.events).find((x) => x.node_id === activeLiveCall.nodeId);
    return t?.llmCalls.find((c) => c.call_id === activeLiveCall.callId) ?? null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeLiveCall, activeRun?.events]);

  // When a live call's run finishes + persists, swap to its continuation so the
  // composer enables. Retries briefly — node_runs commit just after run_finished.
  const liveRunStatus = activeLiveCall ? liveRuns[activeLiveCall.runId]?.status : undefined;
  useEffect(() => {
    if (!activeLiveCall) return;
    if (liveRunStatus !== 'success' && liveRunStatus !== 'error' && liveRunStatus !== 'cancelled') {
      return;
    }
    const target = activeLiveCall;
    let cancelled = false;
    let tries = 0;
    let timer: number | undefined;
    const attempt = async () => {
      tries += 1;
      try {
        const full = await api.getRun(target.runId);
        const nr = full.node_runs.find((n) => n.node_id === target.nodeId);
        if (nr) {
          let chat: CallChat;
          try {
            chat = await api.openCallChat(nr.id, target.callId);
          } catch (e) {
            // A 404 here is terminal, not transient: the call has no continuable
            // transcript. That can mean it failed before finishing (an errored
            // call_llm is never recorded) OR its transcript exceeded the persist
            // budget — the backend returns distinct details, but both conclude
            // "can't continue", so use one neutral message rather than parse the
            // body. Stop retrying and drop the now-pointless read-only live view.
            if (e instanceof ApiError && e.status === 404) {
              if (cancelled) return;
              setActiveLiveCall((cur) => (cur === target ? null : cur));
              setDialog({
                kind: 'alert',
                message:
                  'this call finished but can’t be continued — its conversation wasn’t saved (it may have failed, or its transcript was too large to persist).',
                variant: 'error',
              });
              return;
            }
            throw e; // transient (e.g. node_run not committed yet) → retry
          }
          if (cancelled) return;
          setCallChatMessages((prev) =>
            prev[chat.id] ? prev : { ...prev, [chat.id]: messagesToChat(chat.messages) });
          setCallChatModelById((prev) =>
            prev[chat.id]
              ? prev
              : { ...prev, [chat.id]: { providerID: chat.provider_id, modelID: chat.model, variant: chat.variant || null } });
          setActiveLiveCall((cur) => (cur === target ? null : cur));
          setActiveContinuation(chat);
          return;
        }
      } catch {
        // node_run not written yet → retry below.
      }
      if (!cancelled && tries < 6) timer = window.setTimeout(attempt, 600);
    };
    void attempt();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeLiveCall, liveRunStatus]);

  // The active chat conversation: live > continuation > orchestrator.
  const cont = activeContinuation;
  const chatMessages = activeLiveCall
    ? (liveCall ? liveCallToChat(liveCall) : [])
    : cont
      ? (callChatMessages[cont.id] ?? [])
      : messages;
  // A live call is read-only (can't send while it streams / before it persists).
  const chatDisabled = activeLiveCall
    ? true
    : cont
      ? streamingChatIds.has(cont.id)
      : isOrchestrating;
  const chatSelection: ModelSelection | null = activeLiveCall
    ? null
    : cont
      ? (callChatModelById[cont.id] ?? null)
      : orchestratorSelection;
  const chatModelLabel = activeLiveCall
    ? (liveCall?.model ?? '')
    : cont
      ? (chatSelection?.modelID ?? '')
      : orchestratorModel;
  const conversationLabel = activeLiveCall?.label ?? cont?.label;
  const onChatSend = (text: string) => {
    if (activeLiveCall) return; // read-only while live
    if (cont) void streamToCallChat(cont.id, text, callChatModelById[cont.id] ?? null);
    else void handleSend(text);
  };
  const onChatCancel = () => {
    if (activeLiveCall) return;
    if (cont) cancelCallChat(cont.id);
    else void cancelOrchestrator();
  };
  const backToOrchestrator = () => {
    setActiveContinuation(null);
    setActiveLiveCall(null);
  };
  // Model edits: a continuation's stay per-call (in memory); the orchestrator's
  // persist to Settings. A live call's model is fixed, so no picker (below).
  const onChatPickModel = (sel: ModelSelection) => {
    if (cont) {
      setCallChatModelById((prev) => ({ ...prev, [cont.id]: sel }));
    } else {
      const s = loadSettings();
      saveSettings({ ...s, orchestrator: sel });
    }
  };
  const onChatCycleVariant = (next: string | null) => {
    if (cont) {
      setCallChatModelById((prev) => {
        const cur = prev[cont.id];
        return cur ? { ...prev, [cont.id]: { ...cur, variant: next } } : prev;
      });
    } else {
      const s = loadSettings();
      if (s.orchestrator) saveSettings({ ...s, orchestrator: { ...s.orchestrator, variant: next } });
    }
  };

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
    ? {
        ...Object.fromEntries(
          viewingRun.node_runs.map((nr) => [nr.node_id, nr.status]),
        ),
        ...(viewingRunLive ? viewingRunLive.nodeStates : {}),
      }
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
        }}
        onNew={handleNew}
        onRename={handleRename}
        onDelete={handleDelete}
        onOpenSettings={() => setView('settings')}
        onOpenRun={() => {
          // RunPanel is the default right-side surface — "Runs" in the
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
                setRightPanelMode('chat');
                handleSend(text);
              }}
              onImport={handleOpenImport}
              onOpenSettings={() => setView('settings')}
              pendingAttachments={imageAttachments.attachments}
              onRemoveAttachment={imageAttachments.remove}
              draggingFile={imageAttachments.dragging}
              attachmentNotice={imageAttachments.notice}
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
                {/* Canvas (and the empty-canvas placeholder) need
                 * `position: relative` to host React Flow's absolute layout.
                 * Action bar / banner stack above and below via flex; this
                 * wrapper takes the rest. */}
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
                        onSelectNode={(id) => {
                          setSelectedSnapshotNodeId(id);
                          // Clicking a canvas node from the chat tab should
                          // surface the node detail; the chat hides the
                          // workspace where NodePanel lives. Pane deselects
                          // (id === null) shouldn't yank the user out of chat.
                          if (id !== null) setRightPanelMode('workspace');
                        }}
                        nodeStates={snapshotNodeStates}
                        headerActions={
                          <>
                            <button
                              type="button"
                              onClick={handleExportCanvas}
                              className="snapshot-action-btn snapshot-action-btn--secondary"
                              title="view and copy this snapshot as JSON"
                            >
                              export
                            </button>
                            <button
                              type="button"
                              onClick={exitSnapshotView}
                              className="snapshot-action-btn"
                              title="return to the live, editable canvas"
                            >
                              back to live
                            </button>
                          </>
                        }
                      />
                    ) : null;
                  })()
                ) : detail ? (
                  <Canvas
                    detail={detail}
                    selectedNodeId={selectedNodeId}
                    onSelectNode={(id) => {
                      setSelectedNodeId(id);
                      if (id !== null) setRightPanelMode('workspace');
                    }}
                    // Overlay live node states for the latest run executing
                    // on the live graph (manual run, orchestrator-triggered
                    // run). Snapshot reruns are excluded — their snapshot can
                    // diverge from live, so dots may apply to wrong nodes or
                    // miss entirely. Snapshot view is the right place to
                    // watch those; the rerun handler keeps the user there.
                    nodeStates={
                      latestRunFor(detail.id, { liveGraphOnly: true })?.nodeStates
                    }
                    headerActions={
                      <button
                        type="button"
                        onClick={handleExportCanvas}
                        className="snapshot-action-btn snapshot-action-btn--secondary"
                        title="view and copy this project as JSON"
                      >
                        export
                      </button>
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
                      <span className="italic-em">new project</span> to start fresh.
                    </div>
                  </div>
                )}
                </div>
                {viewingRun && (
                  <SnapshotBanner run={viewingRun} />
                )}
              </div>

              {/* right 3/5 — workspace (run/node) or orchestrator chat,
                  toggled via the tabs at the top. */}
              <div
                style={{
                  flex: 3,
                  minWidth: 0,
                  display: 'flex',
                  flexDirection: 'column',
                }}
              >
                <RightPanelTabs
                  mode={rightPanelMode}
                  setMode={setRightPanelMode}
                  onWorkspaceTab={() => {
                    // The workspace tab always lands on the run list, no
                    // matter what surface was left behind — a node config
                    // panel or a snapshot run view would otherwise stick
                    // around and greet the user instead of the runs.
                    setRightPanelMode('workspace');
                    setSelectedNodeId(null);
                    exitSnapshotView();
                  }}
                  showChatActivityDot={isOrchestrating && rightPanelMode !== 'chat'}
                />
                <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
                  {rightPanelMode === 'chat' ? (
                    <div
                      style={{
                        position: 'absolute',
                        inset: 0,
                        display: 'flex',
                        flexDirection: 'column',
                      }}
                    >
                      <ChatPanel
                        // Remount when the conversation identity changes (live
                        // call / continuation / orchestrator) so transient
                        // panel state — composer draft, scroll position — never
                        // bleeds from one conversation into the next.
                        key={
                          activeLiveCall
                            ? `live:${activeLiveCall.runId}:${activeLiveCall.nodeId}:${activeLiveCall.callId}`
                            : cont
                              ? `cont:${cont.id}`
                              : `orch:${activeId ?? 'none'}`
                        }
                        messages={chatMessages}
                        onSend={onChatSend}
                        pendingAttachments={imageAttachments.attachments}
                        onRemoveAttachment={imageAttachments.remove}
                        draggingFile={imageAttachments.dragging}
                        attachmentNotice={imageAttachments.notice}
                        onCancel={activeLiveCall ? undefined : onChatCancel}
                        disabled={chatDisabled}
                        modelLabel={chatModelLabel}
                        onClearContext={clearChatContext}
                        conversationLabel={conversationLabel}
                        onBack={backToOrchestrator}
                        modelSelection={chatSelection}
                        modelVariants={variantsFor(chatSelection)}
                        catalog={catalog}
                        // A live call's model is fixed — no picker while streaming.
                        onPickModel={activeLiveCall ? undefined : onChatPickModel}
                        onCycleVariant={activeLiveCall ? undefined : onChatCycleVariant}
                        onViewRun={(runId) => {
                          // Snapshot view renders inside the workspace tab —
                          // flip back from chat so the run panel is actually
                          // visible after the click.
                          setRightPanelMode('workspace');
                          void enterSnapshotView(runId);
                        }}
                      />
                    </div>
                  ) : (
                    <>
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
                              // Pass the viewed run's live stream so the trace
                              // tab streams events when this snapshot view is
                              // bound to an in-flight run (rerun-from-snapshot,
                              // or recent-run click on a running run). Without
                              // it, the trace would fall back to
                              // viewingRun.node_runs — empty for runs that
                              // haven't materialised yet.
                              currentRun={viewingRunLive}
                              onSendErrorToOrchestrator={sendErrorToOrchestrator}
                              onContinue={openContinuation}
                              onViewLive={openLiveCall}
                            />
                          );
                        }
                        return (
                          <SnapshotRunPanel
                            run={viewingRun}
                            currentRun={viewingRunLive}
                            onExit={exitSnapshotView}
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
                          onContinue={openContinuation}
                          onViewLive={openLiveCall}
                        />
                      )}
                      {!viewingRun && detail && !selectedNode && (
                        <RunPanel
                          workflow={detail}
                          currentRun={currentRun}
                          onStart={startRun}
                          onViewRunOnCanvas={enterSnapshotView}
                          onRunDeleted={dropRun}
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
                            no project yet.
                          </div>
                          <div style={{ fontSize: 13, maxWidth: 420, textAlign: 'center', lineHeight: 1.6 }}>
                            open the{' '}
                            <button
                              type="button"
                              onClick={() => setRightPanelMode('chat')}
                              className="italic-em"
                              style={{
                                background: 'none',
                                border: 0,
                                padding: 0,
                                color: 'var(--accent-ink)',
                                cursor: 'pointer',
                                font: 'inherit',
                                fontStyle: 'italic',
                              }}
                            >
                              chat
                            </button>{' '}
                            tab and describe a problem, or click{' '}
                            <span className="italic-em">new project</span> to start fresh.
                          </div>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </main>

      {dialog.kind === 'alert' && (
        <AlertDialog
          message={dialog.message}
          variant={dialog.variant}
          onClose={() => setDialog({ kind: 'none' })}
        />
      )}
      {dialog.kind === 'confirm-clear-context' && (
        <ConfirmDialog
          title="clear chat context"
          message="clear this chat context? the project graph and run history will stay."
          confirmLabel="clear"
          variant="danger"
          onConfirm={doClearChatContext}
          onCancel={() => setDialog({ kind: 'none' })}
        />
      )}
      {dialog.kind === 'import-project' && (
        <ProjectTransferPanel
          mode="import"
          value={importDraft}
          onChange={setImportDraft}
          onConfirm={handleConfirmImport}
          onClose={() => {
            setDialog({ kind: 'none' });
            setImportDraft('');
          }}
        />
      )}
      {dialog.kind === 'export-project' && (
        <ProjectTransferPanel
          mode="export"
          value={exportDraft}
          onClose={() => {
            setDialog({ kind: 'none' });
            setExportDraft('');
          }}
        />
      )}
      {/* Re-auth prompts. The LLM one is a corner toast (non-blocking), the
          MCP one a small modal, so they can coexist. Both are suppressed while
          Settings is open: it has its own sign-in flows and shows the same
          warnings inline, and two concurrent OAuth flows would contend for
          the loopback callback. */}
      {view !== 'settings' && staleLlmProviders && (
        <LlmAuthToast
          providers={staleLlmProviders}
          onOpenSettings={() => {
            setStaleLlmProviders(null);
            setView('settings');
          }}
          onClose={() => setStaleLlmProviders(null)}
        />
      )}
      {view !== 'settings' && mcpAuthServers && (
        <McpAuthDialog
          servers={mcpAuthServers}
          onOpenSettings={() => {
            setMcpAuthServers(null);
            setView('settings');
          }}
          onClose={() => setMcpAuthServers(null)}
        />
      )}
    </div>
  );
}

function RightPanelTabs({
  mode,
  setMode,
  onWorkspaceTab,
  showChatActivityDot,
}: {
  mode: 'workspace' | 'chat';
  setMode: (m: 'workspace' | 'chat') => void;
  /** Clicking the workspace tab resets the workspace to its default surface
   * (the run list) rather than just toggling visibility, so the handler
   * differs from a plain setMode('workspace'). */
  onWorkspaceTab: () => void;
  /** When true, paint a small accent dot on the chat tab to signal that
   * the orchestrator is doing work the user can't currently see. */
  showChatActivityDot: boolean;
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'stretch',
        gap: 0,
        padding: '0 12px',
        borderBottom: '1px solid var(--rule)',
        background: 'var(--paper)',
        flexShrink: 0,
      }}
    >
      <PanelTabButton
        active={mode === 'workspace'}
        onClick={onWorkspaceTab}
      >
        workspace
      </PanelTabButton>
      <PanelTabButton
        active={mode === 'chat'}
        onClick={() => setMode('chat')}
      >
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          chat
          {showChatActivityDot && (
            <span
              aria-label="orchestrator is working"
              style={{
                width: 5,
                height: 5,
                borderRadius: 999,
                background: 'var(--accent)',
                animation: 'pulse 1.4s ease-in-out infinite',
              }}
            />
          )}
        </span>
      </PanelTabButton>
    </div>
  );
}

function PanelTabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="smallcaps"
      aria-pressed={active}
      style={{
        background: 'transparent',
        border: 0,
        padding: '10px 14px',
        cursor: 'pointer',
        color: active ? 'var(--ink)' : 'var(--ink-4)',
        fontSize: 10.5,
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        borderBottom: active
          ? '1.5px solid var(--accent)'
          : '1.5px solid transparent',
        marginBottom: -1,
        transition: 'color .15s, border-color .15s',
      }}
    >
      {children}
    </button>
  );
}
