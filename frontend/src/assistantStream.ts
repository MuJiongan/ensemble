import { useEffect, useRef } from 'react';
import {
  foldContentDelta, foldReasoningDelta, appendToolCall, resolveToolCall,
  appendParagraph, appendNotice,
} from './chatBlocks';
import type {
  AssistantMessage, ChatNotice, ChatToolCall, ChatToolStatus,
} from './components/ChatPanel';
import type { CallChatTurnEvent, OrchestratorEvent } from './types';

export type AssistantMutation = (a: AssistantMessage) => AssistantMessage;

export type AssistantStreamEvent =
  | { type: 'content_delta'; text: string }
  | { type: 'reasoning_delta'; text: string }
  | { type: 'text'; text: string }
  | { type: 'paragraph'; text: string }
  | {
      type: 'tool_start';
      tool: string;
      args: string;
      argsFull?: Record<string, unknown> | null;
    }
  | {
      type: 'tool_end';
      tool: string;
      status: ChatToolStatus;
      result?: unknown;
    }
  | { type: 'tool_patch'; tool: string; patch: Partial<ChatToolCall> }
  | { type: 'cost'; cost: number }
  | { type: 'context_compacted'; summarized?: number }
  | { type: 'notice'; text: string; kind?: ChatNotice['kind'] }
  | { type: 'error'; message: string }
  | { type: 'done' };

type DeltaEvent = Extract<AssistantStreamEvent, { type: 'content_delta' | 'reasoning_delta' }>;

export function reduceAssistantStreamEvent(ev: AssistantStreamEvent): AssistantMutation | null {
  if (ev.type === 'content_delta' && ev.text) {
    return (a) => foldContentDelta(a, ev.text);
  }
  if (ev.type === 'reasoning_delta' && ev.text) {
    return (a) => foldReasoningDelta(a, ev.text);
  }
  if (ev.type === 'text' && ev.text) {
    return (a) => {
      const last = a.content[a.content.length - 1];
      if (last && last.t === 'p' && last.text) return a;
      return appendParagraph(a, ev.text);
    };
  }
  if (ev.type === 'paragraph') {
    return (a) => appendParagraph(a, ev.text);
  }
  if (ev.type === 'tool_start') {
    return (a) => appendToolCall(a, {
      tool: ev.tool,
      args: ev.args,
      argsFull: ev.argsFull,
    });
  }
  if (ev.type === 'tool_end') {
    return (a) => resolveToolCall(a, {
      tool: ev.tool,
      status: ev.status,
      result: ev.result,
    });
  }
  if (ev.type === 'tool_patch') {
    return (a) => {
      const content = [...a.content];
      for (let i = content.length - 1; i >= 0; i--) {
        const b = content[i];
        if (b.t === 'tool' && b.tool === ev.tool && b.status === 'pending') {
          content[i] = { ...b, ...ev.patch };
          break;
        }
      }
      return { ...a, content };
    };
  }
  if (ev.type === 'cost') {
    return (a) => ({ ...a, cost: (a.cost ?? 0) + ev.cost });
  }
  if (ev.type === 'context_compacted') {
    return (a) => appendNotice(a, 'context compacted', 'compaction');
  }
  if (ev.type === 'notice') {
    return (a) => appendNotice(a, ev.text, ev.kind);
  }
  if (ev.type === 'error') {
    return (a) => appendParagraph(a, `*[error]* ${ev.message}`);
  }
  if (ev.type === 'done') {
    return (a) => ({ ...a, streaming: false });
  }
  return null;
}

type AssistantWireEvent = OrchestratorEvent | CallChatTurnEvent;

function stringifyArgs(args: Record<string, unknown>): string {
  try {
    return JSON.stringify(args);
  } catch {
    return '';
  }
}

function mapOrchestratorWireEvent(ev: OrchestratorEvent): AssistantStreamEvent[] {
  if (ev.kind === 'assistant_thinking_chunk' && ev.text) {
    return [{ type: 'reasoning_delta', text: ev.text }];
  }
  if (ev.kind === 'assistant_text_chunk' && ev.text) {
    return [{ type: 'content_delta', text: ev.text }];
  }
  if (ev.kind === 'assistant_text' && ev.text) {
    return [{ type: 'text', text: ev.text }];
  }
  if (ev.kind === 'tool_call_start') {
    return [{
      type: 'tool_start',
      tool: ev.tool,
      args: ev.args,
      argsFull: ev.args_full,
    }];
  }
  if (ev.kind === 'tool_call_end') {
    return [{
      type: 'tool_end',
      tool: ev.tool,
      status: ev.status,
      result: ev.result,
    }];
  }
  if (ev.kind === 'run_agent_event') {
    // App owns and batches the inline node trace. Keeping token events out of
    // the chat block avoids a second event copy and a render per token.
    return [];
  }
  if (ev.kind === 'run_started') {
    return [{ type: 'tool_patch', tool: 'run_workflow', patch: { runId: ev.run_id } }];
  }
  if (ev.kind === 'assistant_cost') {
    return [{ type: 'cost', cost: ev.cost }];
  }
  if (ev.kind === 'context_compacted') {
    return [{ type: 'context_compacted', summarized: ev.summarized }];
  }
  if (ev.kind === 'rate_limit_retry') {
    return [{
      type: 'notice',
      text: `provider overloaded — retrying in ${ev.delay_seconds}s (${ev.attempt}/${ev.max_retries})`,
    }];
  }
  if (ev.kind === 'error') {
    return [{ type: 'error', message: ev.message }];
  }
  if (ev.kind === 'done') {
    return [{ type: 'done' }];
  }
  return [];
}

