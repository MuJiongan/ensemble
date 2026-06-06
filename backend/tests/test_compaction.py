"""Unit tests for context compaction — core module + orchestrator wiring.
No API keys / no real LLM calls."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import compaction, models
from app.auth import codex_api
from app.catalog.models_dev import CatalogModel, ModelLimit
from app.db import Base
from app.orchestrator import agent as orch_agent
from app.orchestrator.agent import persistence as persist


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def session_row(db):
    w = models.Workflow(name="wf")
    db.add(w)
    db.commit()
    db.refresh(w)
    s = models.Session(workflow_id=w.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _turns(n: int) -> list[dict]:
    """n user/assistant turn pairs."""
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"user message {i}"})
        out.append({"role": "assistant", "content": f"assistant reply {i}"})
    return out


# ---------------------------------------------------------------------------
# overflow detection
# ---------------------------------------------------------------------------


def test_usable_subtracts_reserve_from_input_limit():
    # input_limit present → usable = input_limit - reserved(=min(BUFFER, output))
    u = compaction.usable(context=200_000, output_limit=8_000, input_limit=190_000)
    assert u == 190_000 - 8_000


def test_usable_derives_from_context_when_no_input_limit():
    u = compaction.usable(context=100_000, output_limit=4_000, input_limit=None)
    assert u == 100_000 - 4_000


def test_usable_caps_reserve_at_buffer():
    # A huge output limit is capped at COMPACTION_BUFFER for the reserve.
    u = compaction.usable(context=500_000, output_limit=100_000, input_limit=400_000)
    assert u == 400_000 - compaction.COMPACTION_BUFFER


def test_is_overflow_true_at_or_above_budget():
    assert compaction.is_overflow(token_count=192_000, context=200_000, input_limit=190_000)
    assert not compaction.is_overflow(token_count=10_000, context=200_000, input_limit=190_000)


def test_unknown_context_never_overflows():
    assert not compaction.is_overflow(token_count=10**9, context=0)


def test_estimate_scales_with_length():
    assert compaction.estimate_tokens("") == 0
    assert compaction.estimate_tokens("x" * 40) == 10
    assert compaction.estimate_messages([{"role": "user", "content": "hi"}]) > 0


# ---------------------------------------------------------------------------
# tail selection
# ---------------------------------------------------------------------------


def test_select_tail_keeps_recent_turns():
    msgs = _turns(4)  # 4 user turns at indices 0,2,4,6
    # Generous budget; keep the last 2 turns → tail starts at the 3rd turn (idx 4).
    idx = compaction.select_tail(msgs, context=200_000, output_limit=8_000, tail_turns=2)
    assert idx == 4
    assert msgs[idx]["content"] == "user message 2"


def test_select_tail_returns_zero_for_small_history():
    msgs = _turns(2)
    # Only 2 turns and tail_turns=2 → keeping them means keep everything → 0.
    assert compaction.select_tail(msgs, context=200_000, tail_turns=2) == 0


def test_select_tail_disabled_when_tail_turns_zero():
    assert compaction.select_tail(_turns(5), context=200_000, tail_turns=0) == 0


# ---------------------------------------------------------------------------
# compaction
# ---------------------------------------------------------------------------


def test_compact_messages_summarizes_head_keeps_tail_and_system():
    msgs = [{"role": "system", "content": "sys prompt"}, *_turns(4)]
    seen = {}

    def fake_summarize(head, prompt):
        seen["head"] = head
        seen["prompt"] = prompt
        return "ANCHORED SUMMARY"

    res = compaction.compact_messages(
        msgs, summarize=fake_summarize, context=200_000, output_limit=8_000, tail_turns=2
    )
    assert res is not None
    new = res["messages"]
    # Leading system preserved, summary inserted, tail (last 2 turns) verbatim.
    assert new[0] == {"role": "system", "content": "sys prompt"}
    assert new[1]["content"].startswith(compaction.SUMMARY_PREFIX)
    assert "ANCHORED SUMMARY" in new[1]["content"]
    assert new[-4:] == _turns(4)[-4:]
    # The callback received the head (system stripped) + a templated prompt.
    assert seen["prompt"].count("## Goal") == 1
    assert all(m["role"] != "system" for m in seen["head"])
    assert res["summarized"] == len(seen["head"])


def test_compact_messages_noop_when_head_too_small():
    msgs = [{"role": "system", "content": "s"}, *_turns(2)]
    called = []
    res = compaction.compact_messages(
        msgs, summarize=lambda h, p: called.append(1) or "x", context=200_000, tail_turns=2
    )
    assert res is None
    assert not called  # never summarized


def test_compact_messages_incremental_anchor_passes_previous_summary():
    prior = compaction.summary_message("OLD SUMMARY")
    msgs = [{"role": "system", "content": "sys"}, prior, *_turns(4)]
    captured = {}

    def fake_summarize(head, prompt):
        captured["prompt"] = prompt
        return "NEW SUMMARY"

    res = compaction.compact_messages(
        msgs, summarize=fake_summarize, context=200_000, tail_turns=2
    )
    assert res is not None
    # The prior anchor is fed in for an incremental update and dropped from the
    # rebuilt list (replaced by the single new anchor).
    assert "OLD SUMMARY" in captured["prompt"]
    anchors = [m for m in res["messages"] if str(m.get("content", "")).startswith(compaction.SUMMARY_PREFIX)]
    assert len(anchors) == 1
    assert "NEW SUMMARY" in anchors[0]["content"]


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def test_prune_blanks_old_tool_outputs_beyond_protect():
    big = "x" * 4_000  # ~1000 tokens each
    msgs: list[dict] = []
    for i in range(60):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": big})
    pruned = compaction.prune_messages(msgs, protect=10_000, minimum=5_000)
    assert pruned > 0
    # Oldest tool outputs got blanked; the most recent stay intact.
    assert msgs[1]["content"] == compaction.TOOL_OUTPUT_PRUNED
    assert msgs[-1]["content"] == big


def test_prune_noop_when_below_minimum():
    msgs = [
        {"role": "tool", "tool_call_id": "a", "content": "x" * 4_000},
        {"role": "tool", "tool_call_id": "b", "content": "x" * 4_000},
    ]
    # protect window already covers everything → nothing to free.
    assert compaction.prune_messages(msgs, protect=10_000, minimum=5_000) == 0
    assert all(m["content"] != compaction.TOOL_OUTPUT_PRUNED for m in msgs)


# ---------------------------------------------------------------------------
# orchestrator persistence: history rebuild after a compaction anchor
# ---------------------------------------------------------------------------


def test_history_rebuild_replaces_head_with_summary_keeps_tail(db, session_row):
    sid = session_row.id
    # Build a history: u0, a0, u1, a1, then compact keeping the tail at u1.
    db.add(models.Message(session_id=sid, role="user", content="first"))
    db.add(models.Message(session_id=sid, role="assistant", content="reply one"))
    tail_user = models.Message(session_id=sid, role="user", content="second")
    db.add(tail_user)
    db.add(models.Message(session_id=sid, role="assistant", content="reply two"))
    db.commit()
    db.refresh(tail_user)

    persist._persist_compaction(db, sid, "THE SUMMARY", tail_start_id=tail_user.id)

    history = persist._history_messages(db, sid)
    # First message is the anchor; head (first/reply one) is gone; tail kept.
    assert history[0]["role"] == "system"
    assert "THE SUMMARY" in history[0]["content"]
    contents = [m["content"] for m in history[1:]]
    assert contents == ["second", "reply two"]
    assert "first" not in contents


def test_history_rebuild_with_no_tail_keeps_only_summary(db, session_row):
    sid = session_row.id
    db.add(models.Message(session_id=sid, role="user", content="hi"))
    db.add(models.Message(session_id=sid, role="assistant", content="yo"))
    db.commit()
    persist._persist_compaction(db, sid, "SUM", tail_start_id=None)

    history = persist._history_messages(db, sid)
    assert len(history) == 1
    assert "SUM" in history[0]["content"]


def test_render_history_ignores_compaction_marker(db, session_row):
    """The user-facing chat still shows the full conversation — the marker is
    a model-context concern only."""
    sid = session_row.id
    db.add(models.Message(session_id=sid, role="user", content="hello"))
    db.add(models.Message(session_id=sid, role="assistant", content="hi there"))
    db.commit()
    persist._persist_compaction(db, sid, "SUMMARY", tail_start_id=None)

    bubbles = orch_agent.render_history(db, sid)
    roles = [b["role"] for b in bubbles]
    assert roles == ["user", "assistant"]
    assert all("SUMMARY" not in str(b) for b in bubbles)


# ---------------------------------------------------------------------------
# orchestrator trigger: _maybe_compact persists an anchor on overflow
# ---------------------------------------------------------------------------


def test_maybe_compact_triggers_and_persists_anchor(db, session_row, monkeypatch):
    sid = session_row.id
    for i in range(4):
        db.add(models.Message(session_id=sid, role="user", content=f"u{i}"))
        db.add(models.Message(session_id=sid, role="assistant", content=f"a{i}"))
    db.commit()

    # A small-context model so the token count trips overflow.
    fake_model = CatalogModel(
        id="m", name="m", provider_id="p", api_id="m", npm="@ai-sdk/openai-compatible", api_url="",
        limit=ModelLimit(context=1000, output=100, input=900),
    )
    monkeypatch.setattr("app.catalog.models_dev.get_model", lambda p, m: fake_model)
    monkeypatch.setenv("LLM_PROVIDER_ID", "p")

    def fake_stream(db_, model, messages, tool_specs, cancel_event=None):
        yield ("done", {"message": {"role": "assistant", "content": "GEN SUMMARY"}, "usage": {}})

    monkeypatch.setattr(orch_agent, "_resolve_llm_stream", fake_stream)

    did = orch_agent._maybe_compact(db, sid, "m", {"prompt_tokens": 950, "completion_tokens": 100})
    assert did is True

    # An anchor row now exists, and the rebuilt history leads with the summary.
    markers = [r for r in persist._ordered_rows(db, sid) if persist._is_compaction_marker(r)]
    assert len(markers) == 1
    assert "GEN SUMMARY" in markers[0].content
    history = persist._history_messages(db, sid)
    assert "GEN SUMMARY" in history[0]["content"]


def test_maybe_compact_skips_when_under_budget(db, session_row, monkeypatch):
    sid = session_row.id
    db.add(models.Message(session_id=sid, role="user", content="u"))
    db.add(models.Message(session_id=sid, role="assistant", content="a"))
    db.commit()

    fake_model = CatalogModel(
        id="m", name="m", provider_id="p", api_id="m", npm="@ai-sdk/openai-compatible", api_url="",
        limit=ModelLimit(context=200_000, output=8_000, input=190_000),
    )
    monkeypatch.setattr("app.catalog.models_dev.get_model", lambda p, m: fake_model)
    monkeypatch.setenv("LLM_PROVIDER_ID", "p")

    did = orch_agent._maybe_compact(db, sid, "m", {"prompt_tokens": 100, "completion_tokens": 10})
    assert did is False
    assert not [r for r in persist._ordered_rows(db, sid) if persist._is_compaction_marker(r)]


# ---------------------------------------------------------------------------
# per-node runner: Codex (Responses API) loop compacts like the standard loop
# ---------------------------------------------------------------------------


def test_codex_chat_compacts_on_overflow(monkeypatch):
    """The Codex node-runtime loop runs the same prune/compact hook as the
    standard runner — a long Codex conversation gets an anchor spliced in."""
    monkeypatch.setenv("LLM_PROVIDER_ID", "codex")

    # A prior multi-turn conversation so there's a head worth summarizing.
    prompt: list[dict] = []
    for i in range(4):
        prompt.append({"role": "user", "content": f"u{i}"})
        prompt.append({"role": "assistant", "content": f"a{i}"})

    registry = {"noop": lambda **k: {"ok": True}}
    schemas = {"noop": {"type": "function", "function": {"name": "noop", "parameters": {}}}}

    # Round 1: a tool call with usage that trips overflow (codex usable ~272k).
    # Round 2: no tool calls → the loop exits.
    rounds = iter([
        [("done", {"message": {"role": "assistant", "content": "working", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "noop", "arguments": "{}"}}
        ]}, "usage": {"prompt_tokens": 300_000, "completion_tokens": 50}})],
        [("done", {"message": {"role": "assistant", "content": "done"},
                   "usage": {"prompt_tokens": 100, "completion_tokens": 10}})],
    ])

    def fake_stream(model, messages, tool_schemas, access_token, account_id, reasoning_effort=None, **k):
        last = messages[-1]
        if isinstance(last.get("content"), str) and "## Goal" in last["content"]:
            return iter([("done", {"message": {"role": "assistant", "content": "CODEX SUMMARY"}, "usage": {}})])
        return iter(next(rounds))

    monkeypatch.setattr(codex_api, "call_codex_stream", fake_stream)

    res = codex_api.call_codex_chat(
        model="gpt-5.3-codex", prompt=prompt, tools=["noop"],
        tool_registry=registry, tool_schemas_by_name=schemas,
        on_event=None, call_id=None, access_token="t", account_id=None,
    )
    msgs = res["messages"]
    anchors = [m for m in msgs if isinstance(m.get("content"), str) and m["content"].startswith(compaction.SUMMARY_PREFIX)]
    assert len(anchors) == 1
    assert "CODEX SUMMARY" in anchors[0]["content"]
    # The oldest turn was summarized away (no standalone u0 left).
    assert not any(m.get("content") == "u0" for m in msgs)


def test_codex_chat_unknown_model_does_not_compact(monkeypatch):
    """A model absent from the catalog → context 0 → loop runs unchanged."""
    monkeypatch.setenv("LLM_PROVIDER_ID", "codex")
    prompt = [{"role": "user", "content": "hi"}]

    def fake_stream(model, messages, tool_schemas, access_token, account_id, reasoning_effort=None, **k):
        return iter([("done", {"message": {"role": "assistant", "content": "ok"},
                               "usage": {"prompt_tokens": 10**9, "completion_tokens": 0}})])

    monkeypatch.setattr(codex_api, "call_codex_stream", fake_stream)

    res = codex_api.call_codex_chat(
        model="totally-unknown-model", prompt=prompt, tools=[],
        tool_registry={}, tool_schemas_by_name={},
        on_event=None, call_id=None, access_token="t", account_id=None,
    )
    assert not any(
        isinstance(m.get("content"), str) and m["content"].startswith(compaction.SUMMARY_PREFIX)
        for m in res["messages"]
    )
