"""Catalog API — provider/model/variant data for the frontend.

Replaces the frontend's hardcoded provider presets + direct ``{base}/models``
calls. The provider list, per-model capability flags, and reasoning variants
all come from the backend's models.dev catalog (see app/catalog/).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.catalog import models_dev as md
from app.catalog import providers as prov
from app.catalog.variants import default_variant, variant_names

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/providers")
def get_providers(db: Session = Depends(get_db)) -> dict:
    providers = prov.list_providers(db)
    # Only synthetic (OAuth-only) providers means the real catalog hasn't loaded.
    real = [p for p in providers if p["id"] not in md._SYNTHETIC_RAW]
    return {"providers": providers, "stale": len(real) == 0}


@router.get("/providers/{provider_id}/models")
def get_provider_models(provider_id: str, db: Session = Depends(get_db)) -> dict:
    p = md.get_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"unknown provider '{provider_id}'")
    info = prov.provider_info(db, p)
    return {"models": info["models"]}


@router.get("/models/{provider_id}/{model_id:path}/variants")
def get_model_variants(provider_id: str, model_id: str) -> dict:
    m = md.get_model(provider_id, model_id)
    if m is None:
        raise HTTPException(
            status_code=404, detail=f"unknown model '{provider_id}/{model_id}'"
        )
    return {
        "variants": variant_names(m),
        "default": default_variant(m),
        "raw": m.variants,
    }


@router.post("/refresh")
def refresh_catalog() -> dict:
    updated = md.refresh(force=True)
    return {"ok": True, "updated": updated, "providers": len(md.get_catalog())}
