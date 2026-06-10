"""Gemini (generateContent) protocol adapter.

Port of opencode's gemini.ts, scoped to gorchestra. Translates
chat-completions-shaped messages to Gemini's ``contents`` body, streams
generateContent, and re-emits gorchestra's event tuples. Reasoning variants
apply as the native ``thinkingConfig``.

Transport rides the official ``google-genai`` SDK. Lowering still produces
wire-shaped (camelCase) dicts — they cross into the SDK's types via JSON-mode
validation and come back out via JSON-mode dumps, because the SDK models
base64 fields (``inlineData.data``, ``thoughtSignature``) as ``bytes`` and
only its JSON mode converts base64 strings losslessly in both directions.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.llm.sse import sanitize_json_schema, compute_cost

PROTOCOL = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _user_parts(content: Any) -> list[dict]:
    """Lower user content — a raw string or an OpenAI-style parts array
    (text / image_url / file with base64 data URLs) — to Gemini parts.
    Images and PDFs both ride ``inlineData``."""
    if not isinstance(content, list):
        return [{"text": _as_text(content)}]

    def inline(url: str) -> dict | None:
        head, _, data = url.partition(";base64,")
        mime = head[len("data:"):] if head.startswith("data:") else ""
        return {"inlineData": {"mimeType": mime, "data": data}} if mime and data else None

    parts: list[dict] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            parts.append({"text": p["text"]})
        elif p.get("type") == "image_url":
            if part := inline((p.get("image_url") or {}).get("url") or ""):
                parts.append(part)
        elif p.get("type") == "file":
            if part := inline((p.get("file") or {}).get("file_data") or ""):
                parts.append(part)
    return parts or [{"text": ""}]


def _lower(messages: list[dict]) -> tuple[dict | None, list[dict]]:
    system_parts: list[dict] = []
    contents: list[dict] = []
    id_to_name: dict[str, str] = {}

    def push_user_part(part: dict) -> None:
        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"].append(part)
        else:
            contents.append({"role": "user", "parts": [part]})

    for m in messages:
        role = m.get("role")
        if role == "system":
            txt = _as_text(m.get("content"))
            if txt:
                system_parts.append({"text": txt})
            continue
        if role == "user":
            for part in _user_parts(m.get("content")):
                push_user_part(part)
            continue
        if role == "tool":
            name = id_to_name.get(m.get("tool_call_id") or "", m.get("name") or "tool")
            push_user_part({
                "functionResponse": {"name": name, "response": {"name": name, "content": _as_text(m.get("content"))}}
            })
            # functionResponse is JSON-only; attachments (image bytes from
            # read_file) ride alongside as inlineData parts in the same user
            # turn.
            for a in (m.get("attachments") or []):
                push_user_part({
                    "inlineData": {"mimeType": a.get("mime") or "", "data": a.get("data") or ""}
                })
            continue
        if role == "assistant":
            parts: list[dict] = []
            for rd in (m.get("reasoning_details") or []):
                text = rd.get("text") or rd.get("thinking")
                sig = rd.get("thoughtSignature") or rd.get("signature")
                if text and sig:
                    parts.append({"text": text, "thought": True, "thoughtSignature": sig})
            txt = _as_text(m.get("content"))
            if txt:
                parts.append({"text": txt})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.get("id"):
                    id_to_name[tc["id"]] = fn.get("name") or ""
                fc_part: dict = {"functionCall": {"name": fn.get("name") or "", "args": args}}
                # Gemini attaches a thoughtSignature to the functionCall part
                # (not the thought part); it must be echoed back or the model
                # rejects the turn / loses reasoning continuity.
                if tc.get("thoughtSignature"):
                    fc_part["thoughtSignature"] = tc["thoughtSignature"]
                parts.append(fc_part)
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            continue

    system_instruction = {"parts": system_parts} if system_parts else None
    return system_instruction, contents


def _lower_tools(tool_schemas: list[dict]) -> list[dict]:
    decls = []
    for t in tool_schemas or []:
        fn = t.get("function") or {}
        decls.append({
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": sanitize_json_schema(fn.get("parameters") or {"type": "object", "properties": {}}),
        })
    return [{"functionDeclarations": decls}] if decls else []


def _parse_events(events: Iterator[dict]) -> Iterator[tuple]:
    """Parse generateContent stream chunks (as wire-shaped camelCase dicts)
    into gorchestra event tuples, ending with the assembled ``done`` payload
    (usage carries token counts; the caller adds cost)."""
    content_parts: list[str] = []
    tool_calls: list[dict] = []
    reasoning: dict = {"type": "thinking", "text": "", "thoughtSignature": None}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    tc_index = 0

    for ev in events:
        um = ev.get("usageMetadata")
        if um:
            usage["prompt_tokens"] = um.get("promptTokenCount") or usage["prompt_tokens"]
            cand = um.get("candidatesTokenCount") or 0
            usage["completion_tokens"] = cand + (um.get("thoughtsTokenCount") or 0)
            # promptTokenCount already includes cached content; expose the
            # subset so it bills at the cheaper cache-read rate.
            usage["cache_read_tokens"] = um.get("cachedContentTokenCount") or 0
        cand0 = (ev.get("candidates") or [{}])[0]
        content = cand0.get("content") or {}
        for part in content.get("parts") or []:
            if part.get("thoughtSignature") and part.get("thought"):
                reasoning["thoughtSignature"] = part["thoughtSignature"]
            text = part.get("text")
            if text:
                if part.get("thought"):
                    reasoning["text"] += text
                    yield ("thinking", text)
                else:
                    content_parts.append(text)
                    yield ("text", text)
                continue
            fc = part.get("functionCall")
            if fc:
                args_json = json.dumps(fc.get("args") or {})
                tc: dict = {
                    "id": f"tool_{tc_index}",
                    "type": "function",
                    "function": {"name": fc.get("name") or "", "arguments": args_json},
                }
                # Carry the per-call thoughtSignature so it round-trips
                # back to Gemini on the next turn (see _lower).
                if part.get("thoughtSignature"):
                    tc["thoughtSignature"] = part["thoughtSignature"]
                tool_calls.append(tc)
                yield ("tool_args", tc_index, fc.get("name") or "", args_json)
                tc_index += 1

    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning["text"]:
        msg["reasoning_details"] = [reasoning]
    yield ("done", {"message": msg, "usage": usage})


def _sdk_client(base_url: str, api_key: str, extra_headers: dict | None) -> genai.Client:
    """The SDK joins ``base_url + api_version + path``; our configured base
    URLs carry the version suffix (the raw-wire convention), so split it."""
    base = base_url.rstrip("/")
    http_options: dict[str, Any] = {"timeout": None}
    for suffix in ("/v1beta", "/v1alpha", "/v1"):
        if base.endswith(suffix):
            base, http_options["api_version"] = base[: -len(suffix)], suffix[1:]
            break
    http_options["base_url"] = base
    if extra_headers:
        http_options["headers"] = extra_headers
    return genai.Client(api_key=api_key, http_options=http_options)


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
    extra_body: dict | None = None,  # OpenAI-shaped caller opts; not portable here.
    cancel_event=None,
) -> Iterator[tuple]:
    system_instruction, contents = _lower(messages)
    # JSON-mode validation decodes the base64 fields into the SDK's bytes
    # types; python-mode would utf-8-encode the strings and corrupt them.
    sdk_contents = [genai_types.Content.model_validate_json(json.dumps(c)) for c in contents]
    config: dict[str, Any] = {}
    if system_instruction:
        config["system_instruction"] = system_instruction
    tools = _lower_tools(tool_schemas)
    if tools:
        config["tools"] = tools
    tcfg = (variant_opts or {}).get("thinkingConfig")
    if isinstance(tcfg, dict):
        config["thinking_config"] = tcfg

    client = _sdk_client(base_url, api_key, extra_headers)
    try:
        stream = client.models.generate_content_stream(
            model=model, contents=sdk_contents, config=config or None
        )

        def _events():
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    return
                yield chunk.model_dump(mode="json", by_alias=True, exclude_none=True)

        for item in _parse_events(_events()):
            if item[0] == "done":
                item[1]["usage"]["cost"] = compute_cost(item[1]["usage"], cost)
            yield item
    except genai_errors.APIError as e:
        raise RuntimeError(f"LLM {e.code}: {str(e)[:500]}") from None
