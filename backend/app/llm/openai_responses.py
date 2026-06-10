"""OpenAI Responses protocol adapter.

The native protocol for OpenAI's API-key models. OpenAI gates the agentic
request shape — function tools combined with ``reasoning_effort`` — behind
``/v1/responses`` for its newest models (chat completions 400s with "Please
use /v1/responses instead"), so every native-OpenAI model routes through
here. The adapter translates gorchestra's chat-completions message shape into
Responses ``input`` items, streams the Responses API, and re-emits the shared
event tuples.

Requests are stateless (``store: false``): the request asks for
``reasoning.encrypted_content``, the encrypted blobs land on the assistant
message's ``reasoning_details``, and the next round echoes them back as
``reasoning`` input items so the model keeps its chain of thought across tool
calls without OpenAI persisting anything server-side.

Transport rides the official ``openai`` SDK (``client.responses``); request
lowering and event parsing stay ours, with non-core fields on ``extra_body``.
The translation/parsing helpers here are shared with the
ChatGPT-subscription caller (:mod:`app.auth.codex_api`), which speaks the same
protocol against ``chatgpt.com`` — raw httpx there, since OAuth headers and
the Codex backend aren't an SDK target — via the line-based
:func:`parse_responses_sse` wrapper.
"""
from __future__ import annotations

import json
from typing import Any, Iterator, Optional

from openai import OpenAI, APIStatusError

from app.llm.openai_chat import lower_attachments
from app.llm.sse import iter_sse_json, compute_cost

PROTOCOL = "openai_responses"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Marker on reasoning_details entries produced by this adapter, so input
# lowering only echoes blobs that really are Responses-API reasoning items
# (Anthropic/OpenRouter entries use their own shapes and must not leak here).
REASONING_FORMAT = "openai-responses-v1"

# Chat-completions body fields whose Responses-API spelling differs. Caller
# opts arrive in chat shape (ctx.call_llm(**opts)); remap on the way out.
_EXTRA_KEY_MAP = {"max_tokens": "max_output_tokens", "max_completion_tokens": "max_output_tokens"}


# ---------------------------------------------------------------------------
# Translation: chat-completions shape  →  Responses API shape
# ---------------------------------------------------------------------------

