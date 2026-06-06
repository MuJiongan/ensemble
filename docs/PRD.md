# PRD: Ensemble — Dynamic Agent-Team Orchestrator

## 1. Pitch
A local web application where a user describes a complex problem in natural language, and a central **Orchestrator Agent** (orchestrator LLM) dynamically designs, custom-programs, and links a tailored team of **Specialized Agents** (a directed graph of Python-based execution nodes) to solve it. 

The user interacts with an **Agent Collaboration Board** (canvas) and **Control Panel** side by side. They can execute the agent team's collaboration with custom inputs, directly inspect and rewrite any specialized agent's Python code in a Monaco editor, or chat with the Orchestrator Agent to keep refining the team's topology. 

Every specialized agent runs custom Python code equipped with a dedicated LLM model and tools (shell execution, web search, web fetch) that the agent can invoke to perform its specific role within the team.

* **Deployment Model**: Single-user, localhost-only.

## 2. Core Concepts

Below is how the user-facing **Agent-Team Metaphor** maps directly to the underlying physical **Database and Code Model**:

| User-Facing Concept | Technical Entity | Description |
| :--- | :--- | :--- |
| **Orchestrator Agent** | Orchestrator LLM | The central coordinator that plans the team topology, writes the specialized agents' Python scripts, and presents execution summaries in chat. |
| **Agent Team (Topology)** | Workflow | A directed, acyclic graph (DAG) of specialized collaborating agents linked by communications channels. Also the core project session unit. |
| **Specialized Agent** | Node | A custom Python program that performs a specific sub-task. Equipped with declared inputs, outputs, its own LLM model configuration, and access to a execution toolset. |
| **Handoff Channel** | Edge | A data pathway linking a named output of one agent to a named input of a downstream agent, defining the collaboration flow. |
| **Team Collaboration Run** | Run | A single end-to-end execution of the agent team. Can be triggered by the user or by the Orchestrator Agent. Carries a frozen snapshot of the topology to enable historical inspections and re-runs. |
| **Agent Execution Trace** | NodeRun | The specific outputs, inputs, logs, and sub-LLM or tool calls recorded for an individual agent during a team collaboration run. |
| **Collaboration Board** | Canvas | The visual board rendering the team topology, displaying active execution statuses in real time. |
| **Team Forking** | Workflow Forking | Duplicating a live team session or historical run snapshot into a brand new project session for variation testing. |
| **External MCP Server** | MCP Server Config | A user-configured local stdio or remote streamable-HTTP Model Context Protocol server. Configured in Settings as an opencode-style JSON map; each server can be enabled/disabled and can disable individual tools. |
| **External MCP Tool** | Tool Registry Entry | A discovered MCP tool registered into the runner's in-process tool registry (alongside `shell` / `web_search` / `web_fetch`), namespaced `<server>_<tool>` for LLM tool-calling and reachable from node code as `ctx.tools.<server>.<tool>` (dotted) or `ctx.tools.<server>_<tool>` (flat). |
| **MCP OAuth Credential** | `McpCredential` row | OAuth client info + access/refresh tokens for one remote MCP server, persisted server-side. The API process owns refresh; the runner subprocess only ever receives a fresh bearer at spawn time. |

---

## 3. User Flows

### 3.1 Establishing a New Team
1. The user lands on an empty visual Collaboration Board.
2. The user either clicks **new** in the top bar (creating a fresh, untitled session) or types a prompt in the chat panel.
3. The first message lazily creates an agent team session on the backend and names the project workspace after the query.
4. The visual **Collaboration Board** (left, 2/5 width) and the **Control Panel** (right, 3/5 width) open side by side.
5. In the right Control Panel, under the **chat** tab, the **Orchestrator Agent** streams reasoning and plans team recruitment. As the Orchestrator Agent calls topology-mutation tools (such as recruiting agents or linking outputs), the Collaboration Board on the left refreshes in real time.
6. A status badge in the top bar indicates the current state: **idle / building / running / ready**.

### 3.2 Refining the Team
- The chat panel hosts a persistent conversation per agent team. The user can request structural changes like: *"Add a specialized code auditor agent that reads the developer agent's output,"* *"Make the researcher agent also fetch PDF papers,"* or *"Simplify this team structure to reduce cost."*
- The Orchestrator Agent can mutate the collaboration topology at any time, except when a team collaboration run is currently executing (during which mutations are locked, but read-only inspections are fully available).
- Sending a new message while the Orchestrator Agent is mid-turn cancels the active planning stream immediately. An explicit cancel button is also available.
- For complex, multi-stage problems, the Orchestrator Agent frequently proposes a sequential process (e.g., first set up a team to scope a design, then execute, then verify). The Orchestrator Agent uses the `clean_canvas` tool as a seam between these phases, wiping the active collaboration board clean to design a fresh stage team while fully preserving session history, messages, and historical runs.

