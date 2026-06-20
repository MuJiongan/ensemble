# Ensemble as a Custom Agent Harness — Vision & Roadmap

> Status: brainstorm / direction-setting. Not a commitment, a map.
> Grounded in the codebase as of this branch — file paths and table names below are real.

## The reframe

Today **Ensemble** is a *workflow builder*: the orchestrator's entire job is to emit topology
mutations (`add_node`, `configure_node`, `add_edge` in `backend/app/orchestrator/tools.py`), and
the artifact is a DAG that ran once.

The vision is bigger — a **custom agent harness for a task the user describes**. The unit of value
stops being "a graph that ran once" and becomes **a reusable, composable, packaged agent system
with execution semantics you control.**

That's three layers:

| Layer | What it is | State today |
| --- | --- | --- |
| **Authoring** | The orchestrator designs the team from a natural-language goal | **Strong** |
| **Execution semantics** | The loop, hooks, compaction, LLM primitives — *how* agents run | **The harness layer we want to deepen** |
| **Distribution** | Packaging, subworkflows, a library of reusable workflows | **The missing third leg** |

**The throughline for everything below:** the orchestrator should be able to program the
*execution semantics and the reusable assets*, not just the topology — while preserving the
invariants the codebase is deliberately built on (see [Invariants](#invariants-to-preserve)).

---

## Where we are today (grounded)

A local, single-user app. An **orchestrator agent** (chat-driven LLM) designs a **DAG of Python
agent nodes**; the **runtime** executes the graph in an isolated subprocess.

- **Node** = `run(inputs, ctx)` Python with its own model + tools. **Edge** = wires one node's
  named output to another's named input. Branching is **null-propagation**, not control flow —
  no cyclic or conditional edges; dynamic loops live *inside* one node.
- **Node tools** = a fixed `REGISTRY` of 6 built-ins (`shell`, `read_file`, `write_file`,
  `edit_file`, `web_search`, `web_fetch`) + MCP-discovered tools. Reached two ways:
  *agentic* (`ctx.call_llm(tools=[...])`) or *direct* (`ctx.tools.X(...)`).
- **One LLM primitive:** `ctx.call_llm(model, prompt, tools)` → `{content, messages,
  tool_calls_made, usage, cost}`.
- **Multi-protocol transport** (`backend/app/llm/`): native adapters for Anthropic Messages,
  Gemini, OpenAI Responses, OpenAI Chat, behind one uniform event contract.
- **Compaction** (`backend/app/compaction.py`): summarize the head into a structured anchor, keep
  the tail verbatim, prune stale tool outputs. Shared by the orchestrator loop and the per-node
  loop.
- **Persistence/UX:** SQLite (10 tables), run snapshots, rerun-from-snapshot, JSON import/export,
  live event streaming, MCP with OAuth.

The design has strong opinions — *pull-not-push*, *orchestrator plans / doesn't do*, *acyclic
graph*, *isolated subprocess* — that the workstreams below must respect.

---

## Workstream 1 — Tool creation ("create tools when necessary")

**Today:** the node `REGISTRY` is fixed + MCP. The orchestrator writes node *code* but can't mint a
named, reusable *tool*. The "research node" pattern in the system prompt is this instinct already —
a probe you build, run, and discard — but with no durable artifact.

**Moves:**

- **Synthesized tools as first-class objects.** Let the orchestrator define a named, schema'd,
  reusable callable (new `tools` table; JSON input schema — reuse the MCP schema machinery). Once
  defined it lands in the project's `REGISTRY`, callable both ways the dual surface already
  supports: `ctx.tools.my_tool(...)` and named in `call_llm(tools=[...])`.
- **Tool-from-workflow.** A workflow is *already* a typed single-in/single-out function. Expose
  any workflow as a callable tool — the hinge between this workstream and
  [Packaging](#workstream-5--packaging--reuse-reusable-outside-its-project).
- **Policy wrappers** — caching/memoization (deterministic tools like `web_fetch` benefit),
  approval-gating, rate limits, sandbox level. This also closes a gap the README admits today:
  *no per-call MCP approval prompts*.
- **Tools as behavioral signals.** A `critique` / `verify` / `extract_schema` tool that is "just a
  wrapped `call_llm` + prompt" is still worth minting — the **name** changes *when* the orchestrator
  reaches for the pattern, independent of what it wraps.

---

## Workstream 2 — Compaction ("its own LLM compaction logic")

**Today:** `compaction.py` is genuinely good — structured anchor template, incremental-anchor merge,
tail-by-token-budget, attachment-aware prune, transport-agnostic, shared by both loops. So this is
*refinement, not invention*.

**Moves:**

- **Pluggable strategy per node / loop.** A `compaction` policy field: aggressive-prune for a long
  file-grinding node, semantic-memory for a research node, none for short deterministic ones.
- **`ctx.memory` — externalized recall.** Today a pruned tool output is *gone*. Spill old turns to a
  retrievable store (each run already gets a `ctx.workdir`) so the loop can *recall* a pruned result
  instead of losing it. This is the compaction × primitives intersection.
- **Compaction as a hook** (→ Workstream 4) so an author can say "always preserve rows matching this
  regex / this output port."

---

## Workstream 3 — LLM primitives ("more primitives about the LLM")

**Today:** exactly one primitive (`ctx.call_llm`). The multi-protocol transport is already built —
these are mostly *thin, well-named surfaces over it*.

**Moves (roughly in leverage order):**

- **Structured output — `call_llm(..., schema=...)`.** *Highest-leverage single item on this whole
  list.* Half of all node code is "ask the model, then parse/validate." Force a JSON schema, return
  the parsed object, retry on mismatch. All four adapters support it natively. Also unblocks
  hook-based repair and [typed ports](#workstream-6--natural-extensions).
- **`ctx.sample(prompt, n)` + `ctx.judge(...)` / `ctx.vote(...)`.** Make the judge-panel and
  adversarial-verify patterns first-class primitives instead of hand-rolled `ThreadPoolExecutor`.
- **`ctx.classify(text, labels)` / `ctx.embed(texts)`.** Embeddings unlock semantic dedup,
  clustering, and RAG nodes — a class of workflow that's awkward today.
- **`ctx.spawn(...)`** — an in-node child agent loop with its *own* context/compaction/budget,
  returning only a result. The fractal version of the whole app; keeps pull-not-push *inside* a node.
- **Budgets** — `max_cost` / `max_tokens` per call and per run. The README's "a runaway loop is a
  cancel button, not a turn cap" is a deliberate stance; a *budget* is the author-controllable
  middle ground the harness enforces gracefully.

---

## Workstream 4 — Loop control & hooks ("more control over the agentic loops")

**The heart of "harness," and the biggest behavioral differentiator.** Neither loop has hooks today;
both run until the model stops calling tools, stopped only by cancel.

- **Lifecycle events to hook:** `pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`,
  `on_iteration`, `on_compaction`, `on_node_start` / `on_node_end`, `on_run_start` / `on_run_end`,
  `on_error`.
- **Hook powers:**
  - *observe* — metrics, logging, tracing
  - *mutate* — rewrite a tool call's args, inject a system reminder
  - *gate* — approve/block a tool call (fills the missing MCP-approval gap)
  - *short-circuit* — return a cache hit instead of calling
  - *repair* — on schema-mismatch or error, re-prompt or run a one-node fixer workflow
- **A hook is just Python** (consistent with the node-code model) — or itself a sub-agent. Scoped
  per-node, per-workflow, and per-project.
- **Loop policies beyond "until model stops":** max-iterations, until-predicate, until-budget,
  **until-N-consecutive-empty** (loop-until-dry), force-tool-first, require-tool-before-finishing.
  Make the stop condition a *declarative field the harness enforces* — not something begged for in
  the prompt.

**Keep the README's proud distinction:** hooks add iteration *inside* a node loop; they do **not**
add cycles to the graph.

**Where it threads in:** the orchestrator turn loop (`backend/app/orchestrator/agent/`) and the
node `call_llm` loop (`backend/app/runner/llm.py`).

---

## Workstream 5 — Packaging & reuse ("reusable outside its project")

**The most concrete ask and the biggest architectural leap.** Today: JSON import/export with
regenerated IDs, and **no nesting**.

- **Subworkflow node.** A node whose body *is* another workflow — its input node = the node's
  inputs, its output node = the node's outputs. Clean, because a workflow is already a typed
  single-in/single-out function.
  - **Decision:** *flatten* the sub-DAG into the parent snapshot at freeze time (simpler; preserves
    the "one frozen snapshot" property) **vs.** *recursive scheduling* (independent
    compaction/budget per subworkflow; more power). **Recommendation: start with flatten.**
- **Local workflow library** (`~/.ensemble/library/`). Each entry = the existing portable JSON
  bundle + a **capability manifest**: required node tools, MCP servers, model roles, keys. On
  import, check/ask for what's missing — *reuse the MCP `needs_auth` probe that already exists.*
- **Orchestrator recruits from the library.** New tools: `search_library(query)` /
  `import_subworkflow(id)`. When the user describes a new problem, the orchestrator pulls a
  published workflow as a sub-node — *exactly the original example.* The deepest version is a
  "reuse before build" discipline that mirrors the existing "research before build" one.
- **Parameterization + versioning.** Typed params so a published workflow specializes on import; a
  subworkflow node pins a version. Snapshots already freeze code, so reproducibility is nearly free.

---

## Workstream 6 — Natural extensions (not originally asked, but they follow)

- **Eval & regression.** Once workflows are durable assets, make them *testable*. The
  snapshot + rerun machinery already exists — add expected-output assertions and a workflow becomes
  a regression test of itself.
- **Triggers / headless run.** A harness runs workflows beyond chat — cron, webhook, file-watch, a
  CLI/API entrypoint. That turns a built workflow into a *deployable agent*.
- **Typed contracts.** Port `type_hint` is advisory today; make it an enforced schema (same
  machinery as structured output) so edges type-check and bad data fails *at the seam*, not three
  nodes downstream.
- **Replay / observability.** `NodeRun` already captures llm_calls / tool_calls / logs / duration /
  cost — record tool *outputs* in the snapshot so a rerun can *replay without re-calling the world*.
  Huge for debugging and eval.

---

## Invariants to preserve

These are deliberate, and several asks above push on them. Decide consciously where they bend.

1. **Pull-not-push.** The orchestrator never auto-ingests run output; it pulls just what it needs.
   New primitives and hooks must *summarize for* the orchestrator, never dump full output into its
   context.
2. **Orchestrator plans; it doesn't do.** Tool-creation and hook-authoring stay *design* actions —
   the orchestrator writes the tool/hook code; the runtime executes it. Don't let it start firing
   tools itself.
3. **Acyclic graph.** Subworkflows must stay acyclic (a workflow can't include itself). Loop-control
   hooks add iteration *within a node*, not cycles in the graph.
4. **Isolation is not a sandbox.** Node code runs arbitrary Python in a subprocess that is *not* a
   security boundary. "Reusable outside its project" is fine on one machine; **"reusable from
   *someone else*" is a security step-change** — the distribution story needs a trust/sandbox answer
   before third-party workflows execute locally.

---

## Suggested arc (what unlocks what)

1. **Structured-output primitive** — small, high-leverage, unblocks the rest.
2. **Enforced typed ports** — makes workflows real functions.
3. **Subworkflow node** — the #1 ask; needs typed ports; flatten first.
4. **Local library + `recruit` tool** — needs subworkflows + capability manifest.
5. **Hooks** — parallel track; start with `pre`/`post_tool_call` + `on_error`/approval-gate.
6. **Loop policies + budgets** — build on hooks.
7. **Pluggable compaction + `ctx.memory`** — refinement once the above exist.
8. **Triggers / eval / replay** — productionize workflows into deployable, trustworthy assets.

---

## Open decisions

- Subworkflow execution: **flatten vs. recursive** (leaning flatten).
- Hook contract: **plain Python callable vs. sub-agent** (likely both, Python first).
- Library scope: **local-only first**, defer cross-machine sharing until the trust story exists.
- Whether tools and hooks become **canvas-visible objects** or stay code-level config.
