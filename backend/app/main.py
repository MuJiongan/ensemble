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


# Map of inbound request header → process env var. Settings live client-side
# in localStorage; the frontend forwards them on every request and we apply
# them to the process env so existing call_llm / runner code keeps working.
_HEADER_TO_ENV = {
    "x-llm-api-key": "LLM_API_KEY",
    "x-llm-base-url": "LLM_BASE_URL",
    "x-parallel-key": "PARALLEL_API_KEY",
    "x-orchestrator-model": "DEFAULT_ORCHESTRATOR_MODEL",
    "x-node-model": "DEFAULT_NODE_MODEL",
}

# X-Llm-Provider-Id is handled separately from the additive map above. The
# frontend sends it on every LLM-bound request (a non-empty string for OAuth
# presets like ``codex``/``xai``; empty string when on an API-key preset).
# *Other* requests — notably the auth-status polls running concurrently with
# a streaming orchestrator response — must NOT touch the env, or they'll race
# with the orchestrator's in-flight rounds and drop us back to the API-key
# dispatch mid-turn.
_PROVIDER_ID_HEADER = "x-llm-provider-id"
_PROVIDER_ID_ENV = "LLM_PROVIDER_ID"

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
    # Provider-id semantics: header *present* (any value) is authoritative;
    # header *absent* means "this request isn't an LLM call — leave env
    # alone." That keeps concurrent auth-status polls from clearing the
    # provider mid-turn for a streaming orchestrator response.
    provider_id = request.headers.get(_PROVIDER_ID_HEADER)
    if provider_id is not None:
        if provider_id:
            os.environ[_PROVIDER_ID_ENV] = provider_id
        else:
            os.environ.pop(_PROVIDER_ID_ENV, None)
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
