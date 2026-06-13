# Ensemble

A local, single-user app where you describe a goal in chat and an **orchestrator agent** assembles a team to solve it — a directed graph of small **Python agent nodes**, each with its own model and tools. You watch the team take shape on a live canvas, open any node to read or rewrite its code, run the graph on real inputs, and keep chatting to refine it.

Everything runs on your machine against your own LLM keys. Nothing is hosted, and the backend never persists your credentials.

## How it works

The orchestrator owns the graph. You talk to it; it recruits nodes, wires their inputs and outputs, writes their Python, and can run the team for you. The canvas is a read-only view of what it builds — you can't drag nodes or draw edges by hand (that is the orchestrator's job), but you can open any node to read or rewrite its code, and you can run the graph yourself from the run console.

How the on-screen vocabulary maps to the code:

| In the UI | In the code | What it is |
| --- | --- | --- |
| Orchestrator | orchestrator agent | The chat-driven LLM that plans the team and writes node code |
| Project / team | `Workflow` | A DAG of nodes; also the unit of a chat session |
| Agent | `Node` | A Python `run(inputs, ctx)` function with its own model + tools |
| Handoff | `Edge` | Wires one node's named output to another node's named input |
| Run | `Run` | One end-to-end execution; freezes a snapshot of the graph |
| Agent trace | `NodeRun` | The inputs/outputs/logs/LLM + tool calls for one node in a run |
| Canvas | Canvas | The read-only visual board; topology is orchestrator-owned |

