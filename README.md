# Ensemble

Local **Dynamic Agent-Team Orchestrator**. Describe a complex task or problem in chat, and an **orchestrator agent** (orchestrator LLM) dynamically recruits, programs, and links a custom team of **specialized agents** (Python nodes) on a visual collaboration board. Watch them collaborate in real time, inspect their communication flow, and directly edit any agent's custom code or chat with the orchestrator to refine the topology.

See `docs/PRD.md` for the full product specification.

## Status

Phases 0–7 are fully implemented, providing a robust local runtime for multi-agent collaboration and design:

- **Phase 0** Backend (FastAPI) + frontend (Vite/React/React Flow/Monaco) scaffold.
- **Phase 1** Multi-agent coordination runtime: tool registry (`shell`, `web_search`, `web_fetch`), `call_llm` over OpenRouter with agent-loop tool-calling, execution context (`ctx`) injected into specialized agents, subprocess-isolated runner with topological dependency resolution, null-propagation, and skip rule execution.
- **Phase 2** SQLite persistence + REST API for agent teams, specialized agent configurations, handoff channels, run executions, chat sessions, messages, and settings.
- **Phase 3** Collaboration board UI: top bar with team session selector, agent topology visualization, Monaco code editor per specialized agent, model + tool configurations, execution panel with input form, and per-agent log traces.
- **Phase 4** Live execution streaming over WebSocket: real-time agent status indicators on the canvas, live logs / LLM calls / tool invocations in the panel, and execution cancellation.
- **Phase 5** Orchestrator Agent: SSE chat session with extended thinking, topology-mutation tool surface (`add_node` / `add_edge` / `configure_node` / …), live collaboration board refresh, and `user_edited` preservation (hand-edited agent code is locked across orchestrator turns).
- **Phase 6** Orchestrator-driven executions: The orchestrator agent can trigger team runs (`run_workflow`) directly from chat, inspect collaboration results (`view_run`), and clear the collaboration board (`clean_canvas`) to transition between different stages of a multi-turn solve. All runs carry a frozen snapshot of the agent topology so past executions can be inspected or rerun independently.
- **Phase 7** External MCP tool integrations: users connect any number of external Model Context Protocol servers in Settings (local stdio commands or remote streamable-HTTP URLs); discovered tools are exposed to specialized agent nodes and surfaced to the orchestrator as a per-turn directory so it can write nodes that call them.
- **Team Forking & Branching**: Duplicate any live agent team or historical run snapshot into a brand-new editable project workspace (`POST /api/workflows/{wid}/fork` and `POST /api/runs/{rid}/fork`) to test variations and branch ideas.

## External MCP Tool Integrations

The settings panel includes an MCP section where you add servers in the opencode shape — a JSON map of `name → {type: "local", command: [...], environment: {...}} | {type: "remote", url, headers: {...}}`. Each row supports a per-server status probe (connected / needs_auth / failed + discovered-tool count), a per-tool disable toggle, and — for remote servers — an OAuth sign-in flow with discovery, dynamic client registration (RFC 7591), PKCE, and automatic token refresh handled by the MCP SDK. Settings autosave.

Servers that don't implement RFC 7591 (Slack's `https://mcp.slack.com/mcp`, others) require a pre-registered OAuth app: supply `oauth: {clientId, clientSecret}` (and `scope` if the provider needs it) on the server entry, and emdash skips DCR and uses your client for the authorize → token-exchange flow. The loopback callback is `http://127.0.0.1:19876/mcp/oauth/callback` by default; override per-server with `oauth.redirectUri` (any loopback URL) or `oauth.callbackPort` (port shorthand) when a provider needs a different URI — register whichever you choose as a redirect URI on the OAuth app.

At run start the runner subprocess connects to each enabled server, calls `tools/list`, and registers each tool into the same in-process registry the built-in tools live in. Node code reaches MCP tools two ways:

- **Direct** — `ctx.tools.<server>.<tool>(arg=...)` (dotted, keyword args), or the flat namespaced form `ctx.tools.<server>_<tool>(...)`.
- **Agentic** — name the flat form in `ctx.call_llm(tools=[...])` and let the node's inner LLM decide when to invoke it.

