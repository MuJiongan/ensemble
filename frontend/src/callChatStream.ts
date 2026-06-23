import { useEffect, useRef } from 'react';
import { api } from './api';
import {
  foldContentDelta, foldReasoningDelta, appendToolCall, resolveToolCall,
  appendParagraph, appendNotice, updateLastAssistant,
} from './chatBlocks';
import type { AssistantMessage, ChatMessage } from './components/ChatPanel';
import type { CallChatTurnEvent, ModelSelection } from './types';

type AssistantMutation = (a: AssistantMessage) => AssistantMessage;

/** Map a continue-chat turn event to an assistant-bubble mutation, or `null`
 * when the event has no bubble effect. A turn streams the same per-call event
 * contract a run does, so this mirrors `reduceAssistantOnEvent` — folding
 * content/reasoning chunks and tool calls into the streaming assistant bubble.
 * Pure, for unit-testability. */
export function reduceCallChatOnEvent(ev: CallChatTurnEvent): AssistantMutation | null {
  if (ev.type === 'llm_call_chunk' && ev.delta) {
    if (ev.kind === 'reasoning') return (a) => foldReasoningDelta(a, ev.delta);
    if (ev.kind === 'content') return (a) => foldContentDelta(a, ev.delta);
    // tool_args deltas are ignored — the full args arrive on tool_call_started.
    return null;
  }
  if (ev.type === 'tool_call_started') {
    let argsStr = '';
    try {
      argsStr = JSON.stringify(ev.args);
    } catch {
      /* leave '' — the argsFull dict still carries the raw input */
    }
    return (a) => appendToolCall(a, { tool: ev.tool, args: argsStr, argsFull: ev.args });
  }
  if (ev.type === 'tool_call_finished') {
    return (a) =>
      resolveToolCall(a, {
        tool: ev.tool,
        status: ev.error ? 'err' : 'ok',
        result: ev.error ?? ev.result,
      });
  }
  if (ev.type === 'llm_call_finished' && ev.cost) {
    // Accumulate provider-reported cost across rounds onto the streaming
    // bubble, matching the orchestrator chat's per-turn cost display.
    return (a) => ({ ...a, cost: (a.cost ?? 0) + ev.cost });
  }
  if (ev.type === 'context_compacted') {
    return (a) => appendNotice(a, 'context compacted');
  }
  if (ev.type === 'error') {
    return (a) => appendParagraph(a, `*[error]* ${ev.error}`);
  }
  if (ev.type === 'run_finished') {
    return (a) => {
      const done: AssistantMessage = { ...a, streaming: false };
      if (ev.status === 'cancelled') return appendNotice(done, 'turn cancelled');
      if (ev.status === 'error') {
        return appendParagraph(done, `*[turn failed]* ${ev.error ?? 'unknown error'}`);
      }
      return done;
    };
  }
  return null;
}

interface UseCallChatStreamArgs {
  setCallChatMessages: React.Dispatch<React.SetStateAction<Record<string, ChatMessage[]>>>;
  setStreamingChatIds: React.Dispatch<React.SetStateAction<Set<string>>>;
}

/** Owns per-continuation chat-turn lifecycle: optimistic bubble append, the turn WS,
 * event → reducer dispatch, and syncing the canonical transcript on success.
 * One turn at a time per continuation (a new turn aborts any in-flight WS). */
