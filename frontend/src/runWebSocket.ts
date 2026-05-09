import { useRef, useState, type SetStateAction } from 'react';
import { api } from './api';
import { ensureNotificationPermission, notifyRunFinished } from './notify';
import type { CurrentRun, RunEvent, RunStatus, Workflow } from './types';

/** Apply one streamed RunEvent to the in-memory CurrentRun. Pure for
 * unit-testability — the caller passes in the prior state and stores the
 * returned next state. */
export function applyRunEvent(cur: CurrentRun, ev: RunEvent): CurrentRun {
  const nextStates = { ...cur.nodeStates };
  let nextStatus = cur.status;
  let finalOutputs = cur.finalOutputs;
  let error = cur.error;
  let totalCost = cur.totalCost;

  if (ev.type === 'run_started') {
    nextStatus = 'running';
    for (const nodeId of ev.order) {
      if (!nextStates[nodeId]) nextStates[nodeId] = 'pending';
    }
  } else if (ev.type === 'node_started') {
    nextStates[ev.node_id] = 'running';
  } else if (ev.type === 'node_finished') {
    nextStates[ev.node_id] = ev.status;
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
    for (const nid of Object.keys(nextStates)) {
      const s = nextStates[nid];
      if (s === 'running') {
        nextStates[nid] = ev.status === 'cancelled' ? 'skipped' : 'error';
      } else if (s === 'pending') {
        nextStates[nid] = 'skipped';
      }
    }
  }

  return {
    ...cur,
    events: [...cur.events, ev],
    nodeStates: nextStates,
    status: nextStatus,
    finalOutputs,
    error,
    totalCost,
  };
}

/** Hook that owns the live-run WebSocket: opening the socket, applying
 * incoming events to `currentRun`, and surfacing the desktop notification
 * when a run finishes. Caller is responsible for closing the socket on
 * workflow switch (via `closeWs`). */
export function useRunWebSocket(workflows: Workflow[]) {
  const [currentRun, setCurrentRun] = useState<CurrentRun | null>(null);
  const currentRunRef = useRef<CurrentRun | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // Held as a ref so the WS message handler always sees the latest list
  // without forcing a reconnect when the user renames a workflow mid-run.
  const workflowsRef = useRef<Workflow[]>(workflows);
  workflowsRef.current = workflows;

  const commitCurrentRun = (next: SetStateAction<CurrentRun | null>) => {
    const value =
      typeof next === 'function'
        ? (next as (prev: CurrentRun | null) => CurrentRun | null)(currentRunRef.current)
        : next;
    currentRunRef.current = value;
    setCurrentRun(value);
  };

  const attachToRun = (
    runId: string,
    workflowId: string,
    initialStatus: RunStatus = 'running',
    executesOnSnapshot: boolean = false,
  ) => {
    ensureNotificationPermission();
    const workflowName =
      workflowsRef.current.find((w) => w.id === workflowId)?.name ?? 'project';
    const startedAt = Date.now();
    commitCurrentRun({
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
    });
    wsRef.current?.close();
    const ws = new WebSocket(api.runEventsUrl(runId));
    wsRef.current = ws;
    ws.onmessage = (e) => {
      let ev: RunEvent;
      try { ev = JSON.parse(e.data) as RunEvent; } catch { return; }
      if (ev.type === 'run_finished') {
        notifyRunFinished({
          runId,
          workflowName,
          status: ev.status,
          error: ev.error,
          outputs: ev.outputs,
          totalCost: ev.total_cost,
          durationMs: Date.now() - startedAt,
        });
      }
      const cur = currentRunRef.current;
      if (!cur || cur.id !== runId) return;
      const next = applyRunEvent(cur, ev);
      currentRunRef.current = next;
      setCurrentRun(next);
    };
    ws.onerror = () => { /* keep state; close handler will fire */ };
    ws.onclose = () => { if (wsRef.current === ws) wsRef.current = null; };
  };

  const closeWs = () => {
    wsRef.current?.close();
    wsRef.current = null;
  };

  return { currentRun, setCurrentRun: commitCurrentRun, attachToRun, closeWs };
}
