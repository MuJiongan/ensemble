"""Tests for the catalog variant port (app/catalog/variants.py).

These pin the faithful port of opencode's transform.ts ``variants()`` so the
reasoning-effort tiers don't silently drift.
"""
from __future__ import annotations

from app.catalog.models_dev import CatalogModel, ModelLimit
from app.catalog import variants as V


def _model(**kw) -> CatalogModel:
    base = dict(
        id=kw.get("id", "m"),
        name=kw.get("name", "m"),
        provider_id=kw.get("provider_id", "p"),
        api_id=kw.get("api_id", kw.get("id", "m")),
        npm=kw.get("npm", "@ai-sdk/openai-compatible"),
        api_url="",
        reasoning=kw.get("reasoning", True),
        release_date=kw.get("release_date", "2025-01-01"),
        limit=ModelLimit(context=200000, output=kw.get("output", 64000)),
    )
    m = CatalogModel(**base)
    object.__setattr__(m, "variants", V.variants(m))
    return m


def test_non_reasoning_has_no_variants():
    m = _model(reasoning=False)
    assert m.variants == {}


def test_openai_gpt5_pro_only_high():
    m = _model(id="gpt-5-pro", api_id="gpt-5-pro", npm="@ai-sdk/openai")
    assert list(m.variants) == ["high"]


def test_openai_gpt5_versioned_efforts():
    m = _model(id="gpt-5.2", api_id="gpt-5.2", npm="@ai-sdk/openai")
    assert list(m.variants) == ["none", "low", "medium", "high", "xhigh"]


def test_anthropic_adaptive_sonnet():
    m = _model(id="claude-sonnet-4-6", api_id="claude-sonnet-4-6", npm="@ai-sdk/anthropic")
    assert list(m.variants) == ["low", "medium", "high", "max"]
    assert m.variants["high"]["thinking"]["type"] == "adaptive"


def test_anthropic_opus45_simple_effort():
    m = _model(id="claude-opus-4-5", api_id="claude-opus-4-5", npm="@ai-sdk/anthropic")
    assert list(m.variants) == ["low", "medium", "high"]
    assert m.variants["high"] == {"effort": "high"}


def test_deepseek_suppressed():
    m = _model(id="deepseek-r1", api_id="deepseek-r1", npm="@ai-sdk/openai-compatible")
    assert m.variants == {}


def test_openrouter_non_supported_suppressed():
    m = _model(id="z-ai/glm-4.6", api_id="z-ai/glm-4.6", npm="@openrouter/ai-sdk-provider")
    assert m.variants == {}


def test_openrouter_gpt_effort_shape():
    m = _model(id="openai/gpt-5", api_id="openai/gpt-5", npm="@openrouter/ai-sdk-provider")
    assert m.variants["medium"] == {"reasoning": {"effort": "medium"}}


def test_to_openai_body_translation():
    assert V.to_openai_body({"reasoningEffort": "high"}) == {"reasoning_effort": "high"}
    assert V.to_openai_body({"reasoning": {"effort": "low"}}) == {"reasoning": {"effort": "low"}}
    # native thinking dicts are dropped on the OAI-compatible path
    assert V.to_openai_body({"thinking": {"type": "enabled", "budgetTokens": 16000}}) == {}


def test_default_variant_prefers_medium():
    m = _model(id="gpt-5.2", api_id="gpt-5.2", npm="@ai-sdk/openai")
    assert V.default_variant(m) == "medium"
