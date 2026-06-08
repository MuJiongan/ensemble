import { useEffect, useRef } from 'react';
import { api } from './api';
import { GRAPH_MUTATING_TOOLS, WORKFLOW_METADATA_TOOLS } from './appHelpers';
import type { AssistantMessage, ChatMessage } from './components/ChatPanel';
import type { OrchestratorEvent } from './types';

type AssistantMutation = (a: AssistantMessage) => AssistantMessage;

/** Map an OrchestratorEvent to an assistant-bubble mutation, or `null` if
 * the event has no bubble effect (graph-mutator side effects, done — caller
 * handles those separately). `run_started` returns a mutation that stashes
 * the run_id on the pending `run_workflow` block, so the chat card is
 * clickable while still running; the caller still also handles run_started
 * separately to attach the run panel WS. Pure for unit-testability. */
export function reduceAssistantOnEvent(ev: OrchestratorEvent): AssistantMutation | null {
  if (ev.kind === 'assistant_thinking_chunk' && ev.text) {
    // Reasoning streams before visible content. Append to the trailing
    // thinking block when it's still live (no p/tool block has appeared
    // since); otherwise start a fresh thinking block — the model is
    // taking a second think mid-turn.
    return (a) => {
      const content = [...a.content];
      const last = content[content.length - 1];
      if (last && last.t === 'thinking') {
        content[content.length - 1] = { ...last, text: last.text + ev.text };
      } else {
        content.push({ t: 'thinking', text: ev.text });
      }
      return { ...a, content };
    };
  }
  if (ev.kind === 'assistant_text_chunk' && ev.text) {
    return (a) => {
      const content = [...a.content];
      const last = content[content.length - 1];
      if (last && last.t === 'p') {
        content[content.length - 1] = { ...last, text: last.text + ev.text };
      } else {
        content.push({ t: 'p', text: ev.text });
      }
      return { ...a, content };
    };
  }
  if (ev.kind === 'assistant_text' && ev.text) {
    return (a) => {
      const last = a.content[a.content.length - 1];
      if (last && last.t === 'p' && last.text) return a;
      return { ...a, content: [...a.content, { t: 'p', text: ev.text }] };
    };
  }
  if (ev.kind === 'tool_call_start') {
    return (a) => ({
      ...a,
      content: [...a.content, { t: 'tool', tool: ev.tool, args: ev.args, status: 'pending' }],
    });
  }
  if (ev.kind === 'tool_call_end') {
    return (a) => {
      const content = [...a.content];
      for (let i = content.length - 1; i >= 0; i--) {
        const b = content[i];
        if (b.t === 'tool' && b.tool === ev.tool && b.status === 'pending') {
          content[i] = { ...b, status: ev.status, result: ev.result };
          break;
        }
      }
      return { ...a, content };
    };
  }
  if (ev.kind === 'run_started') {
    // Find the pending run_workflow block this run belongs to and stash the
    // run_id so the chat card can become clickable before tool_call_end.
    return (a) => {
      const content = [...a.content];
      for (let i = content.length - 1; i >= 0; i--) {
        const b = content[i];
        if (b.t === 'tool' && b.tool === 'run_workflow' && b.status === 'pending') {
          content[i] = { ...b, runId: ev.run_id };
          break;
        }
      }
      return { ...a, content };
    };
  }
  if (ev.kind === 'assistant_cost') {
    return (a) => ({ ...a, cost: (a.cost ?? 0) + ev.cost });
  }
  if (ev.kind === 'context_compacted') {
    return (a) => ({ ...a, content: [...a.content, { t: 'notice', text: 'context compacted' }] });
  }
  if (ev.kind === 'error') {
    return (a) => ({
      ...a,
      content: [...a.content, { t: 'p', text: `*[error]* ${ev.message}` }],
    });
  }
  if (ev.kind === 'done') {
    return (a) => ({ ...a, streaming: false });
  }
  return null;
}

