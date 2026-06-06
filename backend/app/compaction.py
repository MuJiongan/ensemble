"""Context compaction.

When a conversation's token count approaches the model's context window we
*compact* it: summarize the older turns into a structured anchor and keep the
most recent turns verbatim. A lighter-weight *prune* erases stale tool outputs
in place without paying for an LLM call.

This module is transport-agnostic. It operates on plain OpenAI-compatible chat
message dicts (``{"role", "content", "tool_calls", "tool_call_id", ...}``),
which is the shape both the orchestrator agent loop and the per-node runner
loop already use. The two integration points supply:

  * a ``summarize`` callback that runs the configured model over a message
    list and returns the assistant text, and
  * the model's :class:`~app.catalog.models_dev.ModelLimit` so we know the
    context window.

The orchestrator keeps the durable "anchor" by persisting a compaction marker
row (see ``app/orchestrator/agent/persistence.py``); the per-node runner just
mutates its in-memory message list.
"""
from __future__ import annotations

import json
import math
from typing import Callable, Optional

# Tunables for the compaction + prune heuristics.
COMPACTION_BUFFER = 20_000           # default output reserve kept free of input
PRUNE_MINIMUM = 20_000               # don't bother pruning unless it frees this
PRUNE_PROTECT = 40_000               # most-recent tool output to never prune
DEFAULT_TAIL_TURNS = 2               # how many recent turns to keep verbatim
MIN_PRESERVE_RECENT_TOKENS = 2_000
MAX_PRESERVE_RECENT_TOKENS = 8_000
TOOL_OUTPUT_PRUNED = "[tool output pruned to reclaim context]"
# Marks the system message that carries a compaction anchor forward, so a
# later compaction can find the prior summary and update it incrementally.
SUMMARY_PREFIX = "[compacted-context]"

SUMMARY_TEMPLATE = """Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Do not mention the summary process or that context was compacted."""


# ---------------------------------------------------------------------------
# token accounting
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Cheap heuristic: ~4 characters per token. Good enough for budgeting;
    real usage counts from the provider are preferred at the trigger sites."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def estimate_messages(messages: list[dict]) -> int:
    """Estimate the token footprint of a message list by serializing it and
    applying the per-character heuristic."""
    return estimate_tokens(json.dumps(messages, default=str))


def usable(
    *,
    context: int,
    output_limit: int = 0,
    input_limit: Optional[int] = None,
    reserved: Optional[int] = None,
    output_token_max: Optional[int] = None,
) -> int:
    """Tokens of input we can use before compaction must kick in.

    Mirrors ``overflow.ts``: reserve room for the model's output (capped at
    ``COMPACTION_BUFFER``), then subtract from the input window. Providers that
    publish a dedicated ``input`` limit use it directly; otherwise we derive it
    from ``context - output``.
    """
    if context == 0:
        return 0
    out_max = output_token_max if output_token_max is not None else output_limit
    if reserved is None:
        reserved = min(COMPACTION_BUFFER, out_max) if out_max else COMPACTION_BUFFER
    if input_limit:
        return max(0, input_limit - reserved)
    return max(0, context - out_max)


def is_overflow(
    *,
    token_count: int,
    context: int,
    output_limit: int = 0,
    input_limit: Optional[int] = None,
    reserved: Optional[int] = None,
    output_token_max: Optional[int] = None,
) -> bool:
    """True once the live token count reaches the usable budget. A model with
    no published context window (``context == 0``) never overflows — we can't
    reason about a limit we don't know."""
    if context == 0:
        return False
    budget = usable(
        context=context,
        output_limit=output_limit,
        input_limit=input_limit,
        reserved=reserved,
        output_token_max=output_token_max,
    )
    return token_count >= budget


