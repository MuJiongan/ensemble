"""Tests for the catalog variant port (app/catalog/variants.py).

These pin the faithful port of opencode's transform.ts ``variants()`` so the
reasoning-effort tiers don't silently drift.
"""
from __future__ import annotations

from app.catalog import variants as V
from app.catalog import models_dev as MD
from app.catalog.models_dev import CatalogModel, ModelLimit


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


def test_openai_gpt56_family_efforts():
    sol = _model(id="gpt-5.6-sol", api_id="gpt-5.6-sol", npm="@ai-sdk/openai")
    terra = _model(id="gpt-5.6-terra", api_id="gpt-5.6-terra", npm="@ai-sdk/openai")
    luna = _model(id="gpt-5.6-luna", api_id="gpt-5.6-luna", npm="@ai-sdk/openai")

    assert list(sol.variants) == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert list(terra.variants) == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert list(luna.variants) == ["low", "medium", "high", "xhigh", "max"]


def test_anthropic_adaptive_sonnet():
    m = _model(id="claude-sonnet-4-6", api_id="claude-sonnet-4-6", npm="@ai-sdk/anthropic")
    assert list(m.variants) == ["low", "medium", "high", "max"]
    assert m.variants["high"]["thinking"]["type"] == "adaptive"


def test_anthropic_sonnet5_uses_adaptive_thinking():
    m = _model(id="claude-sonnet-5", api_id="claude-sonnet-5", npm="@ai-sdk/anthropic")
    assert list(m.variants) == ["low", "medium", "high", "xhigh", "max"]
    assert m.variants["high"] == {
        "thinking": {"type": "adaptive"},
        "effort": "high",
    }


def test_anthropic_claude5_sonnet_name_uses_adaptive_thinking():
    m = _model(id="claude-5-sonnet", api_id="claude-5-sonnet", npm="@ai-sdk/anthropic")
    assert m.variants["max"]["thinking"]["type"] == "adaptive"


def test_anthropic_fable5_uses_adaptive_thinking():
    m = _model(id="claude-fable-5", api_id="claude-fable-5", npm="@ai-sdk/anthropic")
    assert list(m.variants) == ["low", "medium", "high", "xhigh", "max"]
    assert m.variants["xhigh"] == {
        "thinking": {"type": "adaptive"},
        "effort": "xhigh",
    }


def test_anthropic_mythos_adaptive_efforts():
    m5 = _model(id="claude-mythos-5", api_id="claude-mythos-5", npm="@ai-sdk/anthropic")
    preview = _model(
        id="claude-mythos-preview",
        api_id="claude-mythos-preview",
        npm="@ai-sdk/anthropic",
    )
    assert list(m5.variants) == ["low", "medium", "high", "xhigh", "max"]
    assert list(preview.variants) == ["low", "medium", "high", "max"]


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
    assert V.to_openai_body({"thinking": {"type": "adaptive"}, "effort": "high"}) == {}
    # zai/zhipuai's OpenAI-compatible thinking enable flag is intentionally kept
    assert V.to_openai_body({"thinking": {"type": "enabled", "clear_thinking": False}}) == {
        "thinking": {"type": "enabled", "clear_thinking": False}
    }


def test_default_variant_prefers_medium():
    m = _model(id="gpt-5.2", api_id="gpt-5.2", npm="@ai-sdk/openai")
    assert V.default_variant(m) == "medium"


def test_codex_gpt56_catalog_metadata():
    catalog = MD._parse_catalog({})
    models = catalog["codex"].models

    sol = models["gpt-5.6-sol"]
    terra = models["gpt-5.6-terra"]
    luna = models["gpt-5.6-luna"]

    assert (sol.name, terra.name, luna.name) == (
        "GPT-5.6 Sol",
        "GPT-5.6 Terra",
        "GPT-5.6 Luna",
    )
    assert all(
        model.limit == ModelLimit(context=372000, output=128000)
        for model in (sol, terra, luna)
    )
    assert all(model.responses_lite for model in (sol, terra, luna))
    assert not models["gpt-5.5"].responses_lite
    assert V.default_variant(sol) == "low"
    assert V.default_variant(terra) == "medium"
    assert V.default_variant(luna) == "medium"
