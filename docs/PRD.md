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

---

## 4. Functional Requirements

### 4.1 Orchestrator Agent
- Powered by an LLM over OpenRouter; default model is read from the user's settings (`default_orchestrator_model`), falling back to `anthropic/claude-opus-4.7`.
- Leverages always-on extended thinking (`reasoning.effort = "medium"`) to plan topologies and debug code. Extended thinking outputs are saved and echoed back to the LLM on subsequent turns to maintain Anthropic's strict contextual continuity.
- **Decoupled Agent Customization**: To prevent massive LLM context payloads, the tool surface splits structural recruitment from programming. The orchestrator first calls `add_node` to recruit a specialized agent (defining its name, role description, and port contracts) which creates a basic Python stub. The orchestrator then calls `configure_node` separately to author and inject the actual custom Python code.
- **Outcome Reporting**: When `run_workflow` successfully executes the team, the Orchestrator Agent calls `view_run` to inspect outputs, extracts the most valuable slices of information, and presents them in chat alongside direct highlights.
- Streams extended thinking, assistant content, tool-call states, and execution notifications over **Server-Sent Events** (`POST /api/sessions/{sid}/messages`).
- Every turn, the Orchestrator receives a concise system message describing the `[current graph state]`. Code is omitted; the orchestrator retrieves code only when necessary using `view_node_details` to keep context window sizes minimal and fast.

### 4.2 Agent Runtime & Skip Protocols
Each recruited agent executes its script inside an isolated runner environment, conforming to the following Python contract:
```python
def run(inputs: dict, ctx) -> dict:
    ...
    return {"output_name": value_or_None, ...}
```

The injected execution context (`ctx`) provides:
- `ctx.call_llm(model, prompt, tools=[...], **opts)`: Runs an LLM inside the agent. Pass `model=""` to inherit the default node model. The sub-LLM uses the list of tools to fulfill its task.
- `ctx.tools.<name>(...)`: Direct access to a tool bypassing LLM routing (discouraged in v1; agents are intended to be LLM-driven).
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
- Global utility over OpenRouter.
- Signature: `call_llm(model: str, prompt: str | messages, tools: list[str] = [], **opts) -> dict`.
- If equipped with `tools`, runs a local agent loop: calls the LLM, parses tool calls, executes tools locally, and feeds results back until the LLM returns a final text block.
- Streams token-by-token using unique `call_id` headers, allowing the React frontend to display multiple sub-LLM calls executing in parallel as live, updating text cards.

### 4.4 Specialized Agent Tool Library (v1)

| Tool | Purpose | Auth |
| :--- | :--- | :--- |
| `shell` | Runs an OS terminal command. Handles local file reading, compiling, and execution. Returns `{stdout, stderr, returncode}`. Pinned to a 30s timeout. | None (flagged dangerous in UI) |
| `web_search` | Executes a web search via parallel.ai to fetch ranked URLs and snippets. | `parallel.ai` API Key |
| `web_fetch` | Extracts clean markdown content from target URLs or PDFs via parallel.ai Extract. | `parallel.ai` API Key |

Tools are configured in the global registry `app.runner.tools.REGISTRY`. Agents list their tools in Python calls (`ctx.call_llm(tools=["web_search"])`).

### 4.5 User Interface
- **Visual Collaboration Board (Canvas)**: Left pane (2/5 width) rendering the `@xyflow/react` graph. Agents display as structured cards with execution state indicators. Manual dragging or edge editing is disabled; the Orchestrator Agent maintains complete ownership over layout generation. Clicking an agent selects it.
- **Control Panel**: Right pane (3/5 width) switching between:
  - **chat**: Displays the Orchestrator Agent's SSE text stream, collapsible planning/reasoning logs, and live status badges for tool runs.
  - **workspace**: Displays `RunPanel` (when no agent is selected) showing the input form and live trace feeds, `NodePanel` (when an agent is selected) exposing the Monaco code editor, or snapshot panels when in historical view.
