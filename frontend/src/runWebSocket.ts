import { useRef, useState } from 'react';
import { api } from './api';
import { ensureNotificationPermission, notifyRunFinished } from './notify';
import type { CurrentRun, NodeRunStatus, RunEvent, RunStatus, Workflow } from './types';

/** Clone nodeStates only when a mutation actually changes a value. Keeps
 * the prior reference on log/chunk events so the canvas doesn't rebuild
 * every React Flow node on each streamed token. */
function patchNodeStates(
  cur: Record<string, NodeRunStatus>,
  mutate: (next: Record<string, NodeRunStatus>) => void,
): Record<string, NodeRunStatus> {
  const next = { ...cur };
  mutate(next);
  for (const id of Object.keys(cur)) {
    if (cur[id] !== next[id]) return next;
  }
  for (const id of Object.keys(next)) {
    if (!(id in cur)) return next;
  }
  return cur;
}

/** Apply one streamed RunEvent to the in-memory CurrentRun. Pure for
 * unit-testability — the caller passes in the prior state and stores the
 * returned next state. */
export function applyRunEvent(cur: CurrentRun, ev: RunEvent): CurrentRun {
  let nodeStates = cur.nodeStates;
  let nextStatus = cur.status;
  let finalOutputs = cur.finalOutputs;
  let error = cur.error;
  let totalCost = cur.totalCost;

  if (ev.type === 'run_started') {
    nextStatus = 'running';
    nodeStates = patchNodeStates(nodeStates, (next) => {
      for (const nodeId of ev.order) {
        if (!next[nodeId]) next[nodeId] = 'pending';
      }
    });
  } else if (ev.type === 'node_started') {
    if (nodeStates[ev.node_id] !== 'running') {
      nodeStates = patchNodeStates(nodeStates, (next) => {
        next[ev.node_id] = 'running';
      });
    }
  } else if (ev.type === 'node_finished') {
    if (nodeStates[ev.node_id] !== ev.status) {
      nodeStates = patchNodeStates(nodeStates, (next) => {
        next[ev.node_id] = ev.status;
      });
    }
  } else if (ev.type === 'run_finished') {
    nextStatus = ev.status;
    finalOutputs = ev.outputs;
    error = ev.error;
    totalCost = ev.total_cost;
    // Defensive sweep: if the runner ever fails to emit node_finished
    // for a node (escaped exception, dropped event, cancel mid-flight),
    // the dot would be stuck on running/pending forever. Once
    // run_finished arrives the runner is done — force any non-terminal
    // state to a sensible terminal one. "running" → "error" (the node
    // started but never resolved); "pending" → "skipped" (never
    // started). On a cancelled run prefer "skipped" for both — those
    // nodes didn't fail, they just got interrupted.
    nodeStates = patchNodeStates(nodeStates, (next) => {
      for (const nid of Object.keys(next)) {
        const s = next[nid];
        if (s === 'running') {
          next[nid] = ev.status === 'cancelled' ? 'skipped' : 'error';
        } else if (s === 'pending') {
          next[nid] = 'skipped';
        }
      }
    });
  }

  return {
    ...cur,
    events: [...cur.events, ev],
    nodeStates,
    status: nextStatus,
    finalOutputs,
    error,
    totalCost,
  };
}

function isTerminal(status: RunStatus): boolean {
  return status === 'success' || status === 'error' || status === 'cancelled';
}

/** Hook that owns the live-run WebSockets. Multiple runs — across workflows
 * or stacked on one workflow — can be attached at once; each gets its own
 * socket and its own `CurrentRun` entry in the returned map, so switching
 * workflows mid-run never drops the stream. Sockets are closed when their
 * workflow is dropped (deletion) or on unmount via `closeAllSockets`. */
