"""Shared SSE + tool-schema helpers for the protocol adapters."""
from __future__ import annotations

import json
from typing import Any, Iterator


def iter_sse_json(lines: Iterator[str], cancel_event=None) -> Iterator[dict]:
    """Yield parsed JSON objects from ``data:`` SSE frames.

    Skips comments / ``event:`` / ``id:`` lines, stops on ``[DONE]``, and bails
    between frames if ``cancel_event`` is set."""
    for line in lines:
        if cancel_event is not None and cancel_event.is_set():
            return
        if not line or not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            return
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            continue


# Keys gemini/anthropic-native JSON-schema tool params don't accept; strip them.
_SCHEMA_DROP = {"additionalProperties", "$schema", "$ref", "$defs", "definitions", "default", "title", "uniqueItems"}


def sanitize_json_schema(schema: Any) -> Any:
    """Best-effort cleanup of a JSON Schema for providers that reject the
    OpenAI/JSON-Schema superset (Gemini especially). Recursively drops
    unsupported keys; collapses ``type: [..., "null"]`` to ``nullable``."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in _SCHEMA_DROP:
            continue
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in v:
                out["nullable"] = True
            continue
        if k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: sanitize_json_schema(pv) for pk, pv in v.items()}
            continue
        if k in ("items",):
            out["items"] = sanitize_json_schema(v)
            continue
        if k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            out[k] = [sanitize_json_schema(s) for s in v]
            continue
        out[k] = v
    return out


def compute_cost(usage: dict, cost: dict | None) -> float:
    """Estimate USD cost from token usage + catalog per-Mtoken cost, or 0."""
    if not cost:
        return 0.0
    pin = (usage.get("prompt_tokens") or 0) * (cost.get("input") or 0) / 1_000_000
    pout = (usage.get("completion_tokens") or 0) * (cost.get("output") or 0) / 1_000_000
    return float(pin + pout)
