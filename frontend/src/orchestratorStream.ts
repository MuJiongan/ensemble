import { useEffect, useRef } from 'react';
import { api } from './api';
import { GRAPH_MUTATING_TOOLS, WORKFLOW_METADATA_TOOLS } from './appHelpers';
import { updateLastAssistant } from './chatBlocks';
import {
  mapAssistantWireEvent,
  reduceAssistantWireEvent,
  useAssistantStreamBuffer,
  type AssistantMutation,
} from './assistantStream';
import type { AssistantMessage, ChatMessage } from './components/ChatPanel';
import type { OrchestratorEvent, RunEvent } from './types';

export const mapOrchestratorEvent = mapAssistantWireEvent;

/** Back-compat export for reducer unit tests and callers that do not need batching. */
export function reduceAssistantOnEvent(ev: OrchestratorEvent): AssistantMutation | null {
  return reduceAssistantWireEvent(ev);
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
  onOrchestratorRunStarted?: (workflowId: string, runId: string) => void;
  onRunAgentStarted?: (workflowId: string) => void;
  /** Surface node-runtime events that need app-level handling (for example,
   * an inline agent reporting an MCP server that needs authorization). */
  onRunAgentEvent?: (workflowId: string, event: RunEvent) => void;
  onRunAgentFinished?: (workflowId: string, result: unknown) => void;
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
  onOrchestratorRunStarted,
  onRunAgentStarted,
  onRunAgentEvent,
  onRunAgentFinished,
}: UseOrchestratorStreamArgs) {
  const abortRefs = useRef<Record<string, AbortController>>({});

  useEffect(() => () => {
    for (const ctrl of Object.values(abortRefs.current)) ctrl.abort();
    abortRefs.current = {};
  }, []);

  const updateAssistant = (wid: string, mut: AssistantMutation) => {
    setChatByWorkflow((prev) => {
      const cur = prev[wid] ?? [];
      const next = updateLastAssistant(cur, mut);
      return next === cur ? prev : { ...prev, [wid]: next };
    });
  };
  const assistantStream = useAssistantStreamBuffer<string>(updateAssistant);

  const handleOrchestratorEvent = (wid: string, ev: OrchestratorEvent) => {
    for (const streamEvent of mapAssistantWireEvent(ev)) {
      assistantStream.apply(wid, streamEvent);
    }
    if (ev.kind === 'tool_call_start' && ev.tool === 'run_agent') {
      onRunAgentStarted?.(wid);
    }
    if (ev.kind === 'tool_call_end' && ev.tool === 'run_agent') {
      onRunAgentFinished?.(wid, ev.result);
    }
    if (ev.kind === 'tool_call_end' && ev.status === 'ok' && GRAPH_MUTATING_TOOLS.has(ev.tool)) {
      refreshDetail(wid);
    } else if (ev.kind === 'tool_call_end' && ev.status === 'ok' && WORKFLOW_METADATA_TOOLS.has(ev.tool)) {
      void refreshWorkflows();
    } else if (ev.kind === 'run_started') {
      // Orchestrator kicked off a run via `run_workflow`. Attach the run
      // panel via the same code path the Run button uses, so the user
      // gets live progress while the run continues in the background.
      onOrchestratorRunStarted?.(ev.workflow_id, ev.run_id);
      attachToRunRef.current(ev.run_id, ev.workflow_id);
    } else if (ev.kind === 'run_agent_event') {
      onRunAgentEvent?.(wid, ev.event);
    }
  };

  const finishStream = (wid: string, ctrl: AbortController) => {
    assistantStream.flush(wid);
    refreshDetail(wid);
    if (abortRefs.current[wid] !== ctrl) return;
    setOrchestratingIds((prev) => {
      const s = new Set(prev);
      s.delete(wid);
      return s;
    });
    delete abortRefs.current[wid];
  };

  const streamToOrchestrator = async (
    wid: string,
    sid: string,
    text: string,
    attachments?: { dataUrl: string; filename: string; mime: string }[],
    opts?: { auto?: boolean; notice?: string },
  ) => {
    abortRefs.current[wid]?.abort();
    const ctrl = new AbortController();
    abortRefs.current[wid] = ctrl;

    setOrchestratingIds((prev) => {
      const s = new Set(prev);
      s.add(wid);
      return s;
    });

    // Optimistically add the user bubble + a streaming assistant placeholder.
    // Automatic app-generated turns are not rendered as "you"; they show as
    // a small notice inside the assistant stream instead.
    const placeholder: AssistantMessage = {
      role: 'assistant',
      content: opts?.auto && opts.notice ? [{ t: 'notice', text: opts.notice, kind: 'run' }] : [],
      streaming: true,
    };
    const images = (attachments ?? [])
      .filter((a) => a.mime.startsWith('image/'))
      .map((a) => a.dataUrl);
    const files = (attachments ?? [])
      .filter((a) => !a.mime.startsWith('image/'))
      .map((a) => ({
        name: a.filename,
        kind: a.mime === 'application/pdf' ? 'pdf' : 'txt',
      }));
    setChatByWorkflow((prev) => ({
      ...prev,
      [wid]: [
        ...(prev[wid] ?? []),
        ...(opts?.auto
          ? []
          : [{
              role: 'user' as const,
              text,
              ...(images.length ? { images } : {}),
              ...(files.length ? { files } : {}),
            }]),
        placeholder,
      ],
    }));

    try {
      await api.streamUserMessage(wid, sid, text, (ev) => handleOrchestratorEvent(wid, ev), ctrl.signal, attachments, {
        auto: opts?.auto,
      });
    } catch (e) {
      assistantStream.flush(wid);
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
      assistantStream.flush(wid);
      finishStream(wid, ctrl);
    }
  };

  const resumeOrchestratorStream = async (wid: string, sid: string, turnId: string) => {
    abortRefs.current[wid]?.abort();
    const ctrl = new AbortController();
    abortRefs.current[wid] = ctrl;

    setOrchestratingIds((prev) => {
      const s = new Set(prev);
      s.add(wid);
      return s;
    });

    try {
      await api.streamOrchestratorTurn(
        wid,
        sid,
        turnId,
        (ev) => handleOrchestratorEvent(wid, ev),
        ctrl.signal,
      );
    } catch (e) {
      assistantStream.flush(wid);
      if (ctrl.signal.aborted) {
        return;
      }
      const msg = e instanceof Error ? e.message : String(e);
      updateAssistant(wid, (a) => ({
        ...a,
        streaming: false,
        content: [...a.content, { t: 'p', text: `*[stream failed]* ${msg}` }],
      }));
    } finally {
      assistantStream.flush(wid);
      finishStream(wid, ctrl);
    }
  };

  const abortStream = (wid: string) => {
    assistantStream.flush(wid);
    abortRefs.current[wid]?.abort();
  };

  const dropWorkflow = (wid: string) => {
    assistantStream.flush(wid);
    abortRefs.current[wid]?.abort();
    delete abortRefs.current[wid];
    assistantStream.clear(wid);
  };

  return { streamToOrchestrator, resumeOrchestratorStream, abortStream, dropWorkflow };
}