interface UseOrchestratorStreamArgs {
  setChatByWorkflow: React.Dispatch<React.SetStateAction<Record<string, ChatMessage[]>>>;
  setOrchestratingIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  refreshDetail: (wid?: string) => Promise<void>;
  refreshWorkflows: () => Promise<unknown>;
  /** Ref to the run-attach handler. Held as a ref so the handler can change
   * without invalidating in-flight streams. */
  attachToRunRef: React.MutableRefObject<
    (runId: string, workflowId: string) => void
  >;
}

/** Hook that owns per-workflow orchestrator-stream lifecycle: abort
 * controllers, optimistic bubble append, event → reducer dispatch, and
 * cleanup on unmount. */
export function useOrchestratorStream({
  setChatByWorkflow,
  setOrchestratingIds,
  refreshDetail,
  refreshWorkflows,
  attachToRunRef,
}: UseOrchestratorStreamArgs) {
  const abortRefs = useRef<Record<string, AbortController>>({});

  useEffect(() => () => {
    for (const ctrl of Object.values(abortRefs.current)) ctrl.abort();
    abortRefs.current = {};
  }, []);

  const updateAssistant = (wid: string, mut: AssistantMutation) => {
    setChatByWorkflow((prev) => {
      const cur = prev[wid] ?? [];
      if (cur.length === 0) return prev;
      const last = cur[cur.length - 1];
      if (last.role !== 'assistant') return prev;
      const next: ChatMessage[] = [...cur.slice(0, -1), mut(last)];
      return { ...prev, [wid]: next };
    });
  };

  const streamToOrchestrator = async (wid: string, sid: string, text: string) => {
    abortRefs.current[wid]?.abort();
    const ctrl = new AbortController();
    abortRefs.current[wid] = ctrl;

    setOrchestratingIds((prev) => {
      const s = new Set(prev);
      s.add(wid);
      return s;
    });

    // Optimistically add the user bubble + a streaming assistant placeholder.
    const placeholder: AssistantMessage = { role: 'assistant', content: [], streaming: true };
    setChatByWorkflow((prev) => ({
      ...prev,
      [wid]: [...(prev[wid] ?? []), { role: 'user', text }, placeholder],
    }));

    const handleEvent = (ev: OrchestratorEvent) => {
      const mut = reduceAssistantOnEvent(ev);
      if (mut) updateAssistant(wid, mut);
      if (ev.kind === 'tool_call_end' && ev.status === 'ok' && GRAPH_MUTATING_TOOLS.has(ev.tool)) {
        refreshDetail(wid);
      } else if (ev.kind === 'tool_call_end' && ev.status === 'ok' && WORKFLOW_METADATA_TOOLS.has(ev.tool)) {
        void refreshWorkflows();
      } else if (ev.kind === 'run_started') {
        // Orchestrator kicked off a run via `run_workflow`. Attach the run
        // panel via the same code path the Run button uses, so the user
        // gets live progress while the orchestrator turn awaits the result.
        attachToRunRef.current(ev.run_id, ev.workflow_id);
      }
    };

    try {
      await api.streamUserMessage(sid, text, handleEvent, ctrl.signal);
    } catch (e) {
      if (ctrl.signal.aborted) {
        updateAssistant(wid, (a) => ({ ...a, streaming: false }));
      } else {
        const msg = e instanceof Error ? e.message : String(e);
        updateAssistant(wid, (a) => ({
          ...a,
          streaming: false,
          content: [...a.content, { t: 'p', text: `*[stream failed]* ${msg}` }],
        }));
      }
    } finally {
      refreshDetail(wid);
      setOrchestratingIds((prev) => {
        const s = new Set(prev);
        s.delete(wid);
        return s;
      });
      if (abortRefs.current[wid] === ctrl) delete abortRefs.current[wid];
    }
  };

  const abortStream = (wid: string) => {
    abortRefs.current[wid]?.abort();
  };

  const dropWorkflow = (wid: string) => {
    abortRefs.current[wid]?.abort();
    delete abortRefs.current[wid];
  };

  return { streamToOrchestrator, abortStream, dropWorkflow };
}
