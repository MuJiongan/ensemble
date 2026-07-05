import { useEffect, useState } from 'react';
import { api } from './api';
import type { RunEvent } from './types';

function isTerminalEvent(ev: RunEvent): boolean {
  return ev.type === 'run_finished' || ev.type === 'run_deleted';
}

function belongsToNode(ev: RunEvent, nodeId: string): boolean {
  if (ev.type === 'run_started' || ev.type === 'run_finished' || ev.type === 'run_deleted') {
    return true;
  }
  return 'node_id' in ev && ev.node_id === nodeId;
}

export function useNodeRunEvents(
  runId: string | null | undefined,
  nodeId: string | null | undefined,
  enabled = true,
): RunEvent[] {
  const [events, setEvents] = useState<RunEvent[]>([]);

  useEffect(() => {
    setEvents([]);
    if (!enabled || !runId || !nodeId) return;

    let closed = false;
    const ws = new WebSocket(api.runEventsUrl(runId, { nodeId }));
    ws.onmessage = (e) => {
      if (closed) return;
      let ev: RunEvent;
      try {
        ev = JSON.parse(e.data) as RunEvent;
      } catch {
        return;
      }
      if (!belongsToNode(ev, nodeId)) return;
      setEvents((prev) => [...prev, ev]);
      if (isTerminalEvent(ev)) ws.close();
    };
    ws.onerror = () => {
      /* close handler handles cleanup */
    };
    ws.onclose = () => {
      closed = true;
    };

    return () => {
      closed = true;
      ws.close();
    };
  }, [enabled, runId, nodeId]);

  return events;
}
