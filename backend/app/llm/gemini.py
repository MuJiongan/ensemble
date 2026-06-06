"""Gemini (generateContent) protocol adapter.

Port of opencode's gemini.ts, scoped to gorchestra. Translates
chat-completions-shaped messages to Gemini's ``contents`` body, streams
``:streamGenerateContent?alt=sse``, and re-emits gorchestra's event tuples.
Reasoning variants apply as the native ``generationConfig.thinkingConfig``.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from app.llm.sse import iter_sse_json, sanitize_json_schema, compute_cost

PROTOCOL = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


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
            push_user_part({"text": _as_text(m.get("content"))})
            continue
        if role == "tool":
            name = id_to_name.get(m.get("tool_call_id") or "", m.get("name") or "tool")
            push_user_part({
                "functionResponse": {"name": name, "response": {"name": name, "content": _as_text(m.get("content"))}}
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
    body: dict[str, Any] = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = system_instruction
    tools = _lower_tools(tool_schemas)
    if tools:
        body["tools"] = tools
    tcfg = (variant_opts or {}).get("thinkingConfig")
    if isinstance(tcfg, dict):
        body["generationConfig"] = {"thinkingConfig": tcfg}

    headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    headers.update(extra_headers or {})
    url = f"{base_url.rstrip('/')}/models/{model}:streamGenerateContent?alt=sse"

    content_parts: list[str] = []
    tool_calls: list[dict] = []
    reasoning: dict = {"type": "thinking", "text": "", "thoughtSignature": None}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    tc_index = 0

    with httpx.Client(timeout=None) as client:
        with client.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code >= 400:
                detail = r.read().decode(errors="replace")[:500]
                raise RuntimeError(f"LLM {r.status_code}: {detail}")

            def _lines():
                for line in r.iter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    yield line

            for ev in iter_sse_json(_lines(), cancel_event):
                um = ev.get("usageMetadata")
                if um:
                    usage["prompt_tokens"] = um.get("promptTokenCount") or usage["prompt_tokens"]
                    cand = um.get("candidatesTokenCount") or 0
                    usage["completion_tokens"] = cand + (um.get("thoughtsTokenCount") or 0)
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
    usage["cost"] = compute_cost(usage, cost)
    yield ("done", {"message": msg, "usage": usage})
