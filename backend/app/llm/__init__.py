"""Native multi-protocol LLM layer.

A Python port of opencode's `packages/llm` protocol/route split, scoped to
key-based providers. Every adapter exposes a uniform ``stream_round(...)`` that:

  * takes messages in gorchestra's chat-completions shape (role/content/
    tool_calls, tool results as role="tool"),
  * translates them to the provider's native request body,
  * streams the response, and
  * yields gorchestra's existing event tuples:
        ("text", str)
        ("thinking", str)
        ("tool_args", tc_index, name_so_far, args_delta)
        ("done", {"message": <assistant msg in chat shape>, "usage": {...}})

Because every adapter speaks the same in/out contract, the agent loops in
``runner/llm.py`` and the orchestrator stay protocol-agnostic — they just pick a
``stream_round`` via :func:`app.llm.router.select` and keep their tool-calling
logic unchanged. Each adapter owns its own reasoning-variant translation
(OpenAI ``reasoning_effort``, Anthropic ``thinking`` budget, Gemini
``thinkingConfig``).

Transport rides the official provider SDKs (``openai``, ``anthropic``,
``google-genai``); the lowering and parsing stay ours. Adapters build
wire-shaped payloads (non-standard fields via ``extra_body``) and feed
``model_dump()``-ed stream events to their parsers, so the translation layer
is SDK-agnostic and unit-testable without a network.
"""