function mapCallChatWireEvent(ev: CallChatTurnEvent): AssistantStreamEvent[] {
  if (ev.type === 'llm_call_chunk' && ev.delta) {
    if (ev.kind === 'reasoning') return [{ type: 'reasoning_delta', text: ev.delta }];
    if (ev.kind === 'content') return [{ type: 'content_delta', text: ev.delta }];
    return [];
  }
  if (ev.type === 'tool_call_started') {
    return [{
      type: 'tool_start',
      tool: ev.tool,
      args: stringifyArgs(ev.args),
      argsFull: ev.args,
    }];
  }
  if (ev.type === 'tool_call_finished') {
    return [{
      type: 'tool_end',
      tool: ev.tool,
      status: ev.error ? 'err' : 'ok',
      result: ev.error ?? ev.result,
    }];
  }
  if (ev.type === 'llm_call_finished' && ev.cost) {
    return [{ type: 'cost', cost: ev.cost }];
  }
  if (ev.type === 'context_compacted') {
    return [{ type: 'context_compacted', summarized: ev.summarized }];
  }
  if (ev.type === 'rate_limit_retry') {
    return [{
      type: 'notice',
      text: `provider overloaded — retrying in ${ev.delay_seconds}s (${ev.attempt}/${ev.max_retries})`,
    }];
  }
  if (ev.type === 'error') {
    return [{ type: 'error', message: ev.error }];
  }
  if (ev.type === 'run_finished') {
    const out: AssistantStreamEvent[] = [{ type: 'done' }];
    if (ev.status === 'cancelled') out.push({ type: 'notice', text: 'turn cancelled' });
    if (ev.status === 'error') {
      out.push({ type: 'paragraph', text: `*[turn failed]* ${ev.error ?? 'unknown error'}` });
    }
    return out;
  }
  if (ev.type === 'run_deleted') {
    return [
      { type: 'done' },
      { type: 'notice', text: 'turn stream ended' },
    ];
  }
  return [];
}

export function mapAssistantWireEvent(ev: AssistantWireEvent): AssistantStreamEvent[] {
  return 'type' in ev ? mapCallChatWireEvent(ev) : mapOrchestratorWireEvent(ev);
}

export function reduceAssistantWireEvent(ev: AssistantWireEvent): AssistantMutation | null {
  const events = mapAssistantWireEvent(ev);
  if (events.length === 0) return null;
  return (a) => events.reduce((cur, event) => {
    const mut = reduceAssistantStreamEvent(event);
    return mut ? mut(cur) : cur;
  }, a);
}

export function isAssistantWireTerminal(ev: AssistantWireEvent): boolean {
  if ('type' in ev) return ev.type === 'run_finished' || ev.type === 'run_deleted';
  return ev.kind === 'done';
}

export function useAssistantStreamBuffer<Key extends string>(
  updateAssistant: (key: Key, mut: AssistantMutation) => void,
  flushDelayMs = 33,
) {
  const deltaBuffers = useRef<Record<string, DeltaEvent[]>>({});
  const flushTimers = useRef<Record<string, number>>({});

  const flush = (key: Key) => {
    const k = String(key);
    const timer = flushTimers.current[k];
    if (timer !== undefined) {
      window.clearTimeout(timer);
      delete flushTimers.current[k];
    }
    const pending = deltaBuffers.current[k];
    if (!pending || pending.length === 0) return;
    delete deltaBuffers.current[k];

    updateAssistant(key, (a) => {
      let next = a;
      for (const ev of pending) {
        next = ev.type === 'content_delta'
          ? foldContentDelta(next, ev.text)
          : foldReasoningDelta(next, ev.text);
      }
      return next;
    });
  };

  const scheduleFlush = (key: Key) => {
    const k = String(key);
    if (flushTimers.current[k] !== undefined) return;
    flushTimers.current[k] = window.setTimeout(() => flush(key), flushDelayMs);
  };

  const apply = (key: Key, ev: AssistantStreamEvent) => {
    if ((ev.type === 'content_delta' || ev.type === 'reasoning_delta') && ev.text) {
      const k = String(key);
      deltaBuffers.current[k] = [...(deltaBuffers.current[k] ?? []), ev];
      scheduleFlush(key);
      return;
    }
    flush(key);
    const mut = reduceAssistantStreamEvent(ev);
    if (mut) updateAssistant(key, mut);
  };

  const clear = (key: Key) => {
    const k = String(key);
    const timer = flushTimers.current[k];
    if (timer !== undefined) {
      window.clearTimeout(timer);
      delete flushTimers.current[k];
    }
    delete deltaBuffers.current[k];
  };

  useEffect(() => () => {
    for (const timer of Object.values(flushTimers.current)) window.clearTimeout(timer);
    flushTimers.current = {};
    deltaBuffers.current = {};
  }, []);

  return { apply, flush, clear };
}
