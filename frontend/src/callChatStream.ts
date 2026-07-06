import { useEffect, useRef } from 'react';
import { api } from './api';
import { updateLastAssistant } from './chatBlocks';
import {
  isAssistantWireTerminal,
  mapAssistantWireEvent,
  reduceAssistantWireEvent,
  useAssistantStreamBuffer,
  type AssistantMutation,
} from './assistantStream';
import type { AssistantMessage, ChatMessage } from './components/ChatPanel';
import type { CallChatTurnEvent, ModelSelection } from './types';

export const mapCallChatEvent = mapAssistantWireEvent;

/** Back-compat export for reducer unit tests and callers that do not need batching. */
export function reduceCallChatOnEvent(ev: CallChatTurnEvent): AssistantMutation | null {
  return reduceAssistantWireEvent(ev);
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
  const assistantStream = useAssistantStreamBuffer<string>(updateAssistant);

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
    assistantStream.flush(chatId);
    if (ws) {
      try { ws.close(); } catch { /* noop */ }
      delete wsByChat.current[chatId];
    }
    delete turnByChat.current[chatId];
    setStreaming(chatId, false);
  };

  const streamToCallChat = async (
    runId: string,
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
      const res = await api.sendCallChatTurn(runId, nodeRunId, callId, text, sel);
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
      for (const streamEvent of mapAssistantWireEvent(ev)) {
        assistantStream.apply(chatId, streamEvent);
      }
      if (isAssistantWireTerminal(ev)) {
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
      assistantStream.flush(chatId);
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
    assistantStream.flush(chatId);
    setStreaming(chatId, false);
  };

  /** Tear down every in-flight turn socket and clear streaming state. Used when
   * the continuation set is reset (e.g. switching workflows) so sockets from a
   * previous workflow can't keep mutating now-stale `callChatMessages`. */
  const dropAllStreams = () => {
    const chatIds = new Set([
      ...Object.keys(wsByChat.current),
      ...Object.keys(turnByChat.current),
    ]);
    for (const ws of Object.values(wsByChat.current)) {
      try { ws.close(); } catch { /* noop */ }
    }
    for (const chatId of chatIds) assistantStream.clear(chatId);
    wsByChat.current = {};
    turnByChat.current = {};
    setStreamingChatIds(new Set());
  };

  return { streamToCallChat, cancelCallChat, dropAllStreams };
}