### 3.3 Running Collaboration
- When no specific agent is selected on the board, the **workspace** tab in the Control Panel displays the **Run Console** (`RunPanel`). This console renders a form generated from the designated input agent's declared inputs.
- Alternatively, the Orchestrator Agent can trigger the team collaboration directly via the `run_workflow` tool once all required parameters are established in conversation. The chat streams a `run_started` indicator, the board lights up with live websocket event streams, and the Orchestrator blocks until execution completes.
- During execution, agents on the board display their real-time state via glowing visual markers: `idle → running → success / error / skipped`.
- The Run Console renders a real-time trace for each active agent, displaying internal scripts, stdout logs, sub-LLM calls (with prompts, token cost, and latency), tool invocations, and input/output values.
- The user can click **cancel** mid-execution, which cleanly SIGTERMs the runner subprocess and propagates a cancelled status.
- Recent runs (up to 20) are listed in the history drawer. Clicking any run swaps the viewport into **Snapshot Mode**, rendering the exact frozen topology that executed. The user can inspect the historical trace, click "create project" to fork the historical team into a new active session, or click "← live" to return to the live team.

### 3.4 Tuning an Agent Directly
- The user can click any specialized agent on the Collaboration Board. This auto-flips the right Control Panel to the **workspace** tab and opens the **Agent Details** panel (`NodePanel`).
- The Agent Details panel provides tabs to inspect:
  - **code**: A Monaco editor to hand-edit the agent's custom Python code.
  - **i/o**: Read-only visualization of the inputs/outputs ports.
  - **config**: Individual LLM model configuration.
  - **last run**: The trace and logs specific to this agent during the last execution.
- If the user edits the code or model and clicks save, the agent is flagged with a `user_edited` marker. 
- The Orchestrator Agent is injected with this `user_edited` state per agent during its planning turns. The system instructions mandate that the Orchestrator must respect user edits: it must call `view_node_details` first and surgically modify code rather than rewriting it, unless explicitly directed otherwise.