def _user_input_parts(content) -> tuple[list[dict], str]:
    """Lower user content — a raw string or an OpenAI-style parts array
    (text / image_url / file with base64 data URLs) — to Responses API input
    parts. Returns ``(parts, text)`` with ``text`` being the concatenated
    visible text (used for Codex's ``instructions`` fallback)."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}], content
    if not isinstance(content, list):
        text = json.dumps(content)
        return [{"type": "input_text", "text": text}], text
    parts: list[dict] = []
    texts: list[str] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            parts.append({"type": "input_text", "text": p["text"]})
            texts.append(p["text"])
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url") or ""
            if url:
                parts.append({"type": "input_image", "image_url": url})
        elif p.get("type") == "file":
            f = p.get("file") or {}
            if f.get("file_data"):
                parts.append({
                    "type": "input_file",
                    "filename": f.get("filename") or "document.pdf",
                    "file_data": f["file_data"],
                })
    return parts or [{"type": "input_text", "text": ""}], "".join(texts)


def _reasoning_input_items(msg: dict) -> list[dict]:
    """Echoable ``reasoning`` input items from an assistant message.

    Only entries this adapter produced (format marker) that carry the
    encrypted payload round-trip; ids are stripped — with ``store: false``
    the items aren't persisted server-side, so the encrypted content is the
    whole state and a dangling ``rs_…`` pointer would 400."""
    items: list[dict] = []
    for rd in msg.get("reasoning_details") or []:
        if not isinstance(rd, dict) or rd.get("format") != REASONING_FORMAT:
            continue
        if not rd.get("data"):
            continue
        items.append({
            "type": "reasoning",
            "summary": rd.get("summary") or [],
            "encrypted_content": rd["data"],
        })
    return items


def to_responses_input(
    messages: list[dict], instructions_fallback: bool = False
) -> tuple[Optional[str], list[dict]]:
    """Convert a chat-completions ``messages`` list into Responses API input.

    Returns ``(instructions, input_items)``:
      * ``instructions`` is the concatenated text of any ``role=system``
        messages (Responses API takes them as a separate top-level field).
        With ``instructions_fallback`` (Codex requires a non-empty
        ``instructions``) the first user message's text stands in when no
        system message exists; otherwise it stays ``None``.
      * ``input_items`` is the input list of ``reasoning`` / ``message`` /
        ``function_call`` / ``function_call_output`` items, preserving
        original order.
    """
    # function_call_output is text-only — relocate tool-result attachments
    # (image bytes from read_file) into a user message of OpenAI-style
    # parts, which _user_input_parts lowers to input_image.
    messages = lower_attachments(messages)

    instructions_parts: list[str] = []
    items: list[dict] = []
    first_user_text: Optional[str] = None

    for m in messages:
        role = m.get("role")
        if role == "system":
            content = m.get("content") or ""
            if isinstance(content, str) and content:
                instructions_parts.append(content)
            continue

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id") or "",
                    "output": m.get("content") or "",
                }
            )
            continue

        if role == "assistant":
            # Assistant turn may carry reasoning, visible text, and tool calls.
            content = m.get("content") or ""
            tool_calls = m.get("tool_calls") or []
            # A reasoning input item must be followed by the item it preceded
            # in the original output — the API rejects one with nothing after
            # it, so only echo when this turn still has content to follow.
            if content or tool_calls:
                items.extend(_reasoning_input_items(m))
            if isinstance(content, str) and content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for tc in tool_calls:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "arguments": fn.get("arguments") or "",
                    }
                )
            continue

        # user / anything else → input_text (+ input_image) message
        content = m.get("content") or ""
        parts, text = _user_input_parts(content)
        items.append(
            {
                "type": "message",
                "role": role or "user",
                "content": parts,
            }
        )
        if role == "user" and first_user_text is None and text:
            first_user_text = text

    if instructions_parts:
        instructions: Optional[str] = "\n\n".join(instructions_parts)
    else:
        instructions = first_user_text if instructions_fallback else None
    return instructions, items


def to_responses_tools(tool_schemas: list[dict]) -> list[dict]:
    """Chat-completions tools (``{"type":"function","function":{...}}``) →
    Responses-API tools (flat: ``{"type":"function","name":...,"parameters":...}``)."""
    out: list[dict] = []
    for t in tool_schemas:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {},
            }
        )
    return out


# ---------------------------------------------------------------------------
# SSE parsing: Responses API events  →  internal event tuples
# ---------------------------------------------------------------------------

def _summary_text(summary: list) -> str:
    return "\n\n".join(
        s.get("text") or ""
        for s in summary
        if isinstance(s, dict) and s.get("type") == "summary_text" and s.get("text")
    )


def _reasoning_entry(item: dict) -> dict:
    """One reasoning output item → a ``reasoning_details`` entry.

    ``data`` carries the encrypted payload (what the next round echoes back);
    ``text`` the visible summary so persisted history renders the thinking
    collapsible. Entries with neither are dropped by the caller — and by
    persistence, which only replays self-contained blocks."""
    entry: dict = {"type": "reasoning.encrypted", "format": REASONING_FORMAT}
    summary = item.get("summary") or []
    if summary:
        entry["summary"] = summary
        text = _summary_text(summary)
        if text:
            entry["text"] = text
    if item.get("encrypted_content"):
        entry["data"] = item["encrypted_content"]
    return entry


def _usage_from(u: dict) -> dict:
    """Normalize Responses-API usage keys to chat-completions ones so the
    cost/token UI keeps working. ``input_tokens`` is inclusive of cached input;
    expose the cached subset so it bills at the cache-read rate."""
    usage = {
        "prompt_tokens": u.get("input_tokens", u.get("prompt_tokens", 0)) or 0,
        "completion_tokens": u.get("output_tokens", u.get("completion_tokens", 0)) or 0,
    }
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
    if cached:
        usage["cache_read_tokens"] = cached
    return usage


def parse_responses_sse(lines: Iterator[str], cancel_event=None) -> Iterator[tuple]:
    """Parse a Responses API SSE stream (``data:`` lines) — the entry point
    for raw-httpx callers (Codex). See :func:`parse_responses_events`."""
    return parse_responses_events(iter_sse_json(lines, cancel_event))


def parse_responses_events(events: Iterator[dict]) -> Iterator[tuple]:
    """Parse Responses API stream events (as dicts, wire-shaped).

    Yields the same tuple shapes the chat-completions parser does:

      ``("text",     delta_str)``                     visible content delta
      ``("thinking", delta_str)``                     reasoning delta
      ``("tool_args", tc_index, name, args_delta)``   streaming tool call args
      ``("done", {"message": assistant_msg, "usage": {...}})``

    The assembled ``assistant_msg`` is in chat-completions shape — so the
    rest of the agent loop (which keeps a single chat-completions message
    history) can append it verbatim. Reasoning items land on the message's
    ``reasoning_details`` (summary text + encrypted payload) for replay.
    """
    content_parts: list[str] = []
    # Track function_call items by their item_id (assigned in output_item.added).
    # The Responses API uses an opaque item_id; we map it to a stable index so
    # callers can co-render multiple in-flight tool calls.
    fn_items: dict[str, dict] = {}
    fn_order: list[str] = []
    reasoning_entries: dict[str, dict] = {}
    thinking_emitted = False
    usage: dict = {}

    for evt in events:
        ev_type = evt.get("type") or ""

        if ev_type == "response.output_item.added":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                item_id = item.get("id") or f"_idx{len(fn_order)}"
                fn_items[item_id] = {
                    "id": item.get("call_id") or item_id,
                    "type": "function",
                    "function": {"name": item.get("name") or "", "arguments": ""},
                }
                fn_order.append(item_id)
            continue

        if ev_type == "response.output_text.delta":
            delta = evt.get("delta") or ""
            if delta:
                content_parts.append(delta)
                yield ("text", delta)
            continue

        if ev_type == "response.reasoning_summary_part.added":
            # Summary parts are separate paragraphs; keep them readable when
            # concatenated into one thinking stream.
            if thinking_emitted:
                yield ("thinking", "\n\n")
            continue

        if ev_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
            "response.reasoning.delta",
        ):
            delta = evt.get("delta") or ""
            if delta:
                thinking_emitted = True
                yield ("thinking", delta)
            continue

        if ev_type == "response.function_call_arguments.delta":
            item_id = evt.get("item_id") or ""
            delta = evt.get("delta") or ""
            cur = fn_items.get(item_id)
            if cur is None:
                # Argument delta arrived before the item.added event — rare;
                # synthesize a placeholder so we don't drop the data.
                cur = {
                    "id": item_id,
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
                fn_items[item_id] = cur
                fn_order.append(item_id)
            cur["function"]["arguments"] += delta
            if delta:
                idx = fn_order.index(item_id)
                yield ("tool_args", idx, cur["function"]["name"], delta)
            continue

        if ev_type == "response.output_item.done":
            item = evt.get("item") or {}
            itype = item.get("type")
            item_id = item.get("id") or ""
            if itype == "function_call" and item_id in fn_items:
                cur = fn_items[item_id]
                if item.get("call_id"):
                    cur["id"] = item["call_id"]
                if item.get("name"):
                    cur["function"]["name"] = item["name"]
                # The done item carries the full arguments — authoritative
                # over whatever deltas accumulated.
                if item.get("arguments"):
                    cur["function"]["arguments"] = item["arguments"]
            elif itype == "reasoning":
                entry = _reasoning_entry(item)
                if entry.get("data") or entry.get("text"):
                    reasoning_entries[item_id or f"_rs{len(reasoning_entries)}"] = entry
            continue

        if ev_type in ("response.completed", "response.incomplete"):
            u = (evt.get("response") or {}).get("usage") or {}
            if u:
                usage = _usage_from(u)
            continue

        if ev_type == "response.failed":
            resp = evt.get("response") or {}
            err = (resp.get("error") or {}).get("message") or "response failed"
            raise RuntimeError(f"OpenAI Responses: {err}")

        if ev_type == "error":
            raise RuntimeError(f"OpenAI Responses: {evt.get('message') or 'stream error'}")

    tool_calls = [fn_items[i] for i in fn_order if fn_items[i]["function"]["name"]]
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_entries:
        msg["reasoning_details"] = list(reasoning_entries.values())
    yield ("done", {"message": msg, "usage": usage})


def _parse_response_json(data: dict) -> tuple[dict, dict]:
    """Non-streaming response object → ``(assistant_msg, usage)``."""
    if data.get("status") == "failed":
        err = (data.get("error") or {}).get("message") or "response failed"
        raise RuntimeError(f"OpenAI Responses: {err}")
    content_parts: list[str] = []
    tool_calls: list[dict] = []
    reasoning: list[dict] = []
    for item in data.get("output") or []:
        itype = item.get("type")
        if itype == "message":
            for p in item.get("content") or []:
                if p.get("type") == "output_text" and p.get("text"):
                    content_parts.append(p["text"])
        elif itype == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    },
                }
            )
        elif itype == "reasoning":
            entry = _reasoning_entry(item)
            if entry.get("data") or entry.get("text"):
                reasoning.append(entry)
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning:
        msg["reasoning_details"] = reasoning
    return msg, _usage_from(data.get("usage") or {})


# ---------------------------------------------------------------------------
# Adapter entry point
# ---------------------------------------------------------------------------

def _variant_body(variant_opts: dict | None) -> dict:
    """Translate the catalog's provider-option dict (camelCase shape — see
    ``app.catalog.variants``) into Responses-API body fields. These are
    exactly the fields ``to_openai_body`` drops as "Responses-API-only":
    here they're native."""
    if not variant_opts:
        return {}
    body: dict = {}
    reasoning: dict = {}
    effort = variant_opts.get("reasoningEffort") or variant_opts.get("reasoning_effort")
    if effort:
        reasoning["effort"] = effort
    if variant_opts.get("reasoningSummary"):
        reasoning["summary"] = variant_opts["reasoningSummary"]
    if reasoning:
        body["reasoning"] = reasoning
    if variant_opts.get("include"):
        body["include"] = list(variant_opts["include"])
    if variant_opts.get("textVerbosity"):
        body["text"] = {"verbosity": variant_opts["textVerbosity"]}
    return body


