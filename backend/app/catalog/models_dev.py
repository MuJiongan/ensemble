"""models.dev catalog fetch + cache + parse.

Python port of opencode's ``packages/core/src/models-dev.ts``.

Fetches ``https://models.dev/api.json`` (a ``Record<providerID, Provider>``),
caches it to disk with a 5-minute TTL, and parses it into enriched
``CatalogProvider`` / ``CatalogModel`` dataclasses. The enrichment mirrors
opencode's ``fromModelsDevModel`` (provider.ts) so the variant logic can key
off ``api_id`` / ``npm`` exactly the way opencode does.

The catalog is read both by the API process (which keeps the disk cache fresh
via a background refresh thread) and by the runner subprocess (which has no DB
access and relies on the on-disk cache; if it's missing it falls back to a
best-effort fetch, and degrades to an empty catalog offline).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

# Default fallback SDK package, matching opencode (provider.ts:1172). Unknown
# providers/models are treated as OpenAI-compatible, which drives the
# reasoning_effort variant shape — exactly the wire format gorchestra speaks.
DEFAULT_NPM = "@ai-sdk/openai-compatible"

_DISK_TTL_SECONDS = 5 * 60
_REFRESH_INTERVAL_SECONDS = 60 * 60
_FETCH_TIMEOUT = 10.0


def _source() -> str:
    return (os.getenv("MODELS_DEV_URL") or "https://models.dev").rstrip("/")


def _cache_dir() -> Path:
    base = os.getenv("GORCHESTRA_CACHE_DIR")
    if base:
        return Path(base)
    # Prefer XDG-ish cache; fall back to a temp dir so a locked-down home still works.
    home = Path.home()
    if home and home.exists():
        return home / ".cache" / "gorchestra"
    import tempfile

    return Path(tempfile.gettempdir()) / "gorchestra"


def _cache_file() -> Path:
    src = _source()
    name = "models.json" if src == "https://models.dev" else f"models-{_hash(src)}.json"
    return _cache_dir() / name


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Parsed catalog shapes (enriched, mirroring opencode's Provider.Model)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelLimit:
    context: int = 0
    output: int = 0
    input: Optional[int] = None


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str
    provider_id: str
    # ``api`` fields mirror opencode's ``model.api`` (provider.ts:1169-1173).
    api_id: str
    npm: str
    api_url: str
    reasoning: bool = False
    temperature: bool = False
    tool_call: bool = True
    attachment: bool = False
    interleaved: Any = None  # True | {"field": ...} | None
    release_date: str = ""
    family: Optional[str] = None
    status: str = "active"
    limit: ModelLimit = field(default_factory=ModelLimit)
    cost: Optional[dict] = None
    modalities: Optional[dict] = None
    # Filled in by the variants pass; raw provider-option dicts keyed by name.
    variants: dict[str, dict] = field(default_factory=dict)
    # Extra request-body options merged from experimental.modes, if any.
    options: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogProvider:
    id: str
    name: str
    npm: Optional[str]
    api: Optional[str]
    env: list[str]
    models: dict[str, CatalogModel]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _parse_limit(raw: dict | None) -> ModelLimit:
    raw = raw or {}
    return ModelLimit(
        context=int(raw.get("context") or 0),
        output=int(raw.get("output") or 0),
        input=raw.get("input"),
    )


def _camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _model_from_raw(provider: dict, raw: dict) -> CatalogModel:
    mid = raw.get("id") or ""
    mprov = raw.get("provider") or {}
    return CatalogModel(
        id=mid,
        name=raw.get("name") or mid,
        provider_id=provider.get("id") or "",
        api_id=mid,
        npm=mprov.get("npm") or provider.get("npm") or DEFAULT_NPM,
        api_url=mprov.get("api") or provider.get("api") or "",
        reasoning=bool(raw.get("reasoning")),
        temperature=bool(raw.get("temperature")),
        tool_call=bool(raw.get("tool_call", True)),
        attachment=bool(raw.get("attachment")),
        interleaved=raw.get("interleaved"),
        release_date=raw.get("release_date") or "",
        family=raw.get("family"),
        status=raw.get("status") or "active",
        limit=_parse_limit(raw.get("limit")),
        cost=raw.get("cost"),
        modalities=raw.get("modalities"),
    )


def _expand_experimental_modes(provider: dict, raw: dict, base: CatalogModel) -> dict[str, CatalogModel]:
    """Mirror opencode's experimental.modes expansion (provider.ts:1218-1236).

    Each mode produces a synthetic ``<id>-<mode>`` model with merged
    body/header overrides.
    """
    out: dict[str, CatalogModel] = {}
    modes = ((raw.get("experimental") or {}).get("modes")) or {}
    for mode, opts in modes.items():
        opts = opts or {}
        mid = f"{base.id}-{mode}"
        body = (opts.get("provider") or {}).get("body") or {}
        options = {_camel(k): v for k, v in body.items()} if body else dict(base.options)
        headers = (opts.get("provider") or {}).get("headers") or dict(base.headers)
        out[mid] = CatalogModel(
            id=mid,
            name=f"{base.name} {mode[:1].upper()}{mode[1:]}",
            provider_id=base.provider_id,
            api_id=base.api_id,
            npm=base.npm,
            api_url=base.api_url,
            reasoning=base.reasoning,
            temperature=base.temperature,
            tool_call=base.tool_call,
            attachment=base.attachment,
            interleaved=base.interleaved,
            release_date=base.release_date,
            family=base.family,
            status=base.status,
            limit=base.limit,
            cost=opts.get("cost") or base.cost,
            modalities=base.modalities,
            options=options,
            headers=headers,
        )
    return out


# OAuth-only providers that aren't in the models.dev catalog. ``codex`` routes
# through the ChatGPT Responses API (see app/auth/codex_api.py); we still list
# it here so the connect dialog + model picker can surface its curated models
# (npm ``@ai-sdk/openai`` so reasoning variants compute the same way).
_SYNTHETIC_RAW: dict[str, dict] = {
    "codex": {
        "id": "codex",
        "name": "ChatGPT (subscription)",
        "npm": "@ai-sdk/openai",
        "env": [],
        "models": {
            mid: {
                "id": mid,
                "name": mid,
                "reasoning": True,
                "tool_call": True,
                "release_date": "2025-11-13",
                "limit": {"context": 400000, "output": 128000},
            }
            for mid in (
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex",
                "gpt-5.3-codex-spark",
                "gpt-5.2",
            )
        },
    },
}


def _parse_catalog(raw: dict) -> dict[str, CatalogProvider]:
    # Variants imported lazily to avoid an import cycle at module load.
    from app.catalog.variants import variants as _variants

    merged = {**(raw or {})}
    for pid, praw in _SYNTHETIC_RAW.items():
        merged.setdefault(pid, praw)

    out: dict[str, CatalogProvider] = {}
    for pid, praw in merged.items():
        if not isinstance(praw, dict):
            continue
        praw = {**praw, "id": praw.get("id") or pid}
        models: dict[str, CatalogModel] = {}
        for mkey, mraw in (praw.get("models") or {}).items():
            if not isinstance(mraw, dict):
                continue
            mraw = {**mraw, "id": mraw.get("id") or mkey}
            base = _model_from_raw(praw, mraw)
            base = _with_variants(base, _variants)
            models[base.id] = base
            for sid, smodel in _expand_experimental_modes(praw, mraw, base).items():
                models[sid] = _with_variants(smodel, _variants)
        out[pid] = CatalogProvider(
            id=praw["id"],
            name=praw.get("name") or pid,
            npm=praw.get("npm"),
            api=praw.get("api"),
            env=list(praw.get("env") or []),
            models=models,
        )
    return out


def _with_variants(model: CatalogModel, variants_fn) -> CatalogModel:
    try:
        v = variants_fn(model)
    except Exception:
        v = {}
    object.__setattr__(model, "variants", v)
    return model


# --------------------------------------------------------------------------- #
# Fetch + cache
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_cache: dict[str, CatalogProvider] | None = None
_refresh_started = False


def _disk_fresh() -> bool:
    f = _cache_file()
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < _DISK_TTL_SECONDS


def _read_disk() -> dict | None:
    f = _cache_file()
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _fetch() -> dict | None:
    url = f"{_source()}/api.json"
    try:
        resp = httpx.get(
            url,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": "gorchestra"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _write_disk(data: dict) -> None:
    f = _cache_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))
    except OSError:
        pass


def _populate() -> dict[str, CatalogProvider]:
    """Disk-if-present, else fetch+write, else empty (offline). Mirrors
    opencode's ``populate`` (models-dev.ts:179-193)."""
    disk = _read_disk()
    if disk is not None:
        return _parse_catalog(disk)
    if os.getenv("GORCHESTRA_DISABLE_MODELS_FETCH"):
        return _parse_catalog({})
    fetched = _fetch()
    if fetched is not None:
        _write_disk(fetched)
        return _parse_catalog(fetched)
    # Offline with no cache: still expose synthetic (OAuth-only) providers.
    return _parse_catalog({})


