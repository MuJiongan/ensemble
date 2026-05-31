# Ensemble

Local **Dynamic Agent-Team Orchestrator**. Describe a complex task or problem in chat, and an **orchestrator agent** (orchestrator LLM) dynamically recruits, programs, and links a custom team of **specialized agents** (Python nodes) on a visual collaboration board. Watch them collaborate in real time, inspect their communication flow, and directly edit any agent's custom code or chat with the orchestrator to refine the topology.

See `docs/PRD.md` for the full product specification.

## Status

Phases 0–6 are fully implemented, providing a robust local runtime for multi-agent collaboration and design:

- **Phase 0** Backend (FastAPI) + frontend (Vite/React/React Flow/Monaco) scaffold.
- **Phase 1** Multi-agent coordination runtime: tool registry (`shell`, `web_search`, `web_fetch`), `call_llm` over OpenRouter with agent-loop tool-calling, execution context (`ctx`) injected into specialized agents, subprocess-isolated runner with topological dependency resolution, null-propagation, and skip rule execution.
- **Phase 2** SQLite persistence + REST API for agent teams, specialized agent configurations, handoff channels, run executions, chat sessions, messages, and settings.
- **Phase 3** Collaboration board UI: top bar with team session selector, agent topology visualization, Monaco code editor per specialized agent, model + tool configurations, execution panel with input form, and per-agent log traces.
- **Phase 4** Live execution streaming over WebSocket: real-time agent status indicators on the canvas, live logs / LLM calls / tool invocations in the panel, and execution cancellation.
- **Phase 5** Orchestrator Agent: SSE chat session with extended thinking, topology-mutation tool surface (`add_node` / `add_edge` / `configure_node` / …), live collaboration board refresh, and `user_edited` preservation (hand-edited agent code is locked across orchestrator turns).
- **Phase 6** Orchestrator-driven executions: The orchestrator agent can trigger team runs (`run_workflow`) directly from chat, inspect collaboration results (`view_run`), and clear the collaboration board (`clean_canvas`) to transition between different stages of a multi-turn solve. All runs carry a frozen snapshot of the agent topology so past executions can be inspected or rerun independently.
- **Team Forking & Branching**: Duplicate any live agent team or historical run snapshot into a brand-new editable project workspace (`POST /api/workflows/{wid}/fork` and `POST /api/runs/{rid}/fork`) to test variations and branch ideas.

## Planned: User-Configured MCP Tool Integrations

The next product addition is support for user-configured external MCP servers whose discovered tools can be used by specialized agent nodes. MCP tools are not exposed as direct Orchestrator Agent tools. Instead, each chat/session will have an allowlist UI that determines which MCP tools are visible to the orchestrator as configurable node capabilities and which tools are passed into node execution.

Planned behavior:

- Users add and test MCP server connections locally, then select allowed tools per session.
- The orchestrator can inspect allowed MCP tool names/descriptions/schemas and configure specialized nodes to use them, but cannot call those external tools itself.
- Specialized nodes can use allowed MCP tools directly through `ctx.tools` or agentically through `ctx.call_llm(..., tools=[...])`.
- Tool calls execute without per-call approval once the tool is allowed for the session; audit logs and run traces must still record MCP tool usage.
- MCP tools should be namespaced to avoid collisions with built-in tools such as `shell`, `web_search`, and `web_fetch`.

Implementation guidance follows Codex-style MCP patterns:

- Treat MCP availability as a lifecycle, not a boolean: `configured → auth_known → initialized → tools_discovered → allowed_in_session`.
- For HTTP MCP servers, support OAuth 2.1 + PKCE, OAuth discovery, RFC 8707 resource parameters, and bearer tokens sourced from environment variables rather than inline secrets.
- Verify a connection by completing the MCP handshake and `tools/list`; do not expose a server as usable from config presence alone.
- Include startup and tool-call timeouts, enabled/disabled tool filtering, hot reload after config changes, token refresh, and redacted logging.
- Provide a lightweight status endpoint that reports tools and auth status without requiring slow resource inventory.

## Run it

Requires Python 3.11+ and Node 18+.

```bash
make install       # Install Python dependencies and Node modules
make test          # Run the backend pytest suite
make backend       # Start the API and orchestration server on http://localhost:8000
make frontend      # Start the Vite development UI on http://localhost:5173
```

Open http://localhost:5173 and configure an **LLM provider preset** in **settings**:

- **API-key presets** — OpenRouter (default), OpenAI, Groq, Together, DeepSeek, Cerebras, Fireworks, xAI, Mistral, or **Custom** for any other OpenAI-compatible `{base}/chat/completions` endpoint. Each preset stores its own key; switching providers swaps the active key, default orchestrator model, and default node model.
- **Subscription-OAuth presets** — **ChatGPT (subscription)** (PKCE flow against `auth.openai.com`, routes calls through `chatgpt.com/backend-api/codex/responses` against your ChatGPT Pro/Plus subscription; GPT-5.x models only) and **xAI (sign in)** (PKCE flow against `auth.x.ai`). These replace the key field with a Sign in / Sign out widget that drives an OAuth popup; tokens live server-side in a `Credential` table, refreshed automatically on expiry. Both flows pin loopback callback ports (`1455` and `127.0.0.1:56121`) required by upstream's registered redirect URIs.

