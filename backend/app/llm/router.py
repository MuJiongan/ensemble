"""Protocol routing: pick the right adapter + execution plan for a model.

Mirrors opencode's route layer (which protocol, which endpoint/auth) but scoped
to key-based providers. The protocol is chosen from the catalog model's SDK
package (``npm``), exactly as opencode keys its routes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.catalog import models_dev as md
from app.catalog import providers as prov
from app.catalog.variants import base_options, merge_options
from app.llm import anthropic_messages, gemini, openai_chat, openai_responses

_ANTHROPIC_NPM = {"@ai-sdk/anthropic", "@ai-sdk/google-vertex/anthropic"}
_GEMINI_NPM = {"@ai-sdk/google", "@ai-sdk/google-vertex"}

OPENROUTER_DEFAULT = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class ExecPlan:
    stream_round: Callable
    protocol: str
    base_url: str
    variant_opts: dict = field(default_factory=dict)
    extra_headers: dict = field(default_factory=dict)
    model_output_limit: int = 0
    cost: dict | None = None


def _select(provider_id: str, model_id: str):
    model = md.get_model(provider_id, model_id) if provider_id else None
    npm = model.npm if model else None
    if npm in _ANTHROPIC_NPM:
        return anthropic_messages.stream_round, anthropic_messages.PROTOCOL, anthropic_messages.DEFAULT_BASE_URL
    if npm in _GEMINI_NPM:
        return gemini.stream_round, gemini.PROTOCOL, gemini.DEFAULT_BASE_URL
    # Native OpenAI speaks the Responses API — chat completions rejects the
    # agentic shape (function tools + reasoning_effort) for its newest models.
    # Matching on provider id too keeps the route on a catalog miss. (The
    # codex synthetic provider shares this npm but is dispatched before the
    # router ever sees it.)
    if provider_id == "openai" or npm == "@ai-sdk/openai":
        return openai_responses.stream_round, openai_responses.PROTOCOL, openai_responses.DEFAULT_BASE_URL
    return openai_chat.stream_round, openai_chat.PROTOCOL, OPENROUTER_DEFAULT


def plan(provider_id: str, model_id: str, variant: str | None, base_url: str = "") -> ExecPlan:
    """Build an execution plan. ``base_url`` is the caller-resolved endpoint
    (env ``LLM_BASE_URL``); falls back to the protocol's native default."""
    provider_id = (provider_id or "").strip()
    sr, proto, default_base = _select(provider_id, model_id)
    model = md.get_model(provider_id, model_id) if provider_id else None
    # Always-on provider defaults (turns reasoning on, sets quirks), with the
    # selected variant deep-merged over the top — mirrors opencode's
    # mergeOptions(base_options, …, variant).
    variant_opts: dict = base_options(model) if model is not None else {}
    if model is not None and variant and variant in model.variants:
        variant_opts = merge_options(variant_opts, model.variants[variant])
    return ExecPlan(
        stream_round=sr,
        protocol=proto,
        base_url=(base_url or default_base),
        variant_opts=variant_opts,
        extra_headers=dict(prov.PROVIDER_REQUEST_HEADERS.get(provider_id, {})),
        model_output_limit=(model.limit.output if model else 0),
        cost=(model.cost if model else None),
    )
