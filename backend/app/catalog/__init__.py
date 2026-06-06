"""LLM provider/model catalog.

A Python port of opencode's provider architecture:

  * ``models_dev`` fetches and caches the models.dev catalog (the source of
    truth for which providers/models exist and their capabilities).
  * ``variants`` computes per-model reasoning "variants" (low/medium/high/max,
    etc.) at runtime — a faithful port of opencode's ``transform.ts`` ``variants()``.
  * ``providers`` derives the connectable provider list + auth methods.

Execution (turning a ``{providerID, modelID, variant}`` selection into a live
streamed call) lives in :mod:`app.llm`, which routes to the right native
protocol and applies the variant.
"""