Optionally add a parallel.ai API key for the `web_search` / `web_fetch` tools. API keys are kept in browser `localStorage` and ride as request headers — the backend never persists them.

### Application Layout & Interaction

The interface uses a split-screen layout designed for co-authoring:
- **Collaboration Board (Left, 2/5 width)**: Visualizes the collaborating agent topology. To maintain correct data contract invariants, topology mutations (recruiting new agents, deleting agents, establishing communication handoffs) are handled by the **Orchestrator Agent** via chat. The board is read-only and non-draggable for the user (except clicking an agent to inspect details).
- **Control Panel (Right, 3/5 width)**: A tabbed workspace panel to toggle between:
  - **chat**: Converse with the Orchestrator Agent. The orchestrator reasons out loud (with collapsible thinking logs), plans team structures, and writes specialized Python agents. When a run completes, the orchestrator extracts key results and presents them in chat.
  - **workspace**: Directly inspect and refine the team. Select any agent to edit its Python code (Monaco editor), adjust its individual LLM model, review its port shapes, and run manual test inputs with live tracing.

When viewing a historical run, the UI swaps into **Snapshot Mode** with a visual banner at the bottom and an action bar at the top, allowing you to fork the historical team snapshot into a new session or return to the live workspace.

## Specialized Agent Code Contract

Each specialized agent runs a custom Python block with access to a rich execution context:

```python
def run(inputs, ctx):
    # ctx.call_llm(model="", prompt=..., tools=["shell", "web_search", "web_fetch"])
    #   Run an LLM-mediated sub-agent. The selected model (defaults to workspace default) 
    #   uses the listed tools as-needed to satisfy the prompt.
    #   Planned: allowed MCP tools can also be supplied here for agentic use.
    #
    # ctx.tools.shell(...) / ctx.tools.web_search(...) / ctx.tools.web_fetch(...)
    #   Execute direct, deterministic tool calls bypassing LLM routing.
    #   Planned: allowed MCP tools can also be called directly from node code.
    #
    # ctx.log("...")                     — Appends a line to the agent's live run log
    # ctx.workdir                        — A temporary scratch directory unique to this execution
    return {"output_name": value_or_None}
```

* **Branching & Fallbacks**: Returning `None` for a handoff output halts that execution path. Downstream agents that mark this handoff input as *required* will be skipped, propagating the skip through the topology. Agents with *optional* inputs will execute with the fallback values.

## Layout

```
backend/
  app/
    main.py                # FastAPI server & per-request headers middleware
    db.py models.py schemas.py
    api/                   # Teams, agents, handoffs, executions, settings, orchestrator, auth
    auth/                  # Subscription-OAuth (Codex/ChatGPT, xAI) PKCE flows, loopback HTTP server,
                           #   per-provider token storage & refresh, Codex Responses-API translator
    runner/
      runner.py            # Parent: Spawns the isolated runner process, publishes events
      child.py             # Child: Subprocess boundary executing topological sort & skip rules
      service.py           # Team run lifecycle, process spawning, and persistence
      ctx.py               # Injected agent context (call_llm, direct tools, logging)
      tools.py             # Tool registry & LLM function schemas
      llm.py               # OpenAI-compatible LLM caller (provider-agnostic; agent loop with tool calling)
      events.py            # In-memory execution pub/sub feeding WebSockets
    orchestrator/
      agent/               # SSE orchestrator agent loop modules:
        __init__.py        #   Loop execution & history injection
        llm_stream.py      #   SSE token streaming & parsing
        persistence.py     #   Session messages mapping
        session.py         #   Orchestrator cancel-turn registry
      tools.py             # Orchestrator topology-mutation tools
      prompt.py            # Orchestrator system instructions & state injectors
    services/
      graph.py             # Shared team topology helpers (cascade deletes, forks)
  tests/
    test_runner.py
    test_orchestrator.py
frontend/
  src/
    App.tsx
    appHelpers.ts          # Topology mutation actions & chat helpers
    api.ts types.ts localSettings.ts
    notify.ts              # Browser notifications on run completion
    llmProviders.ts        # LLM provider preset registry (api-key + OAuth presets, Custom)
    llmModels.ts           # Model autocomplete caching (per base URL)
    auth.ts                # OAuth popup driver for subscription-login providers
    orchestratorStream.ts  # Orchestrator SSE event reducer
    runWebSocket.ts        # Execution WebSocket status receiver
    components/
      TopBar.tsx           # Team sessions dropdown, status badges, configuration
      ChatPanel.tsx        # Orchestrator chat (SSE, reasoning renderer)
      Canvas.tsx           # Visual collaboration board (read-only topology)
      Hero.tsx             # Welcome empty-state panel
      NodePanel.tsx        # Monaco editor, port configurations, and run trace
      NodeIOBlock.tsx      # Handoff port layout renderer
      NodeTraceCard.tsx    # Live trace cards with real-time logs and LLM calls
      RunPanel.tsx         # User input console, execution controls, historical run list
      SnapshotBanner.tsx   # Visual indicator for historical snapshots
      SnapshotRunPanel.tsx # Pinned controls for snapshot historical views
      ModelInput.tsx       # Autocomplete-equipped model picker
      Settings.tsx         # Storage of API keys & default models
      ValueViewer.tsx JsonView.tsx Markdown.tsx
```