- **Dollar-sign Rendering Guard**: `remark-math` is configured with `singleDollarTextMath: false` to ensure common notations like currency (`$50K`) render as normal text, reserving math block rendering strictly for double dollar signs (`$$...$$`).
- **Settings Panel**: Input keys for OpenRouter and parallel.ai, and configure default models. Data is held in browser storage and passed on headers, preventing database persistence of keys.

### 4.6 Data Persistence & Workspaces
- **SQLite Database**: Standard relational storage containing:
  - `Workflow` (underlying record of the Agent Team)
  - `Node` (underlying record of a Specialized Agent)
  - `Edge` (underlying record of a Handoff Channel)
  - `Session`, `Message` (chat histories)
  - `Run`, `NodeRun` (execution records)
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
```

---

## 7. Orchestrator Agent Tool Surface

The Orchestrator Agent is equipped with the following tool signatures to inspect state and modify team topologies:

```
# Read-Only Inspection (Always allowed)
view_graph()                           -> {workflow_id, name, input_node_id, output_node_id, nodes[], edges[]}
view_node_details(node_id)             -> {full node record incl. code, user_edited}
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
- **Backend:** Python 3.11+, FastAPI, WebSockets, Server-Sent Events, SQLAlchemy + SQLite, isolated `subprocess` execution, HTTPX client for OpenRouter.
- **Frontend:** React 18, Vite, `@xyflow/react` for Canvas, `@monaco-editor/react` for the code workbench, `react-markdown` + `remark-gfm` for chat. Vanilla CSS and layout styling.

---

## 9. Out of Scope (v1) / Future Work
- Multi-user collaboration, hosted accounts, or cloud database sync.
- Visual loop connections (edges forming cycles). Sub-agent looping is handled inside an individual agent's Python logic instead.
- **Generative UI (Designer Agent)**: A planned Phase 7 addition where a secondary agent generates custom, standalone HTML user interfaces tailored to the team's input/output schemas, letting users execute workflows via tailored forms rather than simple textareas.
- **Run Pruning**: Automatic garbage-collection of runs exceeding 20 is on the roadmap; currently, older runs accumulate in SQLite.
- **Single-Process Packaging**: Packaging Vite and FastAPI together inside a single pip-installable distribution remains a polish goal.

---

## 10. Key Technical Risks
- **Agent Code Quality**: The application relies heavily on the LLM's capability to author functional Python code. The orchestrator uses `run_workflow` and `view_run` to inspect and debug its own generated code before concluding turns.
- **Subprocess Isolation**: Malicious or recursive scripts executed by custom agents are isolated within the subprocess boundary; trusted local execution mitigates high security concerns.
- **Reasoning Order Rules**: Strict compliance with OpenRouter/Anthropic's sequence requirements (re-sending reasoning blocks unmodified) is critical to prevent turn execution failures.

---

## 11. Milestones & Progress Tracker
1. **Skeleton** ✅ FastAPI + React Flow + SQLite, manually-built workflows, can run a hand-coded node graph end-to-end with `call_llm` and one tool.
2. **Persistence & REST APIs** ✅ persistent database tables for workflows, nodes, edges, runs, settings, sessions/messages.
3. **Manual Builder Workbench** ✅ visual canvas, Monaco editor drawers, port inspector, run console with inputs form.
4. **Active Event Streaming** ✅ WebSockets, real-time node state changes, cancel support.
5. **Orchestrator Agent v1** ✅ SSE chat stream, collapsible reasoning logs, tool call cards, and `user_edited` locks.
6. **Orchestrator-Driven Collaboration** ✅ Orchestrator can run graphs, diagnose trace errors, clear board between stage solves, and fork snapshots into fresh active projects.
7. **Generative UI / Designer Agent** ⏳ Dedicated Design tab allowing a specialized designer agent to generate tailor-made UI pages for running stable agent teams.
8. **Distribution Polish** ⏳ Run pruning and single-process packaging integration.
