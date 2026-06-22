"""Ctx object injected into every node's `run(inputs, ctx)` call.

The runner can pass an `on_event` callback that fires for log lines, LLM calls,
and tool invocations as they happen — used to stream events through the
subprocess to the websocket layer.

Each ``ctx.call_llm`` invocation gets a unique ``call_id`` so the run panel can
render concurrent calls (a node spawning threads, each calling ``call_llm``)
as parallel streaming cards instead of mashing them together.
"""
from __future__ import annotations
import inspect
import itertools
import json
import os
import threading
from pathlib import Path
from typing import Callable

from app.runner.tools import (
    MCP_NAMESPACES,
    REGISTRY,
    mcp_unavailable_error,
    strip_attachment_data,
)
from app.runner import llm as llm_mod


EmitFn = Callable[[dict], None]

# A call's full message history is persisted on its run-trace record so the
# "continue chat" feature can re-seed the exact conversation. It's the only
# place we store the verbatim transcript, so cap it: a long agent loop can
# accumulate large tool outputs, and we don't want to bloat the NodeRun JSON
# (which is read on every run load). Over budget → store None; the call is
# then simply not continuable (the UI hides its "open in chat" affordance).
_SEED_MSG_BUDGET_BYTES = 256 * 1024


def _strip_message_attachments(messages: list) -> list:
    """Drop the ``attachments`` key (image/file base64 that rides tool messages
    for the protocol adapters) from a copy of each message.

    Tool messages already carry a text-only ``content`` size-note; the base64
    only exists for the in-process round and must never reach persistence —
    otherwise a single screenshot blows the seed budget and silently makes the
    call non-continuable, and any that slipped under the cap would bloat the row."""
    out = []
    for m in messages:
        if isinstance(m, dict) and "attachments" in m:
            m = {k: v for k, v in m.items() if k != "attachments"}
        out.append(m)
    return out


def _within_seed_budget(messages: list) -> bool:
    """True if `messages` serialize under the persistence budget. Conservative:
    a serialization failure counts as over-budget (don't persist garbage)."""
    if not messages:
        return False
    try:
        return len(json.dumps(messages, default=str)) <= _SEED_MSG_BUDGET_BYTES
    except (TypeError, ValueError):
        return False


class _ServerProxy:
    """One MCP server's tools as attributes: ``ctx.tools.notion.create_pages(...)``."""

    def __init__(self, parent: "_ToolsProxy", server: str, tools: dict[str, str]):
        self._parent = parent
        self._server = server
        self._tools = tools  # tool_attr -> registry key

    def __getattr__(self, name: str):
        key = self._tools.get(name)
        if key is None:
            raise AttributeError(
                f"no tool '{name}' on MCP server '{self._server}'"
            )
        return self._parent._bind(key)


class _ToolsProxy:
    """Direct (non-LLM) access. Built-in tools are attributes
    (``ctx.tools.shell(...)``); MCP tools are reachable both flat
    (``ctx.tools.notion_create_pages(...)``) and dotted by server
    (``ctx.tools.notion.create_pages(...)``)."""

    def __init__(self, recorder: list[dict], on_event: EmitFn, lock: threading.Lock):
        self._recorder = recorder
        self._on_event = on_event
        self._lock = lock
        # Each direct call gets a unique id so the run UI can match its
        # ``tool_call_started`` (pending state) to ``tool_call_finished``
        # (ok/err) instead of dumping everything into a flat list.
        self._call_counter = itertools.count(1)

    def __getattr__(self, name: str):
        if name in REGISTRY:
            return self._bind(name)
        if name in MCP_NAMESPACES:
            return _ServerProxy(self, name, MCP_NAMESPACES[name])
        # The tool may belong to an MCP server that failed to connect (needs
        # auth, unreachable) — explain that instead of a bare registry miss.
        unavailable = mcp_unavailable_error(name)
        if unavailable is not None:
            raise RuntimeError(unavailable["error"])
        raise AttributeError(f"no tool '{name}' in registry")

    def _bind(self, name: str):
        fn = REGISTRY.get(name)
        if fn is None:
            raise AttributeError(f"no tool '{name}' in registry")

        sig = inspect.signature(fn)

        def wrapped(*args, **kwargs):
            # Normalise positional + keyword into a single dict keyed by the
            # tool's parameter names — keeps the event payload consistent
            # whether the caller wrote `ctx.tools.web_fetch(url)` or
            # `ctx.tools.web_fetch(url=url)`. Bind errors (missing required,
            # unexpected name) surface as TypeError, matching plain Python.
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)
            tc_id = f"direct-{next(self._call_counter)}"

            self._on_event(
                {
                    "type": "tool_call_started",
                    "tool": name,
                    "args": call_args,
                    "via": "direct",
                    "call_id": tc_id,
                }
            )
            entry: dict = {"name": name, "args": call_args, "via": "direct"}
            try:
                result = fn(*args, **kwargs)
                # The caller gets the full result (a node may want the bytes);
                # the run record and event stream get a copy with attachment
                # base64 replaced by a size note.
                recorded = strip_attachment_data(result)
                entry["result"] = recorded
                with self._lock:
                    self._recorder.append(entry)
                self._on_event(
                    {
                        "type": "tool_call_finished",
                        "tool": name,
                        "args": call_args,
                        "result": recorded,
                        "via": "direct",
                        "call_id": tc_id,
                    }
                )
                return result
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                entry["error"] = err
                with self._lock:
                    self._recorder.append(entry)
                self._on_event(
                    {
                        "type": "tool_call_finished",
                        "tool": name,
                        "args": call_args,
                        "error": err,
                        "via": "direct",
                        "call_id": tc_id,
                    }
                )
                raise

        return wrapped


