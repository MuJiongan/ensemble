"""Per-model reasoning "variants".

A faithful Python port of opencode's ``packages/opencode/src/provider/transform.ts``
``variants()`` function (and its helpers, lines 490-1007).

``variants(model)`` returns ``{variant_name: provider_option_dict}`` — e.g.
``{"low": {"reasoningEffort": "low"}, "high": {...}, "max": {...}}`` — keyed off
the model's SDK package (``npm``), id, and release date, exactly like opencode.
It returns ``{}`` for models that don't reason or don't expose a toggle.

The provider-option dicts are kept in opencode's native (camelCase / nested)
shape so the catalog is forward-compatible with every provider. For execution,
``to_openai_body()`` translates the OpenAI-compatible subset into chat-completions
request-body fields (``reasoning_effort`` / ``reasoning``); native non-OAI dicts
(Anthropic ``thinking``, Google ``thinkingConfig``, Bedrock ``reasoningConfig``,
SAP ``modelParams``) are dropped, since gorchestra speaks one OpenAI-compatible
wire format.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.catalog.models_dev import CatalogModel

INCLUDE_ENCRYPTED_REASONING = ["reasoning.encrypted_content"]

WIDELY_SUPPORTED_EFFORTS = ["low", "medium", "high"]
OPENAI_EFFORTS = ["none", "minimal", *WIDELY_SUPPORTED_EFFORTS, "xhigh"]
OPENAI_GPT5_1_EFFORTS = ["none", *WIDELY_SUPPORTED_EFFORTS]
OPENAI_GPT5_2_PLUS_EFFORTS = [*OPENAI_GPT5_1_EFFORTS, "xhigh"]
OPENAI_GPT5_PRO_EFFORTS = ["high"]
OPENAI_GPT5_PRO_2_PLUS_EFFORTS = ["medium", "high", "xhigh"]
OPENAI_GPT5_CHAT_EFFORTS = ["medium"]
OPENAI_GPT5_CODEX_XHIGH_EFFORTS = [*WIDELY_SUPPORTED_EFFORTS, "xhigh"]
OPENAI_GPT5_CODEX_3_PLUS_EFFORTS = ["none", *OPENAI_GPT5_CODEX_XHIGH_EFFORTS]

# Dates OpenAI rolled out the `none` / `xhigh` reasoning_effort tiers. Models
# older than these 400 on the new tier, so we only expose it when new enough.
# (ISO date strings compare lexically, matching the TS `>=` comparisons.)
OPENAI_NONE_EFFORT_RELEASE_DATE = "2025-11-13"
OPENAI_XHIGH_EFFORT_RELEASE_DATE = "2025-12-04"

GPT5_FAMILY_RE = re.compile(r"(?:^|/)gpt-5(?:[.-]|$)")
GPT5_VERSION_RE = re.compile(r"(?:^|/)gpt-5[.-](\d+)(?:[.-]|$)")
GPT5_PRO_RE = re.compile(r"(?:^|/)gpt-5[.-]?pro(?:[.-]|$)")
GPT5_VERSIONED_PRO_RE = re.compile(r"(?:^|/)gpt-5[.-]\d+[.-]pro(?:[.-]|$)")
_OPUS_47_RE = re.compile(
    r"opus-(\d+)[.-](\d+)(?:[.@-]|$)|claude-(\d+)[.-](\d+)-opus(?:[.@-]|$)", re.I
)


def _gpt5_version(api_id: str):
    m = GPT5_VERSION_RE.search(api_id)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return n or None


def _versioned_gpt5_efforts(api_id: str):
    if GPT5_VERSIONED_PRO_RE.search(api_id):
        return OPENAI_GPT5_PRO_2_PLUS_EFFORTS
    version = _gpt5_version(api_id)
    if version is None:
        return None
    if version == 1:
        return OPENAI_GPT5_1_EFFORTS
    return OPENAI_GPT5_2_PLUS_EFFORTS


def _gpt5_codex_efforts(api_id: str):
    if not GPT5_FAMILY_RE.search(api_id) or "codex" not in api_id:
        return None
    version = _gpt5_version(api_id)
    if version is not None and version >= 3:
        return OPENAI_GPT5_CODEX_3_PLUS_EFFORTS
    if "codex-max" in api_id or (version is not None and version >= 2):
        return OPENAI_GPT5_CODEX_XHIGH_EFFORTS
    return WIDELY_SUPPORTED_EFFORTS


def _gpt5_chat_efforts(api_id: str):
    if not GPT5_FAMILY_RE.search(api_id) or "-chat" not in api_id:
        return None
    return [] if _gpt5_version(api_id) is None else OPENAI_GPT5_CHAT_EFFORTS


def openai_reasoning_efforts(api_id: str, release_date: str):
    cid = api_id.lower()
    if "deep-research" in cid:
        return ["medium"]
    chat = _gpt5_chat_efforts(cid)
    if chat is not None:
        return chat
    if GPT5_PRO_RE.search(cid):
        return OPENAI_GPT5_PRO_EFFORTS
    codex = _gpt5_codex_efforts(cid)
    if codex is not None:
        return codex
    versioned = _versioned_gpt5_efforts(cid)
    if versioned is not None:
        return versioned
    efforts = list(WIDELY_SUPPORTED_EFFORTS)
    if GPT5_FAMILY_RE.search(cid):
        efforts.insert(0, "minimal")
    if release_date >= OPENAI_NONE_EFFORT_RELEASE_DATE:
        efforts.insert(0, "none")
    if release_date >= OPENAI_XHIGH_EFFORT_RELEASE_DATE:
        efforts.append("xhigh")
    return efforts


def openai_compatible_reasoning_efforts(model_id: str):
    api_id = model_id.lower()
    chat = _gpt5_chat_efforts(api_id)
    if chat is not None:
        return chat
    if GPT5_PRO_RE.search(api_id):
        return OPENAI_GPT5_PRO_EFFORTS
    return _gpt5_codex_efforts(api_id) or _versioned_gpt5_efforts(api_id) or OPENAI_EFFORTS


def anthropic_opus_47_or_later(api_id: str) -> bool:
    m = _OPUS_47_RE.search(api_id)
    if not m:
        return False
    major = int(m.group(1) or m.group(3))
    minor = int(m.group(2) or m.group(4))
    return major > 4 or (major == 4 and minor >= 7)


def anthropic_adaptive_efforts(api_id: str):
    if anthropic_opus_47_or_later(api_id):
        return ["low", "medium", "high", "xhigh", "max"]
    if any(
        v in api_id
        for v in (
            "opus-4-6", "opus-4.6", "4-6-opus", "4.6-opus",
            "sonnet-4-6", "sonnet-4.6", "4-6-sonnet", "4.6-sonnet",
        )
    ):
        return ["low", "medium", "high", "max"]
    return None


def google_thinking_level_efforts(api_id: str):
    cid = api_id.lower()
    if "gemini-3" not in cid:
        return ["low", "high"]
    if "flash-image" in cid:
        return ["minimal", "high"]
    if "pro-image" in cid:
        return ["high"]
    if "flash" in cid:
        return ["minimal", "low", "medium", "high"]
    return ["low", "medium", "high"]


def google_thinking_budget_max(api_id: str) -> int:
    cid = api_id.lower()
    if "2.5" in cid and "pro" in cid and "flash" not in cid:
        return 32_768
    return 24_576


def google_thinking_variants(model: "CatalogModel") -> dict[str, dict]:
    cid = model.api_id.lower()
    if "2.5" in cid:
        return {
            "high": {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": 16000}},
            "max": {
                "thinkingConfig": {
                    "includeThoughts": True,
                    "thinkingBudget": google_thinking_budget_max(cid),
                }
            },
        }
    return {
        effort: {"thinkingConfig": {"includeThoughts": True, "thinkingLevel": effort}}
        for effort in google_thinking_level_efforts(cid)
    }


def _wrap_sap(variants_map: dict[str, dict]) -> dict[str, dict]:
    return {k: {"modelParams": v} for k, v in variants_map.items()}


def variants(model: "CatalogModel") -> dict[str, dict]:
    """Faithful port of transform.ts ``variants(model)`` (lines 640-1007)."""
    if not model.reasoning:
        return {}

    mid = model.id.lower()
    api_id = model.api_id
    adaptive_opus = anthropic_opus_47_or_later(api_id)
    adaptive_efforts = anthropic_adaptive_efforts(api_id)

    if any(
        s in mid
        for s in (
            "deepseek-chat", "deepseek-reasoner", "deepseek-r1", "deepseek-v3",
            "minimax", "glm", "kimi", "k2p", "qwen", "big-pickle",
        )
    ):
        return {}

    if "grok" in mid and "grok-3-mini" in mid:
        if model.npm == "@openrouter/ai-sdk-provider":
            return {"low": {"reasoning": {"effort": "low"}}, "high": {"reasoning": {"effort": "high"}}}
        return {"low": {"reasoningEffort": "low"}, "high": {"reasoningEffort": "high"}}
    if "grok" in mid:
        return {}

    npm = model.npm

    if npm == "@openrouter/ai-sdk-provider":
        if "gpt" not in mid and "gemini-3" not in mid and "claude" not in mid:
            return {}
        efforts = openai_compatible_reasoning_efforts(mid) if "gpt" in mid else OPENAI_EFFORTS
        return {effort: {"reasoning": {"effort": effort}} for effort in efforts}

    if npm == "ai-gateway-provider":
        if api_id.startswith("openai/"):
            efforts = openai_reasoning_efforts(api_id, model.release_date)
            return {effort: {"reasoningEffort": effort} for effort in efforts}
        return {effort: {"reasoningEffort": effort} for effort in WIDELY_SUPPORTED_EFFORTS}

    if npm == "@ai-sdk/gateway":
        if "anthropic" in model.id:
            if adaptive_efforts:
                return {
                    effort: {
                        "thinking": {
                            "type": "adaptive",
                            **({"display": "summarized"} if adaptive_opus else {}),
                        },
                        "effort": effort,
                    }
                    for effort in adaptive_efforts
                }
            return {
                "high": {"thinking": {"type": "enabled", "budgetTokens": 16000}},
                "max": {"thinking": {"type": "enabled", "budgetTokens": 31999}},
            }
        if "google" in model.id:
            if "2.5" in mid:
                return {
                    "high": {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": 16000}},
                    "max": {
                        "thinkingConfig": {
                            "includeThoughts": True,
                            "thinkingBudget": google_thinking_budget_max(mid),
                        }
                    },
                }
            return {
                effort: {"includeThoughts": True, "thinkingLevel": effort}
                for effort in ("low", "high")
            }
        return {
            effort: {"reasoningEffort": effort}
            for effort in openai_compatible_reasoning_efforts(api_id)
        }

    if npm == "@ai-sdk/github-copilot":
        if "gemini" in model.id:
            return {}
        if "claude" in model.id:
            return {effort: {"reasoningEffort": effort} for effort in WIDELY_SUPPORTED_EFFORTS}
        if "5.1-codex-max" in mid or "5.2" in mid or "5.3" in mid:
            copilot_efforts = [*WIDELY_SUPPORTED_EFFORTS, "xhigh"]
        else:
            copilot_efforts = list(WIDELY_SUPPORTED_EFFORTS)
            if "gpt-5" in mid and model.release_date >= "2025-12-04":
                copilot_efforts.append("xhigh")
        return {
            effort: {
                "reasoningEffort": effort,
                "reasoningSummary": "auto",
                "include": INCLUDE_ENCRYPTED_REASONING,
            }
            for effort in copilot_efforts
        }

    if npm in (
        "@ai-sdk/cerebras",
        "@ai-sdk/togetherai",
        "@ai-sdk/xai",
        "@ai-sdk/deepinfra",
        "venice-ai-sdk-provider",
        "@ai-sdk/openai-compatible",
    ):
        efforts = list(WIDELY_SUPPORTED_EFFORTS)
        if "deepseek-v4" in api_id.lower():
            efforts.append("max")
        return {effort: {"reasoningEffort": effort} for effort in efforts}

    if npm == "@ai-sdk/azure":
        if mid == "o1-mini":
            return {}
        return {
            effort: {
                "reasoningEffort": effort,
                "reasoningSummary": "auto",
                "include": INCLUDE_ENCRYPTED_REASONING,
            }
            for effort in openai_reasoning_efforts(mid, model.release_date)
        }

    if npm in ("@ai-sdk/amazon-bedrock/mantle", "@ai-sdk/openai"):
        return {
            effort: {
                "reasoningEffort": effort,
                "reasoningSummary": "auto",
                "include": INCLUDE_ENCRYPTED_REASONING,
            }
            for effort in openai_reasoning_efforts(api_id, model.release_date)
        }

    if npm in ("@ai-sdk/anthropic", "@ai-sdk/google-vertex/anthropic"):
        if adaptive_efforts:
            efforts = list(adaptive_efforts)
            if model.provider_id == "github-copilot":
                if "opus-4.7" in api_id:
                    efforts = ["medium"]
                efforts = [v for v in efforts if v not in ("max", "xhigh")]
            return {
                effort: {
                    "thinking": {
                        "type": "adaptive",
                        **({"display": "summarized"} if adaptive_opus else {}),
                    },
                    "effort": effort,
                }
                for effort in efforts
            }
        if any(v in api_id for v in ("opus-4-5", "opus-4.5")):
            return {effort: {"effort": effort} for effort in WIDELY_SUPPORTED_EFFORTS}
        out = model.limit.output
        return {
            "high": {"thinking": {"type": "enabled", "budgetTokens": min(16_000, out // 2 - 1)}},
            "max": {"thinking": {"type": "enabled", "budgetTokens": min(31_999, out - 1)}},
        }

    if npm == "@ai-sdk/amazon-bedrock":
        if adaptive_efforts:
            return {
                effort: {
                    "reasoningConfig": {
                        "type": "adaptive",
                        "maxReasoningEffort": effort,
                        **({"display": "summarized"} if adaptive_opus else {}),
                    }
                }
                for effort in adaptive_efforts
            }
        if "anthropic" in api_id:
            return {
                "high": {"reasoningConfig": {"type": "enabled", "budgetTokens": 16000}},
                "max": {"reasoningConfig": {"type": "enabled", "budgetTokens": 31999}},
            }
        return {
            effort: {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}}
            for effort in WIDELY_SUPPORTED_EFFORTS
        }

    if npm in ("@ai-sdk/google-vertex", "@ai-sdk/google"):
        return google_thinking_variants(model)

    if npm == "@ai-sdk/mistral":
        if not model.reasoning:
            return {}
        mistral_ids = (
            "mistral-small-2603",
            "mistral-small-latest",
            "mistral-medium-3.5",
            "mistral-medium-2604",
        )
        if not any(x in api_id.lower() for x in mistral_ids):
            return {}
        return {"high": {"reasoningEffort": "high"}}

    if npm == "@ai-sdk/cohere":
        return {}

    if npm == "@ai-sdk/groq":
        return {effort: {"reasoningEffort": effort} for effort in ("none", *WIDELY_SUPPORTED_EFFORTS)}

    if npm == "@ai-sdk/perplexity":
        return {}

    if npm == "@jerome-benoit/sap-ai-provider-v2":
        if "anthropic" in mid:
            if adaptive_efforts:
                return _wrap_sap(
                    {
                        effort: {
                            "thinking": {
                                "type": "adaptive",
                                **({"display": "summarized"} if adaptive_opus else {}),
                            },
                            "output_config": {"effort": effort},
                        }
                        for effort in adaptive_efforts
                    }
                )
            return _wrap_sap(
                {
                    "high": {"thinking": {"type": "enabled", "budget_tokens": 16000}},
                    "max": {"thinking": {"type": "enabled", "budget_tokens": 31999}},
                }
            )
        if "gemini" in mid and "2.5" in mid:
            return _wrap_sap(google_thinking_variants(model))
        if "gpt" in mid or re.search(r"\bo[1-9]", mid):
            efforts = openai_reasoning_efforts(mid, model.release_date)
            return _wrap_sap({effort: {"reasoning_effort": effort} for effort in efforts})
        return _wrap_sap({effort: {"reasoning_effort": effort} for effort in WIDELY_SUPPORTED_EFFORTS})

    return {}


def base_options(model: "CatalogModel") -> dict:
    """Port of transform.ts ``options()`` (lines 1009-1151): the provider-option
    defaults applied to every request *regardless of the selected variant*.

    Variants tune reasoning strength; ``base_options`` turns reasoning on (and
    sets provider quirks) so e.g. Google models stream thoughts and gpt-5
    defaults to medium effort even when the user picks no variant. The selected
    variant is deep-merged *over* this base (variant wins), mirroring opencode's
    ``mergeOptions(base, …, variant)``.

    Kept in opencode's native camelCase shape; ``to_openai_body`` filters to the
    chat-completions-portable subset, and the gemini/anthropic adapters read
    ``thinkingConfig`` / ``thinking`` directly. Session-scoped fields
    (``promptCacheKey``) are omitted — the catalog layer has no session id.
    """
    result: dict = {}
    npm = model.npm
    pid = model.provider_id
    api_id = model.api_id
    cid = api_id.lower()

    if npm in ("@openrouter/ai-sdk-provider", "@llmgateway/ai-sdk-provider"):
        result["usage"] = {"include": True}
        if "gemini-3" in cid:
            result["reasoning"] = {"effort": "high"}

    if pid == "baseten" or (pid == "opencode" and api_id in ("kimi-k2-thinking", "glm-4.6")):
        result["chat_template_args"] = {"enable_thinking": True}

    if any(x in pid for x in ("zai", "zhipuai")) and npm == "@ai-sdk/openai-compatible":
        result["thinking"] = {"type": "enabled", "clear_thinking": False}

    if npm in ("@ai-sdk/google", "@ai-sdk/google-vertex") and model.reasoning:
        tc = {"includeThoughts": True}
        if "gemini-3" in cid:
            tc["thinkingLevel"] = "high"
        result["thinkingConfig"] = tc

    if npm in ("@ai-sdk/anthropic", "@ai-sdk/google-vertex/anthropic") and (
        "k2p" in cid or "kimi-k2." in cid or "kimi-k2p" in cid
    ):
        result["thinking"] = {"type": "enabled", "budgetTokens": min(16_000, model.limit.output // 2 - 1)}

    if (
        pid == "alibaba-cn"
        and model.reasoning
        and npm == "@ai-sdk/openai-compatible"
        and "kimi-k2-thinking" not in cid
    ):
        result["enable_thinking"] = True

    # Azure gpt-5.5 returns early in opencode (skips the generic gpt-5 block).
    if npm == "@ai-sdk/azure" and "gpt-5.5" in cid:
        result["reasoningSummary"] = "auto"
        return result

    if "gpt-5" in cid and "gpt-5-chat" not in cid:
        if "gpt-5-pro" not in cid:
            result["reasoningEffort"] = "medium"
            result["reasoningSummary"] = "auto"
            if npm in ("@ai-sdk/openai", "@ai-sdk/amazon-bedrock/mantle"):
                result["include"] = INCLUDE_ENCRYPTED_REASONING
        if "gpt-5." in cid and "codex" not in cid and "-chat" not in cid and pid != "azure":
            result["textVerbosity"] = "low"
        if pid.startswith("opencode"):
            result["include"] = INCLUDE_ENCRYPTED_REASONING
            result["reasoningSummary"] = "auto"

    return result


def merge_options(base: dict, override: dict) -> dict:
    """Deep-merge ``override`` over ``base`` (override wins on scalars; dicts
    merge recursively). Mirrors opencode's ``mergeOptions``/``mergeDeep`` used to
    layer the selected variant on top of ``base_options``."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_options(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Execution helpers (OpenAI-compatible wire translation)
