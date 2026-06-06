"""Connectable provider list + auth methods.

Derives the provider list the connect-provider dialog renders from the
models.dev catalog plus a small static auth-method registry. Mirrors the split
in opencode between catalog metadata (models-dev.ts) and per-provider auth
plugins (plugin/provider/*.ts) — here the only OAuth providers are the two
gorchestra already implements (codex, xai).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app import models
from app.catalog import models_dev as md
from app.catalog.variants import default_variant, variant_names

# Popular providers surfaced first in the picker (ported from opencode's
# use-providers.ts popularProviders), filtered to ids that exist in our catalog.
POPULAR = [
    "anthropic",
    "openai",
    "codex",  # ChatGPT subscription (synthetic provider)
    "google",
    "openrouter",
    "fireworks-ai",
    "github-copilot",
    "xai",
    "groq",
    "mistral",
]

# OAuth providers gorchestra implements (matches api/auth.py _PROVIDERS). Other
# providers authenticate with an API key.
OAUTH_PROVIDERS = {
    "codex": {"label": "Sign in with ChatGPT"},
    "xai": {"label": "Sign in with xAI"},
}

# Per-provider extra request headers applied on the OpenAI-compatible call
# (ported from opencode plugin/provider/*.ts). Only applied when we actually
# route to that provider's endpoint.
PROVIDER_REQUEST_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {
        "anthropic-beta": "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14",
    },
}

# Base URLs known to speak OpenAI-compatible chat-completions. When a provider
# isn't listed here we fall back to the catalog's ``api`` URL (best effort) or
# the user's configured ``llm_base_url`` / OpenRouter default.
PROVIDER_BASE_URL: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    # Native protocols (handled by app/llm adapters, not OpenAI-chat):
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "togetherai": "https://api.together.xyz/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "deepseek": "https://api.deepseek.com",
    "perplexity": "https://api.perplexity.ai",
    "fireworks-ai": "https://api.fireworks.ai/inference/v1",
}


def _model_info(m: md.CatalogModel) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "reasoning": m.reasoning,
        "variants": variant_names(m),
        "default_variant": default_variant(m),
        "release_date": m.release_date,
        "limit": {"context": m.limit.context, "output": m.limit.output},
        "cost": m.cost,
    }


def _auth_methods(provider: md.CatalogProvider) -> list[dict]:
    methods: list[dict] = []
    oauth = OAUTH_PROVIDERS.get(provider.id)
    if oauth:
        methods.append({"type": "oauth", "label": oauth["label"], "provider": provider.id})
    # API-key auth is offered whenever the catalog advertises env var(s) for it,
    # which is the same signal opencode uses (provider.env).
    if provider.env:
        methods.append({"type": "api", "label": "API Key"})
    if not methods:
        methods.append({"type": "api", "label": "API Key"})
    return methods


def base_url_for(provider_id: str) -> Optional[str]:
    direct = PROVIDER_BASE_URL.get(provider_id)
    if direct:
        return direct
    prov = md.get_provider(provider_id)
    if prov and prov.api:
        return prov.api
    return None


def _oauth_connected(db: Session, provider_id: str) -> bool:
    if provider_id not in OAUTH_PROVIDERS:
        return False
    return (
        db.query(models.Credential).filter_by(provider=provider_id).first() is not None
    )


def provider_info(db: Session, provider: md.CatalogProvider) -> dict:
    return {
        "id": provider.id,
        "name": provider.name,
        "popular": provider.id in POPULAR,
        "env": provider.env,
        "base_url": base_url_for(provider.id),
        "executable": base_url_for(provider.id) is not None or provider.id in OAUTH_PROVIDERS,
        "oauth_connected": _oauth_connected(db, provider.id),
        "auth": _auth_methods(provider),
        "models": [_model_info(m) for m in provider.models.values()],
    }


def list_providers(db: Session) -> list[dict]:
    cat = md.get_catalog()
    out = [provider_info(db, p) for p in cat.values()]
    # Popular first, then alphabetical by display name.
    out.sort(key=lambda p: (not p["popular"], p["name"].lower()))
    return out
