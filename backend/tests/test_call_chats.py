"""Continue-chat (call_llm continuation) API + persistence.

Covers read-only viewing (no row written) vs lazy create-on-first-turn, get-or-
create idempotency, the no-transcript/missing-call 404s, the model the
continuation pins (provider/model/variant), the run payload exposing only a
has_chat flag, lifting transcripts into their own rows (off the run-load hot
path), truncating an oversized seed to fit instead of refusing it, attachment
stripping, the persisted-transcript cap, run-delete cascade, and that sending a
turn materializes + persists the user message before spawning the (stubbed) turn
subprocess.
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
from app.runner.ctx import _strip_message_attachments
from app.runner.service import _split_call_transcripts
from app.runner.chat import (
    cap_transcript,
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
    """Seed a Workflow + Run (with snapshot) + NodeRun carrying one llm call.

    Mirrors the real persist path: the call's transcript lives in its own
    CallTranscript row, not in the llm_calls blob. ``messages=None`` means no
    transcript row was written → the call isn't continuable.
    """
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
            "has_chat": bool(messages),
        }],
    )
    db.add(nr)
    db.commit()
    db.refresh(nr)
    if messages:
        db.add(models.CallTranscript(
            node_run_id=nr.id, call_id=call_id, messages=messages,
        ))
        db.commit()
    return nr.id


def test_view_call_chat_returns_seed_without_writing(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)

    view = cc_api.view_call_chat(nrid, "call-1", db=db)
    assert view.messages == SEED_MESSAGES
    assert view.model == "anthropic/claude-sonnet-4.5"
    # The continuation pins the provider + reasoning variant the call ran with,
    # so it stays on the same model regardless of the current node default.
    assert view.provider_id == "anthropic"
    assert view.variant == "high"
    assert view.tools == ["web_search"]
    assert view.label == "extract · call 1"
    # Viewing is a pure read: no row is materialized, and the id is empty until
    # the first turn creates one.
    assert view.id == ""
    assert db.query(models.CallChat).count() == 0


def test_get_or_create_continuation_is_idempotent(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)

    first = cc_api._get_or_create_continuation(db, nrid, "call-1")
    assert first.id  # a real persisted row now exists
    assert first.messages == SEED_MESSAGES
    # A second call returns the same row, not a duplicate.
    second = cc_api._get_or_create_continuation(db, nrid, "call-1")
    assert second.id == first.id
    assert db.query(models.CallChat).count() == 1
    # Once started, viewing returns the persisted thread (with its real id).
    view = cc_api.view_call_chat(nrid, "call-1", db=db)
    assert view.id == first.id


def test_view_call_chat_404_when_no_transcript(db_factory):
    db = db_factory()
    # A call that never recorded a transcript (e.g. it errored before finishing)
    # has no CallTranscript row → not continuable.
    nrid = _seed_node_run(db, messages=None)
    with pytest.raises(HTTPException) as exc:
        cc_api.view_call_chat(nrid, "call-1", db=db)
    assert exc.value.status_code == 404


def test_view_call_chat_uses_call_label_when_set(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)
    nr = db.get(models.NodeRun, nrid)
    nr.llm_calls = [{**nr.llm_calls[0], "label": "summarise item 3"}]
    db.commit()

    view = cc_api.view_call_chat(nrid, "call-1", db=db)
    assert view.label == "extract · summarise item 3"


def test_view_call_chat_404_when_call_missing(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)
    with pytest.raises(HTTPException) as exc:
        cc_api.view_call_chat(nrid, "no-such-call", db=db)
    assert exc.value.status_code == 404


def test_split_call_transcripts_lifts_messages_and_flags():
    lean, transcripts = _split_call_transcripts([
        {"call_id": "c1", "content": "x", "messages": SEED_MESSAGES},
        {"call_id": "c2", "content": "y", "messages": None},
        {"call_id": "c3", "content": "z"},  # no messages key at all
        "not-a-dict",
    ])
    # The verbatim transcript never stays in the blob; has_chat reflects whether
    # one was lifted into its own row.
    assert all(not isinstance(c, dict) or "messages" not in c for c in lean)
    assert lean[0]["has_chat"] is True
    assert lean[1]["has_chat"] is False
    assert lean[2]["has_chat"] is False
    assert lean[3] == "not-a-dict"  # non-dict entries pass through untouched
    # Only the call with a real transcript yields a (call_id, messages) row.
    assert transcripts == [("c1", SEED_MESSAGES)]


def test_split_call_transcripts_skips_callidless_messages():
    # A transcript can't be keyed without a call_id, so it isn't lifted out and
    # the call is marked not-continuable rather than persisted unkeyed.
    lean, transcripts = _split_call_transcripts([
        {"content": "x", "messages": SEED_MESSAGES},
    ])
    assert lean[0]["has_chat"] is False
    assert transcripts == []


def test_view_call_chat_truncates_oversized_seed_instead_of_refusing(db_factory):
    """The whole point of moving transcripts off the blob: an oversized call now
    opens (trimmed to fit) instead of 404ing the way the old 2 MB seed cap did."""
    big = "x" * (800 * 1024)
    msgs = []
    for i in range(8):
        msgs += [
            {"role": "user", "content": f"turn {i}"},
            {"role": "assistant", "content": big},
        ]
    db = db_factory()
    nrid = _seed_node_run(db, messages=msgs)

    view = cc_api.view_call_chat(nrid, "call-1", db=db)
    # Seeded under the continuation cap, with the latest turn kept and the
    # oldest dropped — never a refusal.
    assert len(json.dumps(view.messages)) <= _TRANSCRIPT_BUDGET_BYTES
    assert {"role": "user", "content": "turn 7"} in view.messages
    assert {"role": "user", "content": "turn 0"} not in view.messages


def test_view_call_chat_does_not_read_blob_messages(db_factory):
    """A transcript only in the (defensive) blob — not in its own row — is NOT
    continuable: the read path is the CallTranscript table, full stop."""
    db = db_factory()
    nrid = _seed_node_run(db, messages=None)  # no transcript row
    nr = db.get(models.NodeRun, nrid)
    nr.llm_calls = [{**nr.llm_calls[0], "messages": SEED_MESSAGES}]  # stray blob seed
    db.commit()
    with pytest.raises(HTTPException) as exc:
        cc_api.view_call_chat(nrid, "call-1", db=db)
    assert exc.value.status_code == 404


def test_run_payload_carries_flag_not_transcript(db_factory):
    """The storage split, checked at the real API boundary: the seed lives in its
    own table, and the serialized run payload exposes only a has_chat flag — the
    NodeRun blob ships as-is and never carries the verbatim transcript."""
    db = db_factory()
    nrid = _seed_node_run(db)
    # Stored separately, keyed by (node_run_id, call_id).
    t = (
        db.query(models.CallTranscript)
        .filter_by(node_run_id=nrid, call_id="call-1")
        .first()
    )
    assert t is not None and t.messages == SEED_MESSAGES
    # What the run endpoint actually serializes carries the flag, not the seed.
    run = db.get(models.Run, db.get(models.NodeRun, nrid).run_id)
    out = runs_api._run_to_out(run, run.node_runs)
    call = out.node_runs[0].llm_calls[0]
    assert "messages" not in call
    assert call["has_chat"] is True


def test_delete_run_cascades_transcripts(db_factory):
    """Deleting a run drops its transcripts via the node_run relationship cascade
    — they must not orphan (unlike CallChat, which the delete endpoint clears
    explicitly because it's FK-free)."""
    db = db_factory()
    nrid = _seed_node_run(db)
    assert db.query(models.CallTranscript).count() == 1
    run_id = db.get(models.NodeRun, nrid).run_id
    db.delete(db.get(models.Run, run_id))
    db.commit()
    assert db.query(models.CallTranscript).count() == 0
    assert db.query(models.NodeRun).count() == 0


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
    assert cap_transcript(SEED_MESSAGES) == SEED_MESSAGES


def test_cap_transcript_trims_oldest_turns_at_user_boundaries():
    big = "x" * (800 * 1024)
    msgs = []
    for i in range(6):
        msgs += [
            {"role": "user", "content": f"turn {i}"},
            {"role": "assistant", "content": big},
        ]
    capped = cap_transcript(msgs)
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
    capped = cap_transcript(msgs)
    assert capped[0] == {"role": "system", "content": "sys"}
    assert len(json.dumps(capped)) <= _TRANSCRIPT_BUDGET_BYTES


def test_cap_transcript_single_oversized_turn_kept_whole():
    # One turn can't be trimmed at a boundary without orphaning tool results —
    # persist it whole rather than corrupt the assistant/tool pairing.
    msgs = [
        {"role": "user", "content": "only turn"},
        {"role": "assistant", "content": "x" * (600 * 1024)},
    ]
    assert cap_transcript(msgs) == msgs


def _continuation_row(db, nrid, call_id="call-1"):
    return (
        db.query(models.CallChat)
        .filter_by(node_run_id=nrid, call_id=call_id)
        .first()
    )


def test_send_turn_materializes_chat_lazily_and_persists(db_factory, monkeypatch):
    db = db_factory()
    nrid = _seed_node_run(db)
    # Viewing never created a row; the turn is what materializes it.
    assert db.query(models.CallChat).count() == 0

    captured = {}

    def fake_start(turn_id, chat_id, messages, tools, model, child_env):
        captured.update(
            turn_id=turn_id, chat_id=chat_id, messages=messages, tools=tools, model=model
        )

    monkeypatch.setattr(cc_api.chat_service, "start_chat_turn", fake_start)
    monkeypatch.setattr(cc_api, "build_child_env", lambda: {})

    out = cc_api.send_call_chat_turn(
        nrid, "call-1", schemas.CallChatTurnIn(text="now expand point 2"), db=db
    )
    assert out.turn_id.startswith("turn-")

    # The first turn materialized the continuation, persisted the user turn
    # immediately (reload-safe), and handed it to the turn runner appended to the
    # seed conversation.
    row = _continuation_row(db, nrid)
    assert row is not None
    assert db.query(models.CallChat).count() == 1
    assert row.messages[-1] == {"role": "user", "content": "now expand point 2"}
    assert captured["chat_id"] == row.id
    assert captured["model"] == "anthropic/claude-sonnet-4.5"
    assert captured["messages"][-1]["content"] == "now expand point 2"
    assert captured["tools"] == ["web_search"]


def test_send_turn_empty_message_400_without_materializing(db_factory):
    db = db_factory()
    nrid = _seed_node_run(db)
    with pytest.raises(HTTPException) as exc:
        cc_api.send_call_chat_turn(nrid, "call-1", schemas.CallChatTurnIn(text="  "), db=db)
    assert exc.value.status_code == 400
    # An empty send must not leave behind a continuation row.
    assert db.query(models.CallChat).count() == 0


def test_send_turn_uses_switched_model_and_persists_selection(db_factory, monkeypatch):
    """Switching the model must send the *new* model name, not the recorded one
    — otherwise the turn pairs the new provider with the old model id."""
    db = db_factory()
    nrid = _seed_node_run(db)  # recorded: anthropic / claude-sonnet-4.5 / high

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
        nrid, "call-1", schemas.CallChatTurnIn(text="hi", model="openai/gpt-4o"), db=db
    )

    # The turn uses the switched model, and the continuation remembers the new
    # selection (so a reopen/reload stays on the switched provider+model).
    assert captured["model"] == "openai/gpt-4o"
    row = _continuation_row(db, nrid)
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
    # Materialize the continuation, then mutate the ORM row to simulate a prior
    # failed turn that left a dangling trailing user message.
    row = cc_api._get_or_create_continuation(db, nrid, "call-1")
    row.messages = [*SEED_MESSAGES, {"role": "user", "content": "first try"}]
    db.commit()

    captured = {}

    def fake_start(turn_id, chat_id, messages, tools, model, child_env):
        captured.update(messages=messages)

    monkeypatch.setattr(cc_api.chat_service, "start_chat_turn", fake_start)
    monkeypatch.setattr(cc_api, "build_child_env", lambda: {})

    cc_api.send_call_chat_turn(
        nrid, "call-1", schemas.CallChatTurnIn(text="second try"), db=db
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
    assert _no_consecutive_users(_continuation_row(db, nrid).messages)


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
