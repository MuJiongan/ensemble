"""Bounded retry support for rate-limited or overloaded model streams.

Model adapters normalize provider HTTP failures to ``RuntimeError`` strings,
which loses the original SDK exception in some transports.  The helpers here
recognize both structured status attributes and those normalized messages.
Retries are only allowed before a stream yields its first item: restarting a
partially-consumed stream would duplicate visible output (and could repeat
tool-call arguments).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, TypeVar


RATE_LIMIT_RETRY_DELAYS = (5.0, 10.0, 15.0, 20.0, 25.0)


@dataclass(frozen=True)
class RateLimitRetry:
    """Control item yielded immediately before a retry wait begins."""

    attempt: int
    max_retries: int
    delay_seconds: float


T = TypeVar("T")


def is_retryable_overload_error(exc: BaseException) -> bool:
    """Return whether ``exc`` represents a retryable provider overload.

    Most transports surface overload as HTTP 429. The OpenAI Responses SSE
    protocol can instead accept the HTTP request and later emit a structured
    ``server_is_overloaded`` failure inside the stream, while Anthropic uses
    the analogous ``overloaded_error`` marker.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for value in (
            getattr(current, "status_code", None),
            getattr(current, "code", None),
            getattr(getattr(current, "response", None), "status_code", None),
        ):
            try:
                if int(value) == 429:
                    return True
            except (TypeError, ValueError):
                pass

        message = str(current)
        if re.search(r"\b(?:server_is_overloaded|overloaded_error)\b", message, re.IGNORECASE):
            return True
        # Our native adapters use "LLM 429: ..." and the ChatGPT transport
        # uses "Codex 429: ...".  If one of those prefixes carries another
        # status, do not mistake a stray "429" in the response body for the
        # actual response code.
        normalized = re.search(r"\b(?:LLM|Codex)\s+(\d{3})\b", message, re.IGNORECASE)
        if normalized:
            return normalized.group(1) == "429"
        if re.search(
            r"\b(?:HTTP|status(?:\s+code)?|error\s+code)\D{0,8}429\b",
            message,
            re.IGNORECASE,
        ):
            return True

        current = current.__cause__ or current.__context__
    return False


def is_rate_limit_error(exc: BaseException) -> bool:
    """Backward-compatible name for retryable rate-limit/overload detection."""
    return is_retryable_overload_error(exc)


def retry_rate_limited_stream(
    stream_factory: Callable[[], Iterable[T]],
    *,
    delays: Iterable[float] | None = None,
    cancel_event=None,
) -> Iterator[T | RateLimitRetry]:
    """Retry pre-stream rate-limit/overload failures after ``delays``.

    A :class:`RateLimitRetry` item is yielded before each wait so callers can
    tell a live UI why it is still waiting.  Once the stream has yielded any
    provider item, all errors propagate without retrying to avoid duplicated
    partial output.  When ``cancel_event`` is supplied, its ``wait`` method
    makes the backoff interruptible and a cancellation ends the iterator.
    """
    retry_delays = tuple(RATE_LIMIT_RETRY_DELAYS if delays is None else delays)
    attempt = 0
    while True:
        yielded = False
        try:
            for item in stream_factory():
                yielded = True
                yield item
            return
        except Exception as exc:
            if (
                yielded
                or not is_retryable_overload_error(exc)
                or attempt >= len(retry_delays)
            ):
                raise

            delay = max(0.0, float(retry_delays[attempt]))
            attempt += 1
            yield RateLimitRetry(
                attempt=attempt,
                max_retries=len(retry_delays),
                delay_seconds=delay,
            )
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    return
            else:
                time.sleep(delay)
