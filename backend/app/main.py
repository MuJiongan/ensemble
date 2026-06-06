from __future__ import annotations
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db, SessionLocal
from app.api import workflows, nodes, edges, runs, orchestrator
from app.api import settings as settings_api
from app.api import auth as auth_api
from app.api import mcp as mcp_api
from app.api import catalog as catalog_api
from app.catalog import models_dev


# Settings live client-side in localStorage; the frontend forwards them on every
# LLM-bound request and we apply them to the process env so call_llm / the runner
# / the orchestrator keep working.
#
# Two independent credential groups ride on every request:
#   * X-Llm-* / X-Orchestrator-*  → LLM_* / DEFAULT_ORCHESTRATOR_*  — used by the
#     orchestrator's own in-process model calls.
#   * X-Node-*                    → NODE_* / DEFAULT_NODE_*          — used by the
#     runner for workflow node calls (incl. runs the orchestrator spawns, which
#     must use the *node's* provider/model, not the orchestrator's).
#
# Semantics: header *present* → set (non-empty) or clear (empty). Header *absent*
# → leave the env alone. Auth-status polls (raw fetch, no settings headers) thus
# never clobber a streaming orchestrator turn's env.
_LLM_HEADER_TO_ENV = {
    "x-llm-provider-id": "LLM_PROVIDER_ID",
    "x-llm-api-key": "LLM_API_KEY",
    "x-llm-base-url": "LLM_BASE_URL",
    "x-orchestrator-model": "DEFAULT_ORCHESTRATOR_MODEL",
    "x-orchestrator-variant": "DEFAULT_ORCHESTRATOR_VARIANT",
    "x-node-provider-id": "NODE_PROVIDER_ID",
    "x-node-api-key": "NODE_API_KEY",
    "x-node-base-url": "NODE_BASE_URL",
    "x-node-model": "DEFAULT_NODE_MODEL",
    "x-node-variant": "DEFAULT_NODE_VARIANT",
}

# Parallel.ai key: additive (only set when present + non-empty).
_HEADER_TO_ENV = {
    "x-parallel-key": "PARALLEL_API_KEY",
}

# MCP server config travels as a single JSON header. Unlike the additive map
# above, an explicitly-present-but-empty value must *clear* the env — otherwise
# a user who removes all their MCP servers would keep spawning them from stale
# process env until the next restart. The config string is identical on every
# request, so set/clear here can't race a concurrent poll.
_MCP_SERVERS_HEADER = "x-mcp-servers"
_MCP_SERVERS_ENV = "MCP_SERVERS"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        # Backwards compat: if any settings happen to be in the DB from a
        # prior version, hydrate the env once at boot. The new frontend
        # source-of-truth is localStorage (sent as headers per-request).
        settings_api.apply_settings_to_env(db)
    finally:
        db.close()
    # Prime + keep the models.dev catalog fresh in the background.
    models_dev.start_background_refresh()
    yield


app = FastAPI(title="Workflow Builder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def apply_settings_headers(request: Request, call_next):
    """Copy localStorage-sourced settings headers into process env for the
    duration of this request. Single-user local app — no concurrent-user
    cross-contamination concerns."""
    # Per-request LLM credentials: present → set/clear; absent → leave alone
    # (so concurrent auth-status polls don't clobber a streaming turn's env).
    for header, env in _LLM_HEADER_TO_ENV.items():
        value = request.headers.get(header)
        if value is not None:
            if value:
                os.environ[env] = value
            else:
                os.environ.pop(env, None)
    for header, env in _HEADER_TO_ENV.items():
        value = request.headers.get(header)
        if value:
            os.environ[env] = value
    mcp_servers = request.headers.get(_MCP_SERVERS_HEADER)
    if mcp_servers is not None:
        if mcp_servers:
            os.environ[_MCP_SERVERS_ENV] = mcp_servers
        else:
            os.environ.pop(_MCP_SERVERS_ENV, None)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


app.include_router(workflows.router)
app.include_router(nodes.router)
app.include_router(edges.router)
app.include_router(runs.router)
app.include_router(orchestrator.router)
app.include_router(settings_api.router)
app.include_router(auth_api.router)
app.include_router(mcp_api.router)
app.include_router(catalog_api.router)