### 3.5 Connecting External MCP Servers
1. The user opens settings and adds one or more MCP server connections. Two transports are supported: **local** (a stdio child process — `{type: "local", command: [...], environment: {...}}`) and **remote** (a streamable-HTTP URL — `{type: "remote", url, headers: {...}}`). The structured editor serialises to the opencode-style JSON map stored in `settings.mcp_servers`; settings autosave on every edit.
2. Each server row has a per-card test button that probes the connection and reports `connected` / `needs_auth` / `failed` along with the discovered tool count. Clicking "view tools" pops out the full per-server tool list with each tool's complete input schema; individual tools can be disabled from this dialog (persisted as `disabled_tools` on the server).
3. Remote servers are OAuth-capable by default (matching opencode's `oauth !== false`). A sign-in widget on the row runs an OAuth flow that uses metadata discovery, dynamic client registration (RFC 7591), and PKCE — all handled by the MCP SDK. Tokens are persisted in the backend `McpCredential` table and never round-tripped to the browser; the API process refreshes them automatically on expiry.
4. On run start, the runner subprocess connects to every enabled server, runs `tools/list`, and registers the surviving tools (after per-server `disabled_tools` filtering) into its in-process tool registry alongside `shell` / `web_search` / `web_fetch`.
5. The Orchestrator Agent receives a per-turn system message listing every discovered tool with a one-line summary and the names to call it by. It cannot invoke MCP tools itself; it only writes node Python that uses them. Before constructing a direct call with nested arguments, it can call `get_mcp_tool_schema(server, tool)` to fetch the untruncated JSON input schema.
6. During a run, every MCP invocation is captured in the node trace (same shape as built-in tool calls) — tool name, arguments, result summary, status, and error details all stream over the existing run WebSocket.

---

## 4. Functional Requirements

### 4.1 Orchestrator Agent
- Powered by an LLM over any OpenAI-compatible chat-completions provider (OpenRouter default, OpenAI, Groq, Together, DeepSeek, Cerebras, Fireworks, xAI, Mistral, or any custom base URL — plus subscription-OAuth presets that authenticate via PKCE against ChatGPT/Codex and xAI). The frontend maintains a `default_orchestrator_models[provider_id]` map and forwards the active value on each request; the backend reads it from the request env (with a legacy DB setting as backwards-compat fallback) and ultimately falls back to `anthropic/claude-opus-4.7`. OpenRouter-only request fields (`usage.include`, `reasoning.effort`) are gated by base-URL inspection so stricter providers don't reject calls.
- Leverages always-on extended thinking (`reasoning.effort = "medium"`) to plan topologies and debug code. Extended thinking outputs are saved and echoed back to the LLM on subsequent turns to maintain Anthropic's strict contextual continuity.
- **Decoupled Agent Customization**: To prevent massive LLM context payloads, the tool surface splits structural recruitment from programming. The orchestrator first calls `add_node` to recruit a specialized agent (defining its name, role description, and port contracts) which creates a basic Python stub. The orchestrator then calls `configure_node` separately to author and inject the actual custom Python code.
- **Outcome Reporting**: When `run_workflow` successfully executes the team, the Orchestrator Agent calls `view_run` to inspect outputs, extracts the most valuable slices of information, and presents them in chat alongside direct highlights.
- Streams extended thinking, assistant content, tool-call states, and execution notifications over **Server-Sent Events** (`POST /api/sessions/{sid}/messages`).
- Every turn, the Orchestrator receives a concise system message describing the `[current graph state]`. Code is omitted; the orchestrator retrieves code only when necessary using `view_node_details` to keep context window sizes minimal and fast.
- **MCP Tool Boundary**: The Orchestrator Agent never receives external MCP tools as callable tools in its own tool surface. When MCP servers are connected, an additional per-turn system message lists each discovered tool with a one-line summary and the exact names to invoke it by (the flat `<server>_<tool>` for LLM tool-calling and the dotted `ctx.tools.<server>.<tool>` for direct calls). The orchestrator uses this directory to write node code; it can fetch the untruncated JSON input schema for any tool via the `get_mcp_tool_schema(server, tool)` helper. Execution itself is restricted to node runtime.

### 4.2 Agent Runtime & Skip Protocols
Each recruited agent executes its script inside an isolated runner environment, conforming to the following Python contract:
```python
def run(inputs: dict, ctx) -> dict:
    ...
    return {"output_name": value_or_None, ...}
```

The injected execution context (`ctx`) provides:
- `ctx.call_llm(model, prompt, tools=[...], **opts)`: Runs an LLM inside the agent. Pass `model=""` to inherit the default node model. The `tools` list names entries from the runner's in-process registry — built-in tools (`shell`, `web_search`, `web_fetch`) and any discovered MCP tools by their flat `<server>_<tool>` namespace.
- `ctx.tools.<name>(...)`: Direct access to a registry tool bypassing LLM routing. MCP tools are reachable both flat (`ctx.tools.<server>_<tool>(...)`) and dotted by server (`ctx.tools.<server>.<tool>(arg=...)`).
- `ctx.log(msg)`: Appends an execution log line visible in the run console.
- `ctx.workdir`: Path to an isolated scratch folder on the local filesystem.

**Branching & Skip Rules (Conditional Fallbacks):**
- An agent's handoff outputs can return `None`.
- Downstream handoff ports are configured as either **required** or **optional**.
- **Skip Protocol**: If any *required* handoff input delivers `None`, the downstream agent is skipped entirely and automatically emits `None` on all of its declared outputs, propagating the skip clean down the branch. If an input is *optional* and receives `None`, the agent executes normally (allowing it to implement fallback logic). This enables dynamic branching and decision paths without complex visual loops.

**Execution Model:**
- Team execution runs in a dedicated subprocess (`python -m app.runner.child`). 
- Independent agents are executed concurrently via a `ThreadPoolExecutor`. An agent is queued the moment all of its upstream dependencies have resolved their outputs (or triggered a skip).
- Logs, sub-LLM calls, and tool calls are piped to the parent process as structured JSON lines (`app.runner.events`), which are published via memory queues directly into the live WebSocket server (`/api/runs/{rid}/events`).

### 4.3 `call_llm` Framework
- Provider-agnostic utility that POSTs to `{base}/chat/completions` against the active LLM provider. The base URL, API key (or OAuth bearer), and provider id ride along the request as headers (`X-Llm-Base-Url`, `X-Llm-Api-Key`, `X-Llm-Provider-Id`); the backend never persists them.
- Signature: `call_llm(model: str, prompt: str | messages, tools: list[str] = [], **opts) -> dict`.
- If equipped with `tools`, runs a local agent loop: calls the LLM, parses tool calls, executes tools locally, and feeds results back until the LLM returns a final text block.
- For OAuth providers, the runtime resolves the bearer from the server-side `Credential` table and refreshes it before calls when expiry is near. The ChatGPT-subscription preset additionally translates chat-completions payloads to/from the Codex Responses-API transport, yielding the same `(text | thinking | tool_args | done)` SSE tuples downstream consumers already expect.
- Streams token-by-token using unique `call_id` headers, allowing the React frontend to display multiple sub-LLM calls executing in parallel as live, updating text cards.

### 4.4 Specialized Agent Tool Library (v1)

| Tool | Purpose | Auth |
| :--- | :--- | :--- |
| `shell` | Runs an OS terminal command. Handles local file reading, compiling, and execution. Returns `{stdout, stderr, returncode}`. Pinned to a 30s timeout. | None (flagged dangerous in UI) |
| `web_search` | Executes a web search via parallel.ai to fetch ranked URLs and snippets. | `parallel.ai` API Key |
| `web_fetch` | Extracts clean markdown content from target URLs or PDFs via parallel.ai Extract. | `parallel.ai` API Key |

Tools are configured in the global registry `app.runner.tools.REGISTRY`. Agents list their tools in Python calls (`ctx.call_llm(tools=["web_search"])`).

### 4.5 External MCP Tool Integrations

**Scope**
- Users connect any number of external MCP servers; a curated built-in integration catalog is out of scope.
- MCP tools are available only to specialized agent nodes at run time. The orchestrator is told *about* them but never calls them.
- Node code can invoke a discovered MCP tool two ways:
  - **Direct** — `ctx.tools.<server>.<tool>(arg=...)` (dotted, keyword args only) or the flat `ctx.tools.<server>_<tool>(...)` form.
  - **Agentic** — name the flat `<server>_<tool>` in `ctx.call_llm(..., tools=[...])` and let the node's inner LLM decide when to call it.

**Server Configuration**
- The Settings panel renders a structured editor over the `settings.mcp_servers` JSON string (opencode shape: a map of `name → { type: "local" | "remote", ... }`). Settings autosave on every edit, so a row becomes live the instant it parses.
- Local servers are stdio child processes: `{type: "local", command: [...], environment: {...}}`. Remote servers are streamable-HTTP URLs: `{type: "remote", url, headers: {...}}`.
- Remote servers are OAuth-capable by default (matching opencode's `oauth !== false`); per-server `enabled` and `disabled_tools` are first-class fields. A `timeout` (ms) overrides the default per-call ceiling.
- Static `headers` on a remote server round-trip verbatim — a user can paste a pre-issued bearer if they prefer that to OAuth. A per-server `oauth` block carries pre-registered client credentials and loopback overrides (`clientId`, `clientSecret`, `scope`, `redirectUri`, `callbackPort`) for providers without dynamic client registration; the structured editor surfaces these in a collapsible "oauth client" disclosure on each remote row.
- Failed transports, failed handshakes, and protocol errors surface in the per-server status probe with a status of `failed` and the underlying error message.

**Tool Discovery & Registry**
- On the first connection (and after a config change), the MCP module performs the initialize handshake and runs `tools/list`, caching the discovered descriptors keyed by config string.
- Discovered tools are registered with the qualified name `<server>_<tool>` (sanitized to match the `^[a-zA-Z0-9_-]{1,64}$` shape OpenAI-style tool calling requires) so two servers can expose tools with the same raw name without colliding. The same descriptor also drives the dotted `ctx.tools.<server>.<tool>` form via a server-keyed namespace map.
- Tool input schemas are normalised into the same callable JSON-schema format `ctx.call_llm` already uses for built-ins. Object schemas that omit `properties` get an empty one inserted so strict providers don't reject the tool spec.
- Per-server `disabled_tools` are applied *before* the orchestrator advertisement and *before* registry registration — a globally disabled tool is invisible everywhere. Discovery itself stays unfiltered so the Settings "view tools" dialog can render the disabled rows with their toggle off.

**Connection Verification & Health**
- A configured server is not considered usable until Ensemble completes the MCP initialization handshake and successfully runs `tools/list`.
- A lightweight `POST /api/mcp/status` endpoint reports per-server `{status, tool_count?, error?}` where `status ∈ {connected, needs_auth, failed}`, optionally narrowed to a single server for the per-card test button. A companion `POST /api/mcp/tools` returns the full per-tool list with descriptions and untruncated input schemas (powers the "view tools" dialog).
- Disabled servers are skipped entirely during status probing and discovery.
- Status probes use the in-memory descriptor cache, so a connected server's tool count is returned without a second `tools/list` round-trip.

**OAuth (Remote Servers)**
- The MCP SDK handles metadata discovery, dynamic client registration (RFC 7591), PKCE, and token refresh. Ensemble plugs in a `TokenStorage` bridge that persists everything the SDK round-trips — access token, refresh token, expiry, scope, dynamically-registered `client_id`/`client_secret`, and the token-endpoint auth method.
- **Pre-registered clients (no DCR)**: Providers that don't implement RFC 7591 (Slack's `https://mcp.slack.com/mcp`, others) fail the SDK's DCR fallback. Supplying `oauth.clientId` (and `clientSecret`/`scope` as needed) makes the `TokenStorage` bridge return that client info directly, so the SDK's DCR gate finds populated `client_info` and skips registration. The token-endpoint auth method is threaded through accordingly — `client_secret_post` for confidential clients, `none` for public/PKCE clients.
- Server-name-keyed login endpoints (`POST /api/mcp/{server}/login/start` / `…/login/status` / `…/login/cancel` / `…/logout`) mirror the existing LLM subscription-OAuth shape so the same `OAuthLoginField` widget can drive them.
- The OAuth callback defaults to the loopback URI `http://127.0.0.1:{MCP_OAUTH_PORT}/mcp/oauth/callback` (`MCP_OAUTH_PORT` defaults to `19876`). A server can override it with `oauth.redirectUri` (any loopback URL) or `oauth.callbackPort` (port shorthand) when a provider's registered redirect contract differs; the loopback callback server binds the effective URI. A non-loopback `redirectUri` is rejected up-front since the local flow can't catch the callback, and a port conflict surfaces as a 409 with an actionable message.
- A workaround for auth servers (e.g. Notion) that return a `client_secret` on DCR without setting `token_endpoint_auth_method`: the storage layer coerces to `client_secret_post` whenever a secret is present, both at storage time and on restore, so the SDK doesn't silently send no client auth and 401.
- Connect/login failures are run through `_format_connect_error`, which recursively unwraps anyio `BaseExceptionGroup`s (so a `TaskGroup` doesn't mask the real cause as "unhandled errors in a TaskGroup"), trims overlong messages, and swaps a DCR-rejection's multi-KB HTML body for an actionable "set oauth.clientId" hint surfaced in the status probe and login error.

**Orchestrator Restrictions**
- The Orchestrator Agent's LLM tool list never includes MCP tools — they are not callable by the orchestrator.
- When MCP servers are configured, every orchestrator turn additionally receives an `[available MCP tools]` system message grouping the discovered tools by server, with each line showing the dotted `ctx.tools.<server>.<tool>` form, the flat `<server>_<tool>` LLM name, and a one-line summary derived from the tool description.
- A new inspection tool `get_mcp_tool_schema(server, tool)` returns the complete `{server, tool, call, llm_name, description, input_schema}` for one tool so the orchestrator can construct nested arguments that pass server-side validation.
- The orchestrator prompt explicitly states that MCP tools are executable only inside node runs.

**Node Runtime Execution**
- At child-subprocess startup, the runner reads `MCP_SERVERS` from its environment, connects to every enabled server on a single shared asyncio loop, and registers the surviving tools into the in-process `app.runner.tools.REGISTRY` (and the dotted-namespace map). Connection failures are logged to stderr (captured by the parent) but never fail the run.
- Direct calls are bridged from sync node code onto the asyncio loop via `run_coroutine_threadsafe`. Each call gets a unique `call_id` and emits `tool_call_started` / `tool_call_finished` events on the run WebSocket alongside built-in tool calls.
- Agentic calls use the existing `call_llm` agent loop: LLM tool calls land in the same registry, are dispatched into the MCP client, and the result is fed back to the node-level LLM as a tool message.
- Tool-call timeouts default to a generous 120s ceiling (well above opencode's 5s default — MCP tools may launch browsers or do long searches). A per-server `timeout` (ms) overrides this.
- On a clean run, MCP transports are shut down gracefully so local stdio servers exit cleanly. On cancel, the runner force-exits and lets process teardown reap them — we don't block a cancel on a slow server.
- MCP tool failures (timeout, transport error, server-reported `isError: true`) are captured as structured tool-call errors and never crash the FastAPI parent.

**OAuth Bearer Resolution Across the Process Boundary**
- OAuth credentials live in the API process's `mcp_credentials` table. The child runner subprocess has no DB access on purpose.
- At child spawn time, the parent calls `mcp.resolve_oauth_config(...)` which, for every remote OAuth-backed server, refreshes the token if it's near expiry and rewrites the config to inject `Authorization: Bearer <token>` into the server's headers. The child reads the resulting `MCP_SERVERS` env var and connects without ever touching the credentials DB.
- A long-running child does *not* auto-refresh — token refresh is only at spawn time, since runs are short-lived and refresh during a run would create a parent/child credential split.

**Traceability**
- `NodeRun.tool_calls` records every MCP invocation with its qualified name, arguments, result (or error), status, and `via: "direct" | "llm"` source. Run snapshots preserve the workflow graph that executed; MCP tools resolved at run-start via the live config.
- The frontend run-trace cards render MCP tool calls in the same shape as built-in tool calls so behaviour is uniform across both.

### 4.6 User Interface
- **Visual Collaboration Board (Canvas)**: Left pane (2/5 width) rendering the `@xyflow/react` graph. Agents display as structured cards with execution state indicators. Manual dragging or edge editing is disabled; the Orchestrator Agent maintains complete ownership over layout generation. Clicking an agent selects it.
- **Control Panel**: Right pane (3/5 width) switching between:
  - **chat**: Displays the Orchestrator Agent's SSE text stream, collapsible planning/reasoning logs, and live status badges for tool runs.
  - **workspace**: Displays `RunPanel` (when no agent is selected) showing the input form and live trace feeds, `NodePanel` (when an agent is selected) exposing the Monaco code editor, or snapshot panels when in historical view.
- **Dollar-sign Rendering Guard**: `remark-math` is configured with `singleDollarTextMath: false` to ensure common notations like currency (`$50K`) render as normal text, reserving math block rendering strictly for double dollar signs (`$$...$$`).
- **Settings Panel**: A provider dropdown lists **API-key presets** (OpenRouter default, OpenAI, Groq, Together, DeepSeek, Cerebras, Fireworks, xAI, Mistral, Custom) and **OAuth presets** (ChatGPT subscription via Codex, xAI sign-in via Grok-CLI). API-key presets show a per-provider key field; switching providers swaps the active key, default orchestrator model, and default node model. OAuth presets replace the key field with a Sign in / Sign out widget that opens a PKCE flow popup against the upstream provider, with tokens stored server-side. A parallel.ai key is configured alongside for `web_search` / `web_fetch`. API keys live in browser `localStorage` and ride request headers; OAuth tokens are persisted server-side in the `Credential` table and never round-trip to the browser. The panel autosaves on every edit.
- **MCP Section**: A structured editor over `settings.mcp_servers` (opencode-shape JSON, kept in `localStorage` and forwarded via the `X-Mcp-Servers` request header). Each row supports local (stdio command) and remote (HTTP URL) types with their own field set, a per-card status probe (connected / needs_auth / failed + discovered-tool count), a "view tools" popout that lists the full per-server tool inventory with input schemas and per-tool disable toggles, and — for remote servers — a Sign in / Sign out widget mirroring the LLM OAuth flow plus a collapsible "oauth client" disclosure with editable `clientId` / `clientSecret` / `scope` / `redirectUri` inputs for providers that need a pre-registered client. The `McpRow` round-trips these fields through the `mcp_servers` JSON.

### 4.7 Data Persistence & Workspaces
- **SQLite Database**: Standard relational storage containing:
  - `Workflow` (underlying record of the Agent Team)
  - `Node` (underlying record of a Specialized Agent)
  - `Edge` (underlying record of a Handoff Channel)
  - `Session`, `Message` (chat histories)
  - `Run`, `NodeRun` (execution records)
  - `Credential` (OAuth access/refresh tokens for subscription-login providers, e.g. ChatGPT subscription, xAI sign-in; never exposed to the frontend)
  - `McpCredential` (OAuth client info + access/refresh tokens for one remote MCP server, keyed by server name; persists everything the MCP SDK's `TokenStorage` round-trips — access/refresh tokens, expiry, scope, dynamically-registered `client_id`/`client_secret`, and the token-endpoint auth method)
  - Default database sits at `./workflow_builder.db`.
- **Filesystem Workspace**: Sandboxed temporary execution folders (`tempfile.mkdtemp(prefix="wfrun-")`) allocated per run to house files, code outputs, or scrap scripts created by active agents.
- **Run Deletion Protection**: Projects cannot be deleted if a team collaboration run is currently `pending` or `running` to avoid orphaned system processes.

---

## 5. Non-Functional Requirements
- **Local-first Security**: All servers run entirely on localhost. No external authentication is required.
- **Resilience & Process Isolation**: If an individual agent crashes, raises an error, or runs a broken shell script, the FastAPI server remains fully operational. The subprocess boundary captures failures cleanly and emits a synthetic failure event to ensure WebSockets close gracefully.
- **State Streaming**: SSE streams text token-by-token; WebSockets stream execution events.

---

## 6. Underlying Data Model (Physical Schema)

Despite the user-facing agent-team framing, the database matches the following physical schema to maintain backward compatibility:

```
Workflow   { id, name, created_at, input_node_id, output_node_id }

Node       { id, workflow_id, name, description, code,
             inputs:  [{name, type_hint, required: bool}],
             outputs: [{name, type_hint, required: bool}],
             config:  { model },
             position: {x, y},
             user_edited_at: datetime? }

Edge       { id, workflow_id,
             from_node_id, from_output,
             to_node_id,   to_input }

Session    { id, workflow_id, created_at }

Message    { id, session_id, role, content, tool_calls,
             tool_call_id?, name?, reasoning_details, ts }

Run        { id, workflow_id, kind, status, inputs, outputs, error,
             started_at, ended_at, total_cost,
             workflow_snapshot: { nodes[], edges[], input_node_id, output_node_id }? }
           # kind: "user" | "orchestrator"
           # status: "pending" | "running" | "success" | "error" | "cancelled"

NodeRun    { id, run_id, node_id, status, inputs, outputs,
             logs, llm_calls, tool_calls, error, duration_ms, cost }

Setting    { key, value }

Credential { id, provider_id, access_token, refresh_token,
             expires_at, account_id?, scope?,
             created_at, updated_at }
           # provider_id: e.g. "chatgpt-subscription", "xai-oauth"
           # account_id used by Codex (sent as X-ChatGPT-Account-Id header)

McpCredential
           { server_name, server_url,
             access_token?, refresh_token?, expires_at?, scope?, token_type?,
             client_id?, client_secret?,
             client_id_issued_at?, client_secret_expires_at?,
             token_endpoint_auth_method?,
             created_at, updated_at }
           # MCP server config itself lives in the browser (localStorage,
           # `settings.mcp_servers`) and forwards as the X-Mcp-Servers header —
           # no McpServer / McpTool tables are needed, since discovery runs
           # in-process and caches by config string.
```

---

## 7. Orchestrator Agent Tool Surface

The Orchestrator Agent is equipped with the following tool signatures to inspect state and modify team topologies:

```
# Read-Only Inspection (Always allowed)
view_graph()                           -> {workflow_id, name, input_node_id, output_node_id, nodes[], edges[]}
view_node_details(node_id)             -> {full node record incl. code, user_edited}
get_mcp_tool_schema(server, tool)      -> {server, tool, call, llm_name, description, input_schema}  # untruncated input schema for one MCP tool
list_runs(limit?)                      -> {runs: [{run_id, status, kind, started_at, ended_at, total_cost, error}], count, limit}
view_run(run_id, node_id?, fields?)    -> {run_id, status, outputs, node_errors, error, total_cost} or per-agent traces

# Topology Mutation (Blocked during active executions)
add_node(name, description, inputs, outputs, model) -> {node_id, node}  # Recruits a specialized agent stub
remove_node(node_id)                                                    # Removes an agent
rename_node(node_id, new_name)                                          # Renames an agent
configure_node(node_id, **partial_fields)                               # Custom-programs agent code, descriptions, or models
add_edge(from_node_id, from_output, to_node_id, to_input) -> {edge_id, edge} # Creates a handoff channel
remove_edge(edge_id)                                                    # Deletes a handoff channel
set_input_node(node_id)                                                 # Sets team input boundary
set_output_node(node_id)                                                # Sets team output boundary
clean_canvas()                                                          # Wipes visual board; starts a fresh stage

# Execution Trigger (Blocks until run terminates)
run_workflow(inputs)                   -> {run_id, status, total_cost}  # Triggers team collaboration
```

---

## 8. Technology Stack
- **Backend:** Python 3.11+, FastAPI, WebSockets, Server-Sent Events, SQLAlchemy + SQLite, isolated `subprocess` execution, HTTPX client for any OpenAI-compatible LLM provider, plus a Codex Responses-API translator for the ChatGPT-subscription transport and a loopback HTTP server for OAuth PKCE callbacks.
- **Frontend:** React 18, Vite, `@xyflow/react` for Canvas, `@monaco-editor/react` for the code workbench, `react-markdown` + `remark-gfm` for chat. Vanilla CSS and layout styling.
- **MCP Layer:** Official `mcp` Python SDK for stdio + streamable-HTTP transports, single shared asyncio loop bridging the synchronous threaded runner via `run_coroutine_threadsafe`, custom `TokenStorage` bridging the SDK's OAuth state into the `mcp_credentials` SQLite table.

---

## 9. Out of Scope (v1) / Future Work
- Multi-user collaboration, hosted accounts, or cloud database sync.
- Visual loop connections (edges forming cycles). Sub-agent looping is handled inside an individual agent's Python logic instead.
- Curated built-in MCP integration marketplace/catalog.
- Direct Orchestrator Agent execution of MCP tools.
- Per-session MCP tool allowlists or per-call approval prompts (per-server `disabled_tools` are global, not per-session).
- Mid-run MCP OAuth token refresh (resolution happens at child-subprocess spawn time only).
- **Generative UI (Designer Agent)**: A planned addition (Phase 9 in the tracker below) where a secondary agent generates custom, standalone HTML user interfaces tailored to the team's input/output schemas, letting users execute workflows via tailored forms rather than simple textareas.
- **Run Pruning**: Automatic garbage-collection of runs exceeding 20 is on the roadmap; currently, older runs accumulate in SQLite.
- **Single-Process Packaging**: Packaging Vite and FastAPI together inside a single pip-installable distribution remains a polish goal.

---

## 10. Key Technical Risks
- **Agent Code Quality**: The application relies heavily on the LLM's capability to author functional Python code. The orchestrator uses `run_workflow` and `view_run` to inspect and debug its own generated code before concluding turns.
- **Subprocess Isolation**: Malicious or recursive scripts executed by custom agents are isolated within the subprocess boundary; trusted local execution mitigates high security concerns.
- **Reasoning Order Rules**: Strict compliance with OpenRouter/Anthropic's sequence requirements (re-sending reasoning blocks unmodified) is critical to prevent turn execution failures.
- **External MCP Tool Trust**: User-configured MCP servers can perform arbitrary external actions. Per-server enable/disable, per-tool `disabled_tools` opt-outs, qualified `<server>_<tool>` namespacing, full trace capture, and local-only deployment are the safeguards. There is no per-call approval step — once a tool is configured and enabled, node code can invoke it without further prompting.
- **MCP Schema Drift / Cache Invalidation**: Tool descriptors are cached by raw config string and reused across orchestrator turns. A live MCP server that changes its tool schema mid-session won't be re-probed until the user edits the config. For long-lived servers (Notion etc.) this is fine; for actively-developed local servers, restart-the-config is the workaround.
- **MCP OAuth Provider Drift**: The MCP SDK handles dynamic client registration + token refresh, but auth servers can vary in non-spec ways. Notion returns a `client_secret` on DCR without a `token_endpoint_auth_method` (coerced to `client_secret_post`); Slack doesn't implement RFC 7591 DCR at all, so it requires a hand-supplied `oauth.clientId`/`clientSecret`. New providers may need similar small storage-layer coercions or a pre-registered client.
- **Reused Public OAuth Client IDs**: Subscription-OAuth presets reuse Codex CLI's and Grok-CLI's published `client_id`s (matching the opencode pattern) and pin specific loopback callback ports (`1455`, `127.0.0.1:56121`) required by upstream's registered redirect URIs. Either provider can revoke the client at any time or change the redirect contract, breaking sign-in until a new client_id is wired up. Local port conflicts surface as a clear 409.

---

## 11. Milestones & Progress Tracker
1. **Skeleton** ✅ FastAPI + React Flow + SQLite, manually-built workflows, can run a hand-coded node graph end-to-end with `call_llm` and one tool.
2. **Persistence & REST APIs** ✅ persistent database tables for workflows, nodes, edges, runs, settings, sessions/messages.
3. **Manual Builder Workbench** ✅ visual canvas, Monaco editor drawers, port inspector, run console with inputs form.
4. **Active Event Streaming** ✅ WebSockets, real-time node state changes, cancel support.
5. **Orchestrator Agent v1** ✅ SSE chat stream, collapsible reasoning logs, tool call cards, and `user_edited` locks.
6. **Orchestrator-Driven Collaboration** ✅ Orchestrator can run graphs, diagnose trace errors, clear board between stage solves, and fork snapshots into fresh active projects.
7. **Multi-Provider LLM Support** ✅ Provider-agnostic LLM caller for any OpenAI-compatible `{base}/chat/completions` endpoint; curated preset registry (OpenRouter, OpenAI, Groq, Together, DeepSeek, Cerebras, Fireworks, xAI, Mistral, Custom); per-provider API-key storage with active-provider headers; subscription-OAuth presets (ChatGPT via Codex PKCE, xAI via Grok-CLI PKCE) with server-side `Credential` token storage, automatic refresh-on-expiry, and a Codex Responses-API ↔ chat-completions translator.
8. **External MCP Tool Integrations** ✅ User-configured MCP servers (local stdio + remote streamable-HTTP) in Settings, runtime tool discovery on every run, dotted + flat ctx.tools access, MCP tools in `ctx.call_llm(tools=[...])`, orchestrator per-turn MCP directory + `get_mcp_tool_schema` helper, OAuth (discovery + DCR + PKCE + refresh) for remote servers persisted in `mcp_credentials`, per-server `disabled_tools` opt-outs, per-card connection status + tool browser in Settings.
9. **Generative UI / Designer Agent** ⏳ Dedicated Design tab allowing a specialized designer agent to generate tailor-made UI pages for running stable agent teams.
10. **Distribution Polish** ⏳ Run pruning and single-process packaging integration.