export function useCallChatStream({
  setCallChatMessages,
  setStreamingChatIds,
}: UseCallChatStreamArgs) {
  const wsByChat = useRef<Record<string, WebSocket>>({});
  const turnByChat = useRef<Record<string, string>>({});

  useEffect(() => () => {
    for (const ws of Object.values(wsByChat.current)) {
      try { ws.close(); } catch { /* noop */ }
    }
    wsByChat.current = {};
  }, []);

  const updateAssistant = (chatId: string, mut: AssistantMutation) => {
    setCallChatMessages((prev) => {
      const cur = prev[chatId] ?? [];
      const next = updateLastAssistant(cur, mut);
      return next === cur ? prev : { ...prev, [chatId]: next };
    });
  };

  const setStreaming = (chatId: string, on: boolean) => {
    setStreamingChatIds((prev) => {
      const s = new Set(prev);
      if (on) s.add(chatId);
      else s.delete(chatId);
      return s;
    });
  };

  const teardown = (chatId: string) => {
    const ws = wsByChat.current[chatId];
    if (ws) {
      try { ws.close(); } catch { /* noop */ }
      delete wsByChat.current[chatId];
    }
    delete turnByChat.current[chatId];
    setStreaming(chatId, false);
  };

  const streamToCallChat = async (
    nodeRunId: string,
    callId: string,
    text: string,
    sel: ModelSelection | null,
  ) => {
    // A continuation is addressed by the call it continues; the local state key
    // mirrors that (and is stable before any row is persisted).
    const chatId = `${nodeRunId}:${callId}`;
    // A new turn supersedes any in-flight one for this continuation.
    teardown(chatId);
    setStreaming(chatId, true);

    // Optimistically add the user bubble + a streaming assistant placeholder.
    const placeholder: AssistantMessage = { role: 'assistant', content: [], streaming: true };
    setCallChatMessages((prev) => ({
      ...prev,
      [chatId]: [...(prev[chatId] ?? []), { role: 'user', text }, placeholder],
    }));

    let turnId: string;
    try {
      const res = await api.sendCallChatTurn(nodeRunId, callId, text, sel);
      turnId = res.turn_id;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      updateAssistant(chatId, (a) => ({
        ...a,
        streaming: false,
        content: [...a.content, { t: 'p', text: `*[failed to start turn]* ${msg}` }],
      }));
      setStreaming(chatId, false);
      return;
    }
    turnByChat.current[chatId] = turnId;

    const ws = new WebSocket(api.callChatEventsUrl(turnId));
    wsByChat.current[chatId] = ws;
    let finished = false;

    ws.onmessage = (e) => {
      let ev: CallChatTurnEvent;
      try {
        ev = JSON.parse(e.data) as CallChatTurnEvent;
      } catch {
        return;
      }
      const mut = reduceCallChatOnEvent(ev);
      if (mut) updateAssistant(chatId, mut);
      if (ev.type === 'run_finished' || ev.type === 'run_deleted') {
        // The streamed bubble already holds the full response — content and
        // tool calls arrived as events, and the run_finished reducer cleared
        // the streaming flag. Don't refetch the persisted transcript here: the
        // backend commits it *after* broadcasting run_finished, so a refetch
        // races the write and would briefly blank the just-finished response.
        // The persisted form is loaded on reopen/reload instead.
        finished = true;
        teardown(chatId);
      }
    };

    ws.onclose = () => {
      if (finished) return;
      // Socket dropped before a terminal event — stop the spinner.
      updateAssistant(chatId, (a) => ({ ...a, streaming: false }));
      teardown(chatId);
    };
    ws.onerror = () => {
      // onclose fires after onerror; let it handle teardown.
    };
  };

  /** Cancel the in-flight turn for a continuation (SIGTERMs its subprocess). */
  const cancelCallChat = (chatId: string) => {
    const turnId = turnByChat.current[chatId];
    if (!turnId) return;
    void api.cancelCallChatTurn(turnId).catch(() => { /* noop */ });
    // Optimistically ungate the composer and stop the bubble spinner now,
    // rather than waiting on the terminal WS event — a wedged child (escalated
    // to SIGKILL backend-side) or a dropped socket could otherwise leave the
    // spinner stuck forever. The later run_finished/run_deleted teardown is
    // idempotent, so this can't double-clear.
    updateAssistant(chatId, (a) => (a.streaming ? { ...a, streaming: false } : a));
    setStreaming(chatId, false);
  };

  /** Tear down every in-flight turn socket and clear streaming state. Used when
   * the continuation set is reset (e.g. switching workflows) so sockets from a
   * previous workflow can't keep mutating now-stale `callChatMessages`. */
  const dropAllStreams = () => {
    for (const ws of Object.values(wsByChat.current)) {
      try { ws.close(); } catch { /* noop */ }
    }
    wsByChat.current = {};
    turnByChat.current = {};
    setStreamingChatIds(new Set());
  };

  return { streamToCallChat, cancelCallChat, dropAllStreams };
}