class Ctx:
    def __init__(
        self,
        workdir: Path,
        default_model: str,
        on_event: EmitFn | None = None,
    ):
        self.workdir = workdir
        self._default_model = default_model
        self._on_event: EmitFn = on_event or (lambda ev: None)
        self.logs: list[str] = []
        self.llm_calls: list[dict] = []
        self.tool_calls: list[dict] = []
        self._lock = threading.Lock()
        self._call_counter = itertools.count(1)
        self.tools = _ToolsProxy(self.tool_calls, self._on_event, self._lock)

    def _next_call_id(self) -> str:
        return f"call-{next(self._call_counter)}"

    def log(self, msg) -> None:
        s = str(msg)
        with self._lock:
            self.logs.append(s)
        self._on_event({"type": "log", "msg": s})

    def call_llm(
        self,
        model: str | None = None,
        prompt=None,
        tools=None,
        label: str | None = None,
        **opts,
    ) -> dict:
        m = model or self._default_model
        if not m:
            raise RuntimeError("call_llm: no model specified and no default configured")

        call_id = self._next_call_id()
        call_label = (label or "").strip() or None
        started: dict = {
            "type": "llm_call_started",
            "call_id": call_id,
            "model": m,
            "tools": tools or [],
        }
        if call_label:
            started["label"] = call_label
        self._on_event(started)
        try:
            result = llm_mod.call_llm(
                m,
                prompt,
                tools=tools,
                on_event=self._on_event,
                call_id=call_id,
                **opts,
            )
        except Exception as e:
            self._on_event(
                {
                    "type": "llm_call_finished",
                    "call_id": call_id,
                    "model": m,
                    "content": "",
                    "usage": {},
                    "cost": 0.0,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            raise

        # Strip attachment base64 before the budget check: it only matters for
        # the in-process round, and counting/persisting it would wrongly push
        # transcripts over budget (silently disabling continuation).
        seed_msgs = _strip_message_attachments(result.get("messages") or [])
        record = {
            "call_id": call_id,
            "model": m,
            "prompt": prompt if isinstance(prompt, str) else "<messages>",
            "tools": tools or [],
            **({"label": call_label} if call_label else {}),
            "content": result.get("content", ""),
            "tool_calls_made": result.get("tool_calls_made", []),
            "usage": result.get("usage", {}),
            "cost": result.get("cost", 0.0),
            # Provider + reasoning variant this call actually ran with, so a
            # continuation can pin the *same* model end-to-end instead of
            # inheriting whatever node default Settings happens to hold later.
            "provider_id": (os.getenv("LLM_PROVIDER_ID") or "").strip(),
            "variant": os.getenv("DEFAULT_NODE_VARIANT") or "",
            # Full conversation, for resuming this call as a chat. None when it
            # exceeds the persistence budget (call is then not continuable).
            "messages": seed_msgs if _within_seed_budget(seed_msgs) else None,
        }
        with self._lock:
            self.llm_calls.append(record)
            for tc in result.get("tool_calls_made", []):
                self.tool_calls.append(
                    {
                        "name": tc.get("name"),
                        "args": tc.get("args"),
                        "result": tc.get("result"),
                        "via": "llm",
                    }
                )

        self._on_event(
            {
                "type": "llm_call_finished",
                "call_id": call_id,
                "model": m,
                "content": record["content"],
                "usage": record["usage"],
                "cost": record["cost"],
            }
        )
        return result
