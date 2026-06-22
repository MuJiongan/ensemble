"""Continue-chat (call_llm continuation) API + persistence.

Covers create-or-get idempotency, the no-seed/missing-call 404s, the model the
continuation pins (provider/model/variant), the lean run-response transform, the
seed size guard, attachment stripping, the persisted-transcript cap, and that
sending a turn appends + persists the user message before spawning the (stubbed)
turn subprocess.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sqlalchemy import text as sa_text

from app.db import Base, _ensure_callchat_unique_index
from app import models, schemas
from app.api import call_chats as cc_api
from app.api import runs as runs_api
from app.runner import events as ev_mod
from app.runner.ctx import _within_seed_budget, _strip_message_attachments
from app.runner.chat import (
    _cap_transcript,
    _merge_consecutive_users,
    _normalize_head,
    prepare_transcript,
    _TRANSCRIPT_BUDGET_BYTES,
)


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


SEED_MESSAGES = [
    {"role": "user", "content": "summarize this"},
    {"role": "assistant", "content": "Here is the summary."},
]


def _seed_node_run(db, *, messages=SEED_MESSAGES, call_id="call-1") -> str:
    """Seed a Workflow + Run (with snapshot) + NodeRun carrying one llm call."""
    wf = models.Workflow(name="wf")
    db.add(wf)
    db.commit()
    db.refresh(wf)
    node_id = "node-abc"
    run = models.Run(
        workflow_id=wf.id,
        status="success",
        inputs={},
        workflow_snapshot={"nodes": [{"id": node_id, "name": "extract"}], "edges": []},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    nr = models.NodeRun(
        run_id=run.id,
        node_id=node_id,
        status="success",
        llm_calls=[{
            "call_id": call_id,
            "model": "anthropic/claude-sonnet-4.5",
            "provider_id": "anthropic",
            "variant": "high",
            "tools": ["web_search"],
            "content": "Here is the summary.",
            "tool_calls_made": [],
            "usage": {},
            "cost": 0.0,
            "messages": messages,
        }],
    )
    db.add(nr)
    db.commit()
    db.refresh(nr)
    return nr.id


def test_open_call_chat_creates_and_is_idempotent(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)

    first = cc_api.open_call_chat(nrid, "call-1", db=db)
    assert first.messages == SEED_MESSAGES
    assert first.model == "anthropic/claude-sonnet-4.5"
    # The fork pins the provider + reasoning variant the call ran with, so it
    # stays on the same model regardless of the current node default.
    assert first.provider_id == "anthropic"
    assert first.variant == "high"
    assert first.tools == ["web_search"]
    assert first.label == "extract · call 1"

    # Opening the same call again returns the same row, not a duplicate.
    second = cc_api.open_call_chat(nrid, "call-1", db=db)
    assert second.id == first.id
    assert db.query(models.CallChat).count() == 1


def test_open_call_chat_404_when_no_seed(db_factory):
    db = db_factory()
    # Over-budget / pre-feature calls persist messages as None → not continuable.
    nrid = _seed_node_run(db, messages=None)
    with pytest.raises(HTTPException) as exc:
        cc_api.open_call_chat(nrid, "call-1", db=db)
    assert exc.value.status_code == 404


def test_open_call_chat_uses_call_label_when_set(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)
    nr = db.get(models.NodeRun, nrid)
    nr.llm_calls = [{**nr.llm_calls[0], "label": "summarise item 3"}]
    db.commit()

    chat = cc_api.open_call_chat(nrid, "call-1", db=db)
    assert chat.label == "extract · summarise item 3"


def test_open_call_chat_404_when_call_missing(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)
    with pytest.raises(HTTPException) as exc:
        cc_api.open_call_chat(nrid, "no-such-call", db=db)
    assert exc.value.status_code == 404


def test_lean_llm_calls_strips_messages_adds_flag():
    lean = runs_api._lean_llm_calls([
        {"call_id": "c1", "content": "x", "messages": SEED_MESSAGES},
        {"call_id": "c2", "content": "y", "messages": None},
    ])
    # Full transcript never ships in the run payload — only a continuable flag.
    assert all("messages" not in c for c in lean)
    assert lean[0]["has_chat"] is True
    assert lean[1]["has_chat"] is False
    assert lean[0]["content"] == "x"


def test_within_seed_budget():
    assert _within_seed_budget([]) is False
    assert _within_seed_budget(SEED_MESSAGES) is True
    huge = [{"role": "user", "content": "x" * (300 * 1024)}]
    assert _within_seed_budget(huge) is False


def test_strip_message_attachments_drops_base64_riders():
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": '{"note": "1 image (12 KB)"}',
            "attachments": [{"data": "QUJD" * 1000}],
        },
    ]
    out = _strip_message_attachments(msgs)
    # The base64 attachment rider is gone; the text-only fields survive.
    assert "attachments" not in out[1]
    assert out[1]["content"] == '{"note": "1 image (12 KB)"}'
    assert out[0] == {"role": "user", "content": "hi"}
    # The input isn't mutated — only a copy of the attachment-bearing message.
    assert "attachments" in msgs[1]


def test_cap_transcript_keeps_under_budget_unchanged():
    assert _cap_transcript(SEED_MESSAGES) == SEED_MESSAGES


def test_cap_transcript_trims_oldest_turns_at_user_boundaries():
    big = "x" * (200 * 1024)
    msgs = []
    for i in range(6):
        msgs += [
            {"role": "user", "content": f"turn {i}"},
            {"role": "assistant", "content": big},
        ]
    capped = _cap_transcript(msgs)
    # Under budget, never cut inside a turn (first kept msg is a user message),
    # the most recent turn is retained, and the oldest turns were dropped.
    assert len(json.dumps(capped)) <= _TRANSCRIPT_BUDGET_BYTES
    assert capped[0]["role"] == "user"
    assert {"role": "user", "content": "turn 5"} in capped
    assert {"role": "user", "content": "turn 0"} not in capped


def test_cap_transcript_preserves_leading_system_prefix():
    big = "x" * (300 * 1024)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn 0"},
        {"role": "assistant", "content": big},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": big},
    ]
    capped = _cap_transcript(msgs)
    assert capped[0] == {"role": "system", "content": "sys"}
    assert len(json.dumps(capped)) <= _TRANSCRIPT_BUDGET_BYTES


def test_cap_transcript_single_oversized_turn_kept_whole():
    # One turn can't be trimmed at a boundary without orphaning tool results —
    # persist it whole rather than corrupt the assistant/tool pairing.
    msgs = [
        {"role": "user", "content": "only turn"},
        {"role": "assistant", "content": "x" * (600 * 1024)},
    ]
    assert _cap_transcript(msgs) == msgs


def test_send_turn_appends_user_message_and_persists(db_factory, monkeypatch):
    db = db_factory()
    nrid = _seed_node_run(db)
    chat = cc_api.open_call_chat(nrid, "call-1", db=db)

    captured = {}

    def fake_start(turn_id, chat_id, messages, tools, model, child_env):
        captured.update(
            turn_id=turn_id, chat_id=chat_id, messages=messages, tools=tools, model=model
        )

    monkeypatch.setattr(cc_api.chat_service, "start_chat_turn", fake_start)
    monkeypatch.setattr(cc_api, "build_child_env", lambda: {})

    out = cc_api.send_call_chat_turn(
        chat.id, schemas.CallChatTurnIn(text="now expand point 2"), db=db
    )
    assert out.turn_id.startswith("turn-")

    # The user turn is persisted immediately (reload-safe) and handed to the
    # turn runner appended to the seed conversation.
    row = db.get(models.CallChat, chat.id)
    assert row.messages[-1] == {"role": "user", "content": "now expand point 2"}
    assert captured["chat_id"] == chat.id
    assert captured["model"] == "anthropic/claude-sonnet-4.5"
    assert captured["messages"][-1]["content"] == "now expand point 2"
    assert captured["tools"] == ["web_search"]


def test_send_turn_uses_switched_model_and_persists_selection(db_factory, monkeypatch):
    """Switching the model must send the *new* model name, not the recorded one
    — otherwise the turn pairs the new provider with the old model id."""
    db = db_factory()
    nrid = _seed_node_run(db)  # recorded: anthropic / claude-sonnet-4.5 / high
    chat = cc_api.open_call_chat(nrid, "call-1", db=db)

    captured = {}

    def fake_start(turn_id, chat_id, messages, tools, model, child_env):
        captured.update(model=model)

    monkeypatch.setattr(cc_api.chat_service, "start_chat_turn", fake_start)
    monkeypatch.setattr(cc_api, "build_child_env", lambda: {})
    # The frontend sends the switched provider/variant via X-Node-* headers,
    # which the middleware has applied to process env by the time this runs.
    monkeypatch.setenv("NODE_PROVIDER_ID", "openai")
    monkeypatch.setenv("DEFAULT_NODE_VARIANT", "")

    cc_api.send_call_chat_turn(
        chat.id, schemas.CallChatTurnIn(text="hi", model="openai/gpt-4o"), db=db
    )

    # The turn uses the switched model, and the fork remembers the new selection
    # (so a reopen/reload stays on the switched provider+model, not the recorded).
    assert captured["model"] == "openai/gpt-4o"
    row = db.get(models.CallChat, chat.id)
    assert row.model == "openai/gpt-4o"
    assert row.provider_id == "openai"
    assert row.variant == ""


# --- transcript sanitization (normalize head + merge consecutive users) ----


def test_normalize_head_splices_leading_non_system():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "{}"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    out = _normalize_head(msgs)
    # The leading assistant/tool head (which strict providers reject as a first
    # message / orphaned tool_result) is dropped; the conversation starts at the
    # first user message.
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_normalize_head_keeps_system_then_user():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert _normalize_head(msgs) == msgs


def test_normalize_head_drops_non_system_between_system_and_user():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "t1", "content": "{}"},
        {"role": "user", "content": "u"},
    ]
    assert _normalize_head(msgs) == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


def test_normalize_head_no_user_keeps_system_only():
    msgs = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "a"}]
    assert _normalize_head(msgs) == [{"role": "system", "content": "s"}]


def test_merge_consecutive_users_folds_string_pairs():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "r"},
        {"role": "user", "content": "c"},
    ]
    out = _merge_consecutive_users(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == "a\n\nb"
    assert out[-1]["content"] == "c"


def test_merge_consecutive_users_leaves_non_string_content():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "user", "content": "b"},
    ]
    # Non-string content isn't merged (can't concatenate) — both kept.
    assert len(_merge_consecutive_users(msgs)) == 2


def test_prepare_transcript_is_idempotent():
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": "{}"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ]
    once = prepare_transcript(msgs)
    assert once == prepare_transcript(once)
    assert [m["role"] for m in once] == ["user"]
    assert once[0]["content"] == "a\n\nb"


def test_send_turn_coalesces_dangling_user_message(db_factory, monkeypatch):
    """A prior failed/cancelled turn leaves a dangling user message; the next
    send must not produce two consecutive user turns (provider 400)."""
    db = db_factory()
    nrid = _seed_node_run(db)
    chat = cc_api.open_call_chat(nrid, "call-1", db=db)
    # open_call_chat returns a Pydantic CallChatOut; mutate the ORM row to
    # simulate a prior failed turn that left a dangling trailing user message.
    row = db.get(models.CallChat, chat.id)
    row.messages = [*SEED_MESSAGES, {"role": "user", "content": "first try"}]
    db.commit()

    captured = {}

    def fake_start(turn_id, chat_id, messages, tools, model, child_env):
        captured.update(messages=messages)

    monkeypatch.setattr(cc_api.chat_service, "start_chat_turn", fake_start)
    monkeypatch.setattr(cc_api, "build_child_env", lambda: {})

    cc_api.send_call_chat_turn(
        chat.id, schemas.CallChatTurnIn(text="second try"), db=db
    )

    def _no_consecutive_users(msgs):
        roles = [m["role"] for m in msgs]
        return not any(
            roles[i] == roles[i + 1] == "user" for i in range(len(roles) - 1)
        )

    sent = captured["messages"]
    assert _no_consecutive_users(sent)
    assert sent[-1]["role"] == "user"
    assert "first try" in sent[-1]["content"] and "second try" in sent[-1]["content"]
    # The persisted row is clean too (reload-safe).
    assert _no_consecutive_users(db.get(models.CallChat, chat.id).messages)


# --- cancel lifecycle (events) ----------------------------------------------


def test_cancel_before_spawn_records_intent_and_reports_success():
    rid = "turn-test-cancel-prespawn"
    ev_mod.discard(rid)
    st = ev_mod.get_or_create(rid)
    assert st.proc is None
    # A cancel during the spawn window (proc not yet created) must record the
    # intent AND report success — the spawner honors the flag once it owns the
    # proc, so the caller mustn't be told the cancel failed.
    assert ev_mod.cancel(rid) is True
    assert ev_mod.get(rid).cancelled is True
    ev_mod.discard(rid)


def test_cancel_unknown_or_finished_returns_false():
    assert ev_mod.cancel("turn-does-not-exist") is False
    rid = "turn-test-finished"
    ev_mod.discard(rid)
    st = ev_mod.get_or_create(rid)
    st.finished = True
    assert ev_mod.cancel(rid) is False
    ev_mod.discard(rid)


# --- call_chats unique-index back-fill migration ----------------------------


def _sqlite_conn():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    return eng.connect()


def test_unique_index_backfilled_on_constraintless_table():
    conn = _sqlite_conn()
    conn.execute(sa_text(
        "CREATE TABLE call_chats (id TEXT PRIMARY KEY, node_run_id TEXT, call_id TEXT)"
    ))
    conn.execute(sa_text("INSERT INTO call_chats VALUES ('a','nr1','c1')"))
    conn.commit()
    _ensure_callchat_unique_index(conn)
    names = [r[1] for r in conn.execute(sa_text("PRAGMA index_list(call_chats)")).fetchall()]
    assert "uq_callchat_call" in names
    # The back-filled index now enforces uniqueness (the runtime dedup relies on
    # the resulting IntegrityError).
    with pytest.raises(Exception):
        conn.execute(sa_text("INSERT INTO call_chats VALUES ('b','nr1','c1')"))
        conn.commit()
    conn.close()


def test_unique_index_noop_when_already_covered():
    conn = _sqlite_conn()
    conn.execute(sa_text(
        "CREATE TABLE call_chats (id TEXT PRIMARY KEY, node_run_id TEXT, call_id TEXT, "
        "CONSTRAINT uq UNIQUE (node_run_id, call_id))"
    ))
    conn.commit()
    before = len(conn.execute(sa_text("PRAGMA index_list(call_chats)")).fetchall())
    _ensure_callchat_unique_index(conn)
    after = len(conn.execute(sa_text("PRAGMA index_list(call_chats)")).fetchall())
    # Coverage detected by columns, not name → no redundant second index added.
    assert after == before
    conn.close()


def test_unique_index_leaves_duplicates_without_data_loss():
    conn = _sqlite_conn()
    conn.execute(sa_text(
        "CREATE TABLE call_chats (id TEXT PRIMARY KEY, node_run_id TEXT, call_id TEXT)"
    ))
    conn.execute(sa_text("INSERT INTO call_chats VALUES ('a','nr1','c1')"))
    conn.execute(sa_text("INSERT INTO call_chats VALUES ('b','nr1','c1')"))
    conn.commit()
    _ensure_callchat_unique_index(conn)  # must not raise, must not delete
    count = conn.execute(sa_text("SELECT COUNT(*) FROM call_chats")).scalar()
    assert count == 2  # no auto-delete
    names = [r[1] for r in conn.execute(sa_text("PRAGMA index_list(call_chats)")).fetchall()]
    assert "uq_callchat_call" not in names  # blocked by dupes, left uncreated
    conn.close()
