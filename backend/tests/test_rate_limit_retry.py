from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import rate_limit
from app.llm.rate_limit import (
    RateLimitRetry,
    is_rate_limit_error,
    is_retryable_overload_error,
    retry_rate_limited_stream,
)
from app.runner.ctx import Ctx
from app.runner.llm import call_llm


def test_recognizes_structured_and_normalized_429_errors():
    structured = RuntimeError("rate limited")
    structured.status_code = 429
    assert is_rate_limit_error(structured)
    assert is_rate_limit_error(RuntimeError("LLM 429: too many requests"))
    assert is_rate_limit_error(RuntimeError("Codex 429: overloaded"))
    assert is_retryable_overload_error(RuntimeError(
        "OpenAI Responses: server_is_overloaded: Our servers are currently overloaded."
    ))
    assert is_retryable_overload_error(RuntimeError(
        "Anthropic stream error: overloaded_error: Overloaded"
    ))
    assert not is_rate_limit_error(RuntimeError("LLM 500: body mentioned 429 requests"))
    assert not is_rate_limit_error(RuntimeError("LLM 401: unauthorized"))


def test_retries_with_bounded_increasing_delays(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def stream():
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise RuntimeError("LLM 429: overloaded")
        yield "done"

    monkeypatch.setattr(rate_limit.time, "sleep", sleeps.append)
    items = list(retry_rate_limited_stream(stream, delays=(5, 10, 15, 20, 25)))

    retries = [item for item in items if isinstance(item, RateLimitRetry)]
    assert [(r.attempt, r.max_retries, r.delay_seconds) for r in retries] == [
        (1, 5, 5.0),
        (2, 5, 10.0),
        (3, 5, 15.0),
        (4, 5, 20.0),
        (5, 5, 25.0),
    ]
    assert items[-1] == "done"
    assert calls == 6
    assert sleeps == [5.0, 10.0, 15.0, 20.0, 25.0]


def test_stops_after_retry_budget(monkeypatch):
    calls = 0

    def stream():
        nonlocal calls
        calls += 1
        raise RuntimeError("LLM 429: still overloaded")
        yield  # pragma: no cover - make this a generator

    monkeypatch.setattr(rate_limit.time, "sleep", lambda _delay: None)
    with pytest.raises(RuntimeError, match="still overloaded"):
        list(retry_rate_limited_stream(stream, delays=(5, 10, 15, 20, 25)))
    assert calls == 6


def test_does_not_retry_other_errors_or_partially_streamed_responses(monkeypatch):
    monkeypatch.setattr(rate_limit.time, "sleep", lambda _delay: None)
    calls = 0

    def other_error():
        nonlocal calls
        calls += 1
        raise RuntimeError("LLM 500: nope")
        yield

    with pytest.raises(RuntimeError, match="500"):
        list(retry_rate_limited_stream(other_error))
    assert calls == 1

    calls = 0

    def partial():
        nonlocal calls
        calls += 1
        yield "visible chunk"
        raise RuntimeError("LLM 429: stream failed late")

    with pytest.raises(RuntimeError, match="429"):
        list(retry_rate_limited_stream(partial))
    assert calls == 1


def test_backoff_can_be_cancelled_before_another_attempt():
    cancel = threading.Event()
    calls = 0

    def stream():
        nonlocal calls
        calls += 1
        raise RuntimeError("LLM 429: overloaded")
        yield

    items = retry_rate_limited_stream(stream, cancel_event=cancel)
    retry = next(items)
    assert isinstance(retry, RateLimitRetry)
    cancel.set()
    assert list(items) == []
    assert calls == 1


def test_continued_agent_round_retries_and_emits_notices(monkeypatch):
    calls = 0

    def stream_round(**_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("LLM 429: overloaded")
        yield (
            "done",
            {
                "message": {"role": "assistant", "content": "hello"},
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    fake_plan = SimpleNamespace(
        stream_round=stream_round,
        base_url="https://example.test/v1",
        variant_opts={},
        extra_headers={},
        model_output_limit=0,
        cost={},
    )
    from app.llm import router

    monkeypatch.setattr(router, "plan", lambda *_args, **_kwargs: fake_plan)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_RETRY_DELAYS", (0, 0, 0, 0, 0))
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER_ID", "test-provider")
    events: list[dict] = []

    result = call_llm(
        "test-model",
        [{"role": "user", "content": "hi"}],
        on_event=events.append,
        call_id="call-1",
        retry_rate_limits=True,
    )

    assert result["content"] == "hello"
    assert calls == 3
    retry_events = [event for event in events if event["type"] == "rate_limit_retry"]
    assert [event["attempt"] for event in retry_events] == [1, 2]
    assert all(event["call_id"] == "call-1" for event in retry_events)


def test_codex_continued_agent_uses_the_same_retry_policy(monkeypatch):
    from app.auth import codex_api

    calls = 0

    def fake_codex_stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(
                "OpenAI Responses: server_is_overloaded: "
                "Our servers are currently overloaded. Please try again later."
            )
        yield (
            "done",
            {
                "message": {"role": "assistant", "content": "recovered"},
                "usage": {},
            },
        )

    monkeypatch.setattr(codex_api, "call_codex_stream", fake_codex_stream)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_RETRY_DELAYS", (0, 0, 0, 0, 0))
    events: list[dict] = []

    result = codex_api.call_codex_chat(
        model="gpt-test",
        prompt=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_registry={},
        tool_schemas_by_name={},
        on_event=events.append,
        call_id="call-codex",
        access_token="token",
        account_id="account",
        retry_rate_limits=True,
    )

    assert result["content"] == "recovered"
    assert calls == 2
    retry = next(event for event in events if event["type"] == "rate_limit_retry")
    assert retry["attempt"] == 1
    assert retry["call_id"] == "call-codex"


def test_ctx_agent_enables_overload_retries_for_workflow_nodes(monkeypatch, tmp_path: Path):
    captured: dict = {}

    def fake_call_llm(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "content": "ok",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok"},
            ],
            "tool_calls_made": [],
            "usage": {},
            "cost": 0.0,
        }

    monkeypatch.setattr("app.runner.ctx.llm_mod.call_llm", fake_call_llm)
    ctx = Ctx(workdir=tmp_path, default_model="test-model")

    ctx.agent(prompt="hi")

    assert captured["retry_rate_limits"] is True