def get_catalog(force: bool = False) -> dict[str, CatalogProvider]:
    """Return the parsed catalog, populating + caching in-process on first use."""
    global _cache
    with _lock:
        if _cache is None or force:
            _cache = _populate()
        return _cache


def refresh(force: bool = False) -> bool:
    """Re-fetch from the network if the disk cache is stale (or ``force``).
    Returns True if a fresh copy was written. Safe to call from a background
    thread; never raises."""
    global _cache
    if not force and _disk_fresh():
        return False
    fetched = _fetch()
    if fetched is None:
        return False
    _write_disk(fetched)
    with _lock:
        _cache = _parse_catalog(fetched)
    return True


def get_provider(provider_id: str) -> Optional[CatalogProvider]:
    return get_catalog().get(provider_id)


def get_model(provider_id: str, model_id: str) -> Optional[CatalogModel]:
    p = get_provider(provider_id)
    if p is None:
        return None
    return p.models.get(model_id)


def supports_image_input(model: Optional[CatalogModel]) -> bool:
    """Whether the model accepts image input, per the catalog's ``modalities``
    (falling back to the coarser ``attachment`` flag). Catalog misses count as
    capable — only a positive "text-only" signal should block an image, the
    provider enforces its own limits otherwise."""
    if model is None:
        return True
    mod_in = (model.modalities or {}).get("input")
    if isinstance(mod_in, list):
        return "image" in mod_in
    return model.attachment


def start_background_refresh() -> None:
    """Start a daemon thread that refreshes the catalog hourly. Idempotent;
    called once from the FastAPI lifespan."""
    global _refresh_started
    with _lock:
        if _refresh_started:
            return
        _refresh_started = True

    if os.getenv("GORCHESTRA_DISABLE_MODELS_FETCH"):
        return

    def _loop() -> None:
        # Prime once on startup, then space subsequent refreshes.
        try:
            refresh()
        except Exception:
            pass
        while True:
            time.sleep(_REFRESH_INTERVAL_SECONDS)
            try:
                refresh()
            except Exception:
                pass

    threading.Thread(target=_loop, name="models-dev-refresh", daemon=True).start()
