/**
 * Shared assistant-bubble folding primitives.
 *
 * Both the orchestrator stream (`orchestratorStream.ts`) and the continue-chat
 * continuation stream (`callChatStream.ts`) fold a stream of events into an
 * `AssistantMessage`'s content blocks. The event *shapes* differ, but the block
 * mutations are identical — so they live here, and each reducer just maps its
 * own events onto these. Keeps a rendering fix in one place instead of two.
 */
import type {
  AssistantMessage, ChatMessage, ChatToolStatus,
} from './components/ChatPanel';

type A = AssistantMessage;

/** Append a content delta to the trailing paragraph, or start a new one. */
export function foldContentDelta(a: A, delta: string): A {
  const content = [...a.content];
  const last = content[content.length - 1];
  if (last && last.t === 'p') {
    content[content.length - 1] = { ...last, text: last.text + delta };
  } else {
    content.push({ t: 'p', text: delta });
  }
  return { ...a, content };
}

/** Append a reasoning delta to the trailing thinking block, or start a new one. */
export function foldReasoningDelta(a: A, delta: string): A {
  const content = [...a.content];
  const last = content[content.length - 1];
  if (last && last.t === 'thinking') {
    content[content.length - 1] = { ...last, text: last.text + delta };
  } else {
    content.push({ t: 'thinking', text: delta });
  }
  return { ...a, content };
}

/** Push a pending tool-call card. */
export function appendToolCall(
  a: A,
  tc: { tool: string; args: string; argsFull?: Record<string, unknown> | null },
): A {
  return {
    ...a,
    content: [
      ...a.content,
      { t: 'tool', tool: tc.tool, args: tc.args, argsFull: tc.argsFull ?? null, status: 'pending' },
    ],
  };
}

/** Resolve the most recent still-pending tool card matching `tool` by name. */
export function resolveToolCall(
  a: A,
  r: { tool: string; status: ChatToolStatus; result?: unknown },
): A {
  const content = [...a.content];
  for (let i = content.length - 1; i >= 0; i--) {
    const b = content[i];
    if (b.t === 'tool' && b.tool === r.tool && b.status === 'pending') {
      content[i] = { ...b, status: r.status, result: r.result };
      break;
    }
  }
  return { ...a, content };
}

/** Append a plain paragraph (e.g. an inline error line). */
export function appendParagraph(a: A, text: string): A {
  return { ...a, content: [...a.content, { t: 'p', text }] };
}

/** Append an inline notice divider. */
export function appendNotice(a: A, text: string): A {
  return { ...a, content: [...a.content, { t: 'notice', text }] };
}

/**
 * Rewrite the trailing assistant bubble of `list` via `mut`. Returns `list`
 * unchanged (same reference) when the last message isn't an assistant bubble,
 * so callers can skip a state update.
 */
export function updateLastAssistant(list: ChatMessage[], mut: (a: A) => A): ChatMessage[] {
  if (list.length === 0) return list;
  const last = list[list.length - 1];
  if (last.role !== 'assistant') return list;
  return [...list.slice(0, -1), mut(last)];
}