def _build_payload(model, messages, tool_schemas, variant_opts, streaming, extra_body):
    instructions, input_items = to_responses_input(messages)
    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        # Stateless: nothing persisted on OpenAI's side; reasoning state
        # round-trips via encrypted_content on reasoning_details instead.
        "store": False,
    }
    if instructions:
        payload["instructions"] = instructions
    if tool_schemas:
        payload["tools"] = to_responses_tools(tool_schemas)
    if streaming:
        payload["stream"] = True
    payload.update(_variant_body(variant_opts))
    # Caller-supplied opts win (merged last), remapped to Responses spellings.
    for k, v in (extra_body or {}).items():
        payload[_EXTRA_KEY_MAP.get(k, k)] = v
    return payload


def _split_payload(payload: dict) -> dict:
    """Wire-shaped payload → SDK call kwargs. ``model``/``input`` become typed
    params; the rest (instructions/store/tools/reasoning/include/text + caller
    opts) rides ``extra_body``, which the SDK merges verbatim."""
    kwargs: dict[str, Any] = {"model": payload.pop("model"), "input": payload.pop("input")}
    payload.pop("stream", None)  # passed as the typed `stream` param instead
    if payload:
        kwargs["extra_body"] = payload
    return kwargs


def stream_round(
    *,
    model: str,
    messages: list[dict],
    tool_schemas: list[dict],
    base_url: str,
    api_key: str,
    variant_opts: dict | None = None,
    extra_headers: dict | None = None,
    model_output_limit: int = 0,
    cost: dict | None = None,
    streaming: bool = True,
    extra_body: dict | None = None,
    cancel_event=None,
) -> Iterator[tuple]:
    payload = _build_payload(model, messages, tool_schemas, variant_opts, streaming, extra_body)
    kwargs = _split_payload(payload)

    with OpenAI(
        api_key=api_key, base_url=base_url, default_headers=extra_headers or None, timeout=None
    ) as client:
        try:
            if streaming:
                with client.responses.create(stream=True, **kwargs) as stream:

                    def _events():
                        for ev in stream:
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            yield ev.model_dump(exclude_none=True)

                    for item in parse_responses_events(_events()):
                        if item[0] == "done":
                            item[1]["usage"]["cost"] = compute_cost(item[1]["usage"], cost)
                        yield item
            else:
                data = client.responses.create(stream=False, **kwargs).model_dump(exclude_none=True)
                msg, usage = _parse_response_json(data)
                usage["cost"] = compute_cost(usage, cost)
                yield ("done", {"message": msg, "usage": usage})
        except APIStatusError as e:
            raise RuntimeError(f"LLM {e.status_code}: {e.response.text[:500]}") from None