def preserve_recent_budget(
    *, context: int, output_limit: int = 0, input_limit: Optional[int] = None
) -> int:
    """How many tokens of recent history to keep verbatim — 25% of the usable
    window, clamped to [MIN, MAX]."""
    window = usable(context=context, output_limit=output_limit, input_limit=input_limit)
    return min(MAX_PRESERVE_RECENT_TOKENS, max(MIN_PRESERVE_RECENT_TOKENS, window // 4))


# ---------------------------------------------------------------------------
# tail selection — keep the most recent turns verbatim
# ---------------------------------------------------------------------------


def _turn_starts(messages: list[dict]) -> list[int]:
    """Indices where a user turn begins. A "turn" is a user message plus every
    assistant/tool message up to the next user message. Splitting only at user
    boundaries guarantees a tail never starts
    with an orphaned ``tool`` result whose ``assistant`` (with ``tool_calls``)
    landed in the summarized head."""
    return [i for i, m in enumerate(messages) if m.get("role") == "user"]


def select_tail(
    messages: list[dict],
    *,
    context: int,
    output_limit: int = 0,
    input_limit: Optional[int] = None,
    tail_turns: int = DEFAULT_TAIL_TURNS,
    preserve_tokens: Optional[int] = None,
) -> int:
    """Return the index where the verbatim tail begins; everything before it is
    eligible for summarization. Keeps up to ``tail_turns`` recent user turns,
    bounded by a token budget so a single huge turn can't blow the window.

    Returns ``0`` (keep everything, nothing to summarize) when the history is
    too small or the budget swallows all turns.
    """
    if tail_turns <= 0:
        return 0
    starts = _turn_starts(messages)
    if not starts:
        return 0
    budget = (
        preserve_tokens
        if preserve_tokens is not None
        else preserve_recent_budget(context=context, output_limit=output_limit, input_limit=input_limit)
    )
    recent = starts[-tail_turns:]
    total = 0
    keep: Optional[int] = None
    for start in reversed(recent):
        size = estimate_messages(messages[start:])  # turn-and-after; cheap bound
        if total + size <= budget:
            total += size
            keep = start
            continue
        break
    if keep is None or keep == 0:
        return 0
    return keep


# ---------------------------------------------------------------------------
# compaction — summarize the head, keep the tail
# ---------------------------------------------------------------------------


def build_prompt(*, previous_summary: Optional[str] = None, context: Optional[list[str]] = None) -> str:
    """The compaction instruction appended after the history being summarized.
    When a prior summary exists we ask the model to *update* the anchor rather
    than start from scratch (incremental-anchor behaviour)."""
    if previous_summary:
        anchor = "\n".join(
            [
                "Update the anchored summary below using the conversation history above.",
                "Preserve still-true details, remove stale details, and merge in the new facts.",
                "<previous-summary>",
                previous_summary,
                "</previous-summary>",
            ]
        )
    else:
        anchor = "Create a new anchored summary from the conversation history above."
    return "\n\n".join([anchor, SUMMARY_TEMPLATE, *(context or [])])


def split_for_compaction(
    messages: list[dict],
    *,
    context: int,
    output_limit: int = 0,
    input_limit: Optional[int] = None,
    tail_turns: int = DEFAULT_TAIL_TURNS,
    preserve_tokens: Optional[int] = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Partition ``messages`` into ``(leading_system, head, tail)``.

    Leading ``system`` messages (the system prompt, graph snapshot, …) are kept
    out front untouched. ``head`` is summarized; ``tail`` is replayed verbatim.
    A ``head`` smaller than two messages isn't worth a summarization round, so
    we return an empty head in that case.
    """
    lead = 0
    while lead < len(messages) and messages[lead].get("role") == "system":
        lead += 1
    leading_system = messages[:lead]
    rest = messages[lead:]

    tail_start = select_tail(
        rest,
        context=context,
        output_limit=output_limit,
        input_limit=input_limit,
        tail_turns=tail_turns,
        preserve_tokens=preserve_tokens,
    )
    head = rest[:tail_start]
    tail = rest[tail_start:]
    if len(head) < 2:
        return leading_system, [], rest
    return leading_system, head, tail


def summary_message(summary: str) -> dict:
    """The replacement message that carries the anchored summary forward. A
    ``system`` role keeps it clearly out-of-band from the user/assistant turns
    it stands in for; the ``SUMMARY_PREFIX`` marker lets a later compaction
    find and update this anchor."""
    return {
        "role": "system",
        "content": (
            f"{SUMMARY_PREFIX} The earlier conversation was compacted to fit the "
            "context window. Anchored summary of everything before the messages "
            "that follow:\n\n" + summary
        ),
    }


def find_previous_summary(messages: list[dict]) -> Optional[str]:
    """Return the anchored summary text from the most recent compaction marker,
    if one is present — so the next compaction can update it instead of
    starting over."""
    for m in reversed(messages):
        content = m.get("content")
        if m.get("role") == "system" and isinstance(content, str) and content.startswith(SUMMARY_PREFIX):
            _, _, body = content.partition("\n\n")
            return body.strip() or None
    return None


def compact_messages(
    messages: list[dict],
    *,
    summarize: Callable[[list[dict], str], str],
    context: int,
    output_limit: int = 0,
    input_limit: Optional[int] = None,
    previous_summary: Optional[str] = None,
    tail_turns: int = DEFAULT_TAIL_TURNS,
    preserve_tokens: Optional[int] = None,
) -> Optional[dict]:
    """Compact ``messages`` in place-equivalent fashion.

    Splits off the head, summarizes it via the ``summarize`` callback, and
    returns a dict describing the result::

        {"messages": [...new list...], "summary": "<text>", "summarized": N}

    Returns ``None`` when there's nothing worth compacting (head too small) so
    callers can no-op. The callback receives ``(head_messages, prompt)`` and
    must return the summary text.
    """
    leading_system, head, tail = split_for_compaction(
        messages,
        context=context,
        output_limit=output_limit,
        input_limit=input_limit,
        tail_turns=tail_turns,
        preserve_tokens=preserve_tokens,
    )
    if not head:
        return None
    # A prior anchor (from an earlier compaction) is superseded by this one:
    # drop it from the leading system block and feed its text in so the new
    # summary merges rather than discards it.
    if previous_summary is None:
        previous_summary = find_previous_summary(leading_system)
    leading_system = [
        m
        for m in leading_system
        if not (isinstance(m.get("content"), str) and m["content"].startswith(SUMMARY_PREFIX))
    ]
    prompt = build_prompt(previous_summary=previous_summary)
    summary = (summarize(head, prompt) or "").strip()
    if not summary:
        return None
    new_messages = [*leading_system, summary_message(summary), *tail]
    return {"messages": new_messages, "summary": summary, "summarized": len(head)}


# ---------------------------------------------------------------------------
# prune — erase stale tool outputs without an LLM call
# ---------------------------------------------------------------------------


def prune_messages(messages: list[dict], *, protect: int = PRUNE_PROTECT, minimum: int = PRUNE_MINIMUM) -> int:
    """Blank out the *output* of older ``tool`` messages to reclaim context,
    keeping the most recent ``protect`` tokens of tool output intact.

    Walks backward, tallying tool-output tokens; once
    past the protected window, marks older tool messages for pruning. Only
    actually prunes if it would free more than ``minimum`` tokens — otherwise
    the churn isn't worth it. Mutates ``messages`` in place; returns the number
    of tool messages pruned.
    """
    seen = 0
    freed = 0
    to_prune: list[dict] = []
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str) or content == TOOL_OUTPUT_PRUNED:
            continue
        size = estimate_tokens(content)
        seen += size
        if seen <= protect:
            continue
        freed += size
        to_prune.append(m)

    if freed <= minimum:
        return 0
    for m in to_prune:
        m["content"] = TOOL_OUTPUT_PRUNED
    return len(to_prune)