The interface is a two-pane workspace: the canvas takes the left ~2/5, and the right ~3/5 toggles between **chat** (talk to the orchestrator) and **workspace** (the run console, or a node's code editor and trace). There is no hand-drawn graph editor — recruiting, deleting, renaming, and wiring agents all happen through the orchestrator.

## Quick start

Requires Python 3.11+ and a recent Node + npm.

```bash
make install     # create backend/.venv, install Python deps, npm install in frontend/
make backend     # FastAPI + orchestration server on http://localhost:8000
make frontend    # Vite dev UI on http://localhost:5173
make dev         # run both together
make test        # backend pytest suite
```

The Vite dev server proxies `/api` (including WebSockets) to the backend, so just open **http://localhost:5173**. Then open **Settings** and connect at least one LLM provider — there is no built-in default model, so a run or chat turn will fail with a "configure a model in Settings" message until you pick one. See [Configuring providers & models](#configuring-providers--models) for the details.

## Architecture

Three moving parts: the **orchestrator** that designs the team, the **runtime** that executes it, and the **LLM transport** they both talk through.

### Orchestrator agent

The orchestrator is the only thing that shapes the team, and it works purely by *designing* — never by *doing*. From your chat it decides which agents to recruit, what each one's inputs and outputs are, and how they're wired together, and it writes the Python that each agent runs. It can also start a run and inspect the results.

What it can't do is execute. The orchestrator never runs a node's tools or calls an MCP server itself — all real work happens in the runtime. It can't hand-edit the graph either: every structural change goes through its tools, so the canvas always reflects exactly what it built. And when it starts a run, it gets back only status and cost, then pulls the specific outputs it needs on demand — to summarize them for you, or to debug a failure — rather than having a run's full output fed back into it.

### Node runtime

The runtime is the engine that actually executes a graph. A run takes a frozen snapshot of the graph and your inputs, then works through the agents in dependency order — independent agents run concurrently, and each one starts as soon as every agent feeding into it has finished, passing its data along the edges.

Two properties matter most:

- **Branching lives in the data.** An agent can return `None` on an output to switch off everything downstream of it: an agent whose *required* input arrives as `None` is skipped and emits `None` in turn, so an entire branch can fall away (an *optional* input lets the agent run anyway and supply a fallback). That's how conditional paths work — there are no loop edges, so any repetition lives inside a single agent's code.
- **A misbehaving agent can't take down the app.** Each run executes in its own isolated subprocess, so an agent that errors, hangs, or runs a broken script fails only its own run — the server stays up — and you can cancel a run at any time. Progress streams to the UI live, so you watch each agent move through idle → running → success, error, or skipped.

### LLM transport

The LLM layer (`backend/app/llm/`) is **multi-protocol**, not a single chat-completions client. A router picks a native adapter per model based on the catalog's SDK package:

| Adapter | Used for |
| --- | --- |
| `anthropic_messages` | Anthropic / Claude models (native Messages API, with prompt caching) |
| `gemini` | Google Gemini models (google-genai SDK) |
| `openai_responses` | Native OpenAI models (the `/v1/responses` Responses API) and the `codex` subscription path |
| `openai_chat` | Everything else — the fallback, defaulting to OpenRouter |

Each adapter lowers messages to the provider's native shape, streams via that provider's official SDK, and re-emits a single uniform event contract (`text` / `thinking` / `tool_args` / `done`) so the agent loops stay protocol-agnostic. Per-model reasoning effort, context limits, and cost estimates all come from the models.dev catalog.

## Node code contract

Each agent is a Python block exposing `run(inputs, ctx)`:

```python
def run(inputs, ctx):
    # ctx.call_llm(model=None, prompt=..., tools=["web_search", "<server>_<tool>"]) -> dict
    #   Run an LLM-mediated sub-agent. Defaults to the node's configured model
    #   (raises if neither set). With tools, runs an agent loop: the model calls
    #   tools, results feed back, until it returns a final answer.
    #   Returns {content, messages, tool_calls_made, usage, cost}.
    #
    # ctx.tools.shell(...) / read_file / write_file / edit_file / web_search / web_fetch
    # ctx.tools.<server>.<tool>(arg=...)  or  ctx.tools.<server>_<tool>(...)
    #   Direct, deterministic tool calls that bypass LLM routing — built-ins and MCP tools.
    #
    # ctx.log("...")    Append a line to this agent's live run log.
    # ctx.workdir       A Path to a scratch directory unique to this run.
    return {"output_name": value_or_None}
```

The six built-in tools available to every node are `shell`, `read_file`, `write_file`, `edit_file`, `web_search`, and `web_fetch`. `read_file` also returns images (PNG/JPEG/GIF/WebP) as attachments for vision-capable models; `web_search` and `web_fetch` are backed by parallel.ai. Tools can be invoked either *agentically* — named in `ctx.call_llm(tools=[...])` so the node's own model decides when to call them — or *directly* via `ctx.tools.<name>(...)`, which runs them deterministically with no model in the loop (see [Design decisions](#design-decisions)).

## Design decisions

A few choices that shape how the system behaves in use:

- **Nodes can trigger tools directly — but agentic is the default.** A node author names a tool in `ctx.call_llm(tools=[...])` and lets the node's own model decide when to call it (the default), *or* calls `ctx.tools.<name>(...)` to fire it deterministically with no model in the loop. Direct calls are the deliberate exception — for steps where you want a guaranteed, un-routed action rather than the model's judgement — and the same dual surface covers both built-in and MCP tools. So a node isn't forced to launder every action through an LLM: it can reason when reasoning helps and just *do the thing* when it doesn't.
- **The orchestrator plans; it doesn't run the work itself.** It shapes the graph and writes node code, but it can't execute the workflow's tools directly — `run_workflow` starts a run and hands back only `{run_id, status, total_cost}`, while the live outputs stream to *you* in the run console. Nothing auto-dumps into the model's context: when the orchestrator needs a result — to summarize it for you, or to debug a failure — it pulls just the node outputs it asks for via `view_run`, the same pull-not-push discipline it uses for the graph. So the build conversation stays lean instead of bloating with every run's full output.
- **The graph is pulled, not pushed.** The orchestrator's prompt never carries the current topology or node code; it calls `view_graph()` / `view_node_details()` on demand. Structure and code are also split across `add_node` (recruit a stub) and `configure_node` (inject the Python) so no single tool call carries both. Both keep context small and turns fast.
- **Branching is data, not control flow.** There are no cyclic or conditional edges — a node returns `None` on an output to skip a downstream path (the [skip rule](#node-runtime)), and any looping lives inside one node's Python. The graph stays a DAG you can read at a glance.
- **The orchestrator and node models are independent.** The chat agent and the agents it builds run on separate provider/key/reasoning settings, so you can pair an expensive planner with cheap workers (or the reverse) without coupling the two.
- **A runaway loop is a cancel button, not a turn cap.** Neither the orchestrator turn nor a node's `call_llm` loop has an iteration limit; each runs until the model stops calling tools, and is stopped — when it needs to be — by cancel (a `SIGTERM` to the run subprocess).

## External MCP tool integrations

**MCP** (the [Model Context Protocol](https://modelcontextprotocol.io)) lets your agents reach external tools — local programs, SaaS APIs, browsers — beyond the six built-ins. Settings includes an MCP section where you add servers as a JSON map of `name → {type: "local", command: [...], environment: {...}} | {type: "remote", url, headers: {...}}`. The config lives in your browser and is forwarded as the `X-Mcp-Servers` header. Each row has a status probe (`connected` / `needs_auth` / `failed`, plus the discovered tool count), a "view tools" popout that lists each tool's full input schema with per-tool disable toggles, and — for remote servers — an OAuth sign-in flow.

At the start of every run the child subprocess connects to each enabled server, calls `tools/list`, and registers the discovered tools into the same in-process registry the built-ins live in. Node code reaches them two ways:

- **Direct** — `ctx.tools.<server>.<tool>(arg=...)` (dotted) or the flat `ctx.tools.<server>_<tool>(...)`.
- **Agentic** — name the flat `<server>_<tool>` form in `ctx.call_llm(tools=[...])`.

The orchestrator never executes MCP tools itself. Instead, each orchestrator turn receives a system message listing every discovered tool with a one-line summary and the exact names to call it by; `get_mcp_tool_schema(server, tool)` fetches a tool's untruncated input schema on demand. Per-server `disabled_tools` opt-outs are applied before both the orchestrator listing and the runtime registry, so a disabled tool is invisible everywhere.

**OAuth.** Remote servers are OAuth-capable by default. The MCP SDK handles metadata discovery, dynamic client registration (RFC 7591), PKCE, and token refresh; tokens are persisted in the `mcp_credentials` table and never returned to the browser. For servers that don't implement RFC 7591 (e.g. Slack), supply `oauth: {clientId, clientSecret}` on the server entry to skip registration and use your pre-registered client. The loopback callback defaults to `http://127.0.0.1:19876/mcp/oauth/callback`; override it per-server with `oauth.redirectUri` (any loopback URL) or `oauth.callbackPort`. The API process owns the refresh loop and injects a fresh bearer into the child's config at spawn time — the child subprocess has no database access.

## Runs, snapshots & the UI

- **Concurrent runs.** Multiple runs can be in flight on one project at once; each has its own cancel control, and the execute button stays available while one is running.
- **Snapshots.** Every run freezes a full copy of the graph (nodes, code, edges, input/output boundaries) at creation. Clicking a run in the history enters a read-only **snapshot view** that renders exactly the graph that executed, even after the live graph has changed.
- **Three ways to branch.** Fork the *live* project (`POST /api/workflows/{wid}/fork`), fork a *run's snapshot* into a new project (`POST /api/runs/{rid}/fork`), or re-run a snapshot in place against the frozen graph (`POST /api/runs/{rid}/rerun`).
- **File viewer.** Any path shown in the UI — a run input/output, a JSON leaf, an inline path in chat — is clickable and opens a side panel that resolves it on the backend (`GET /api/files`) and renders it by type: text/code, Markdown or HTML (with a rendered/source toggle), image, PDF, or a browsable directory. From there you can copy the contents, or open the file in your OS default app / reveal it in the file manager (`POST /api/files/open`). The renderer tab has no disk access, so a path that isn't a real file simply falls back to its raw text.
- **Attachments.** Drag-and-drop or paste images, PDFs, or text files anywhere in the window; they reach the LLM as native content parts (gated by the model's image support) and are validated and downscaled before sending.
- **Theme.** A light/dark toggle; the interface uses a paper-and-ink editorial aesthetic.
- **Cost.** Per-turn and per-run cost is shown in USD where the provider reports it (currently OpenRouter).
- **Markdown.** Chat renders Markdown with currency-safe math: `$50K` stays literal text; only `$$...$$` is treated as a math block.

## Configuring providers & models

Settings ("providers & models") is where you connect providers and choose models. Everything here lives in your browser's `localStorage` and rides along each request as headers; the backend never writes your keys to disk.

- **Provider catalog.** The provider and model lists are fetched live from [models.dev](https://models.dev) by the backend (`/api/catalog/*`), cached on disk and in the browser. There is no hardcoded preset list — connect any catalog provider with an API key, sign in to a subscription provider, or add a **Custom** OpenAI-compatible endpoint with its own base URL.
- **Two model roles.** You pick an **orchestrator** model (drives the chat agent) and a separate **node** model (the default for `ctx.call_llm` inside agents). They can use different providers and keys; a run uses the *node* provider/model, deliberately decoupled from whatever the orchestrator chat is signed into.
- **Reasoning variants.** Reasoning-capable models expose ordered effort variants (e.g. low → medium → high → max); a pill in the UI cycles them, and the choice rides as a per-request header.
- **Subscription sign-in.** Two providers support OAuth login instead of an API key: `codex` (your ChatGPT Pro/Plus subscription, routed through the Responses API on the ChatGPT backend) and `xai`. Both use a PKCE flow against a pinned loopback callback; tokens are stored server-side and never returned to the browser.
- **Web tools key.** A [parallel.ai](https://parallel.ai) API key enables the `web_search` and `web_fetch` node tools.
- **Custom instructions.** A free-text field appended to the orchestrator's system prompt.

## Data model

SQLite (`./workflow_builder.db` by default), ten tables: `workflows`, `nodes`, `edges`, `runs`, `node_runs`, `sessions`, `messages`, `settings`, `credentials`, and `mcp_credentials`.

```
Workflow   { id, name, created_at, input_node_id, output_node_id }
Node       { id, workflow_id, name, description, code,
             inputs/outputs: [{name, type_hint, required}], config: {model}, position }
Edge       { id, workflow_id, from_node_id, from_output, to_node_id, to_input }
Run        { id, workflow_id, kind: "user"|"orchestrator",
             status: pending|running|success|error|cancelled,
             inputs, outputs, error, total_cost, workflow_snapshot }
NodeRun    { id, run_id, node_id, status, inputs, outputs,
             logs, llm_calls, tool_calls, error, duration_ms, cost }
Session    { id, workflow_id, created_at }
Message    { id, session_id, role, content, tool_calls, reasoning_details, cost, ts }
Credential { provider: "codex"|"xai", access_token, refresh_token, expires_at, account_id?, label? }
McpCredential { server_name, server_url, access_token?, refresh_token?, expires_at?,
                client_id?, client_secret?, token_endpoint_auth_method?, ... }
```

The `settings` table exists only as a backward-compat hydration path at startup; the live source of truth for provider config is the browser's `localStorage`, forwarded as request headers and applied to the process environment by middleware for the duration of each request.

## Security & limitations

- **Single-user, localhost only.** CORS is pinned to the Vite dev origin and there is no authentication. The per-request mutation of process environment is a deliberate single-user design and is *not* safe for multi-tenant deployment.
- **Node code runs arbitrary Python** in a subprocess. The subprocess boundary is the isolation seam that keeps a crashing or misbehaving agent from taking down the server — it is not a security sandbox. Run only code you trust on a machine you control.
- **Keys live in the browser** and ride as request headers; the backend never persists provider API keys. OAuth tokens (subscription providers and remote MCP servers) are stored server-side and never returned to the browser.
- **By design, not yet:** no cyclic edges (branch via null-propagation; fan out inside a node), no per-call MCP approval prompts (per-server `disabled_tools` are global), and no mid-run MCP token refresh (bearers are resolved at run start).
- **Pinned OAuth details.** Subscription login reuses upstream's published client IDs and pins loopback callback ports (`1455` for Codex, `56121` for xAI, `19876` for MCP). A second instance, or an upstream change to a client/redirect contract, surfaces as a clear error rather than failing silently.

## Repo layout

```
backend/
  app/
    main.py            # FastAPI app, router mounting, per-request settings→env middleware, CORS
    db.py models.py schemas.py
    compaction.py      # Context-window compaction (summarize old turns, prune tool output)
    images.py          # Inbound attachment validation + downscaling (images, PDFs, text)
    api/
      workflows.py nodes.py edges.py   # Graph CRUD + live-workflow fork
      runs.py          # Run lifecycle, rerun/fork-from-snapshot, WebSocket event stream
      files.py         # File-viewer endpoint: resolve a path, classify + return contents; OS open/reveal
      orchestrator.py  # Chat sessions + SSE turn streaming
      settings.py      # DB-backed settings (backward-compat hydration)
      auth.py          # Subscription-OAuth login (codex, xai)
      mcp.py           # MCP status probe, tool discovery, per-server OAuth
      catalog.py       # models.dev provider / model / variant catalog
    llm/
      router.py        # Picks a native transport per model
      openai_chat.py openai_responses.py anthropic_messages.py gemini.py sse.py
    catalog/
      models_dev.py    # Fetches + caches https://models.dev/api.json
      providers.py variants.py
    auth/
      codex.py codex_api.py xai.py     # PKCE flows + Codex Responses-API translator
      oauth.py resolve.py state.py     # PKCE / loopback helpers, token resolution
      mcp_oauth.py                     # Remote MCP server OAuth (DCR / PKCE / refresh)
    orchestrator/
      agent/           # Turn loop, LLM streaming, message persistence, cancel registry
      prompt.py tools.py               # System prompt + 16-tool surface
    runner/
      runner.py child.py service.py    # Subprocess spawn, scheduler, run lifecycle + persistence
      ctx.py tools.py                  # Injected node context + built-in tool registry
      llm.py mcp.py                    # Node-side call_llm + MCP client
      events.py                        # In-memory run event pub/sub → WebSocket
  tests/               # pytest: runner, orchestrator, mcp, llm transport, catalog, compaction, images, run recovery
frontend/
  src/
    App.tsx            # Top-level shell and app state
    api.ts             # REST / SSE / WebSocket client
    appHelpers.ts types.ts localSettings.ts
    providerCatalog.ts modelVariant.ts   # Backend catalog client + reasoning variants
    theme.ts           # Light/dark theme
    auth.ts mcpApi.ts  # Provider-OAuth + MCP clients
    orchestratorStream.ts runWebSocket.ts notify.ts
    components/
      TopBar.tsx ChatPanel.tsx Canvas.tsx Hero.tsx NodePanel.tsx RunPanel.tsx
      Settings.tsx ProviderDialogs.tsx ImageAttachments.tsx ExecutionStats.tsx
      FilePathLink.tsx FileViewerOverlay.tsx   # Clickable paths + the file-viewer side panel
      ThemeToggle.tsx SnapshotBanner.tsx SnapshotRunPanel.tsx Markdown.tsx ...
```