Tools return `{content, isError, structured?}`; failures are captured as structured tool-call errors and recorded in the node trace alongside built-in tool calls.

The orchestrator never executes MCP tools itself — its graph-shaping tool surface is unchanged. Instead, each orchestrator turn includes a system message listing every discovered tool with a one-line summary and the exact `<server>_<tool>` / `ctx.tools.<server>.<tool>` names to call it by. A new `get_mcp_tool_schema(server, tool)` helper fetches the untruncated JSON input schema before the orchestrator constructs nested args. Per-server `disabled_tools` opt-outs are applied before either the orchestrator listing or the node-runtime registry, so a globally disabled tool is invisible everywhere.

OAuth credentials live in a new `mcp_credentials` table on the backend; the FastAPI process owns the refresh loop and the child runner subprocess only ever receives a fresh bearer (injected into the `MCP_SERVERS` env var at spawn time).

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
    # ctx.call_llm(model="", prompt=..., tools=["shell", "web_search", "web_fetch", "<server>_<tool>"])
    #   Run an LLM-mediated sub-agent. The selected model (defaults to workspace default)
    #   uses the listed tools as-needed to satisfy the prompt. MCP tools from connected
    #   servers can be named here using their flat <server>_<tool> form.
    #
    # ctx.tools.shell(...) / ctx.tools.web_search(...) / ctx.tools.web_fetch(...)
    # ctx.tools.<server>.<tool>(arg=...) / ctx.tools.<server>_<tool>(...)
    #   Direct, deterministic tool calls bypassing LLM routing — both built-ins and
    #   MCP tools, reachable either dotted-by-server or as a flat namespaced attribute.
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
    main.py                # FastAPI server & per-request headers middleware (incl. MCP_SERVERS)
    db.py models.py schemas.py
    api/                   # Teams, agents, handoffs, executions, settings, orchestrator, auth, mcp
    auth/                  # Subscription-OAuth (Codex/ChatGPT, xAI) PKCE flows, loopback HTTP server,
                           #   per-provider token storage & refresh, Codex Responses-API translator,
                           #   plus mcp_oauth.py for remote MCP server OAuth (discovery, DCR, PKCE, refresh)
    runner/
      runner.py            # Parent: Spawns the isolated runner process, publishes events,
                           #   resolves fresh OAuth bearers for MCP servers at spawn time
      child.py             # Child: Subprocess boundary executing topological sort & skip rules,
                           #   connects to configured MCP servers at startup
      service.py           # Team run lifecycle, process spawning, and persistence
      ctx.py               # Injected agent context (call_llm, direct tools, logging,
                           #   dotted-by-server MCP tool access)
      tools.py             # Tool registry, LLM function schemas, and MCP namespace map
      mcp.py               # MCP client: stdio + streamable-HTTP transports, single shared
                           #   asyncio loop, tool discovery + schema normalization, OAuth token
                           #   storage bridge, runtime registry registration
      llm.py               # OpenAI-compatible LLM caller (provider-agnostic; agent loop with tool calling)
      events.py            # In-memory execution pub/sub feeding WebSockets
    orchestrator/
      agent/               # SSE orchestrator agent loop modules:
        __init__.py        #   Loop execution & history injection
        llm_stream.py      #   SSE token streaming & parsing
        persistence.py     #   Session messages mapping
        session.py         #   Orchestrator cancel-turn registry
      tools.py             # Orchestrator topology-mutation tools + get_mcp_tool_schema
      prompt.py            # Orchestrator system instructions, graph-state + MCP-directory injectors
    services/
      graph.py             # Shared team topology helpers (cascade deletes, forks)
  tests/
    test_runner.py
    test_orchestrator.py
    test_mcp.py
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
      Settings.tsx         # API keys, default models, MCP server editor (autosaves)
    mcpApi.ts              # MCP status probe, tool discovery, and OAuth login client
      ValueViewer.tsx JsonView.tsx Markdown.tsx
```