export function useRunWebSocket(workflows: Workflow[]) {
  const [runs, setRuns] = useState<Record<string, CurrentRun>>({});
  const runsRef = useRef<Record<string, CurrentRun>>({});
  const socketsRef = useRef<Map<string, WebSocket>>(new Map());
  // Held as a ref so the WS message handler always sees the latest list
  // without forcing a reconnect when the user renames a workflow mid-run.
  const workflowsRef = useRef<Workflow[]>(workflows);
  workflowsRef.current = workflows;

  const commitRuns = (next: Record<string, CurrentRun>) => {
    runsRef.current = next;
    setRuns(next);
  };

  const attachToRun = (
    runId: string,
    workflowId: string,
    initialStatus: RunStatus = 'running',
    executesOnSnapshot: boolean = false,
  ) => {
    // Already streaming this run — re-attaching would clobber its
    // accumulated events with an empty state.
    if (socketsRef.current.has(runId)) return;
    ensureNotificationPermission();
    const workflowName =
      workflowsRef.current.find((w) => w.id === workflowId)?.name ?? 'project';
    const startedAt = Date.now();
    const fresh: CurrentRun = {
      id: runId,
      workflow_id: workflowId,
      status: initialStatus,
      startedAt,
      events: [],
      nodeStates: {},
      finalOutputs: null,
      error: null,
      totalCost: 0,
      executesOnSnapshot,
    };
    // Finished runs on the same workflow are superseded by the new
    // attachment — pruning them keeps the map (and its event buffers) from
    // growing without bound over a long session. In-flight runs stay.
    const kept = Object.fromEntries(
      Object.entries(runsRef.current).filter(
        ([, r]) => !(r.workflow_id === workflowId && isTerminal(r.status)),
      ),
    );
    commitRuns({ ...kept, [runId]: fresh });
    const ws = new WebSocket(api.runEventsUrl(runId));
    socketsRef.current.set(runId, ws);
    ws.onmessage = (e) => {
      let ev: RunEvent;
      try { ev = JSON.parse(e.data) as RunEvent; } catch { return; }
      if (ev.type === 'run_deleted') {
        // The run's row is gone (deleted server-side, or we attached to a
        // run that no longer exists). Drop the entry entirely — every
        // surface keyed off this map must stop showing a run that doesn't
        // exist. The server closes the socket after sending this.
        if (runId in runsRef.current) {
          const { [runId]: _, ...rest } = runsRef.current;
          commitRuns(rest);
        }
        ws.close();
        return;
      }
      if (ev.type === 'run_finished') {
        notifyRunFinished({
          runId,
          workflowName,
          status: ev.status,
          error: ev.error,
          outputs: ev.outputs,
          durationMs: Date.now() - startedAt,
        });
      }
      const cur = runsRef.current[runId];
      if (!cur) return;
      commitRuns({ ...runsRef.current, [runId]: applyRunEvent(cur, ev) });
    };
    ws.onerror = () => { /* keep state; close handler will fire */ };
    ws.onclose = () => {
      if (socketsRef.current.get(runId) === ws) socketsRef.current.delete(runId);
    };
  };

  /** Forget one run (used when the user deletes it from a run list). */
  const dropRun = (runId: string) => {
    socketsRef.current.get(runId)?.close();
    socketsRef.current.delete(runId);
    if (!(runId in runsRef.current)) return;
    const { [runId]: _, ...rest } = runsRef.current;
    commitRuns(rest);
  };

  /** Forget all runs for a workflow (used when it's deleted). */
  const dropWorkflowRuns = (workflowId: string) => {
    for (const [rid, r] of Object.entries(runsRef.current)) {
      if (r.workflow_id !== workflowId) continue;
      socketsRef.current.get(rid)?.close();
      socketsRef.current.delete(rid);
    }
    commitRuns(
      Object.fromEntries(
        Object.entries(runsRef.current).filter(([, r]) => r.workflow_id !== workflowId),
      ),
    );
  };

  const closeAllSockets = () => {
    for (const ws of socketsRef.current.values()) ws.close();
    socketsRef.current.clear();
  };

  return { runs, attachToRun, dropRun, dropWorkflowRuns, closeAllSockets };
}