# --------------------------------------------------------------------------- #


def to_openai_body(variant_opts: dict) -> dict:
    """Translate a variant's provider-option dict into OpenAI-compatible
    chat-completions request-body fields.

    Handles the executable subset gorchestra speaks:
      * ``{"reasoningEffort": e}``       -> ``{"reasoning_effort": e}``
      * ``{"reasoning": {"effort": e}}`` -> kept verbatim (OpenRouter shape)
      * ``{"reasoning_effort": e}``      -> kept verbatim (SAP shape)
      * ``enable_thinking`` / ``thinking`` / ``chat_template_args`` -> kept
        verbatim (alibaba-cn / zai / baseten body fields from base_options)
    Responses-API/SDK-only fields (``reasoningSummary``/``include``/
    ``textVerbosity``/``store``/``usage``) and native non-OAI dicts
    (``thinkingConfig``/``reasoningConfig``/``modelParams``/``effort``/
    ``output_config``) are dropped — chat-completions rejects them.
    """
    if not variant_opts:
        return {}
    body: dict = {}
    if "reasoningEffort" in variant_opts:
        body["reasoning_effort"] = variant_opts["reasoningEffort"]
    if "reasoning_effort" in variant_opts:
        body["reasoning_effort"] = variant_opts["reasoning_effort"]
    if "reasoning" in variant_opts and isinstance(variant_opts["reasoning"], dict):
        body["reasoning"] = variant_opts["reasoning"]
    for k in ("enable_thinking", "chat_template_args"):
        if k in variant_opts:
            body[k] = variant_opts[k]
    # zai/zhipuai pass a `thinking` *enable flag* on the OAI-compatible wire.
    # Anthropic's native `thinking` (a token-budget dict) is consumed by the
    # anthropic adapter, never this path — drop it so it can't leak onto the wire.
    t = variant_opts.get("thinking")
    if isinstance(t, dict) and "budgetTokens" not in t and "budget_tokens" not in t:
        body["thinking"] = t
    return body


def variant_names(model: "CatalogModel") -> list[str]:
    """Ordered variant names (weakest→strongest) for a model, or ``[]``."""
    return list(model.variants.keys())


def default_variant(model: "CatalogModel"):
    """Best default variant: prefer ``medium``, else the middle of the list."""
    names = variant_names(model)
    if not names:
        return None
    if "medium" in names:
        return "medium"
    return names[len(names) // 2]
