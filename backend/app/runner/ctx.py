"""Ctx object injected into every node's `run(inputs, ctx)` call.

The runner can pass an `on_event` callback that fires for log lines, LLM calls,
and tool invocations as they happen — used to stream events through the
subprocess to the websocket layer.

Each ``ctx.agent`` invocation gets a unique ``call_id`` so the run panel can
render concurrent calls (a node spawning threads, each calling ``agent``)
as parallel streaming cards instead of mashing them together.
"""
from __future__ import annotations
import inspect
import itertools
import json
import re
import os
import threading
from pathlib import Path
from typing import Callable

from app.runner.tools import (
    MCP_NAMESPACES,
    REGISTRY,
    TOOL_SCHEMAS,
    mcp_unavailable_error,
    strip_attachment_data,
)
from app.runner import llm as llm_mod
from app.runner.node_tools import NodeTool


EmitFn = Callable[[dict], None]


def _strip_message_attachments(messages: list) -> list:
    """Drop the ``attachments`` key (image/file base64 that rides tool messages
    for the protocol adapters) from a copy of each message.

    Tool messages already carry a text-only ``content`` size-note; the base64
    only exists for the in-process round and must never reach persistence —
    a single screenshot is megabytes and the persisted transcript never needs
    the bytes, only the size-note text."""
    out = []
    for m in messages:
        if isinstance(m, dict) and "attachments" in m:
            m = {k: v for k, v in m.items() if k != "attachments"}
        out.append(m)
    return out


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

    def __init__(
        self,
        registry: dict[str, Callable],
        recorder: list[dict],
        on_event: EmitFn,
        lock: threading.Lock,
    ):
        self._registry = registry
        self._recorder = recorder
        self._on_event = on_event
        self._lock = lock
        # Each direct call gets a unique id so the run UI can match its
        # ``tool_call_started`` (pending state) to ``tool_call_finished``
        # (ok/err) instead of dumping everything into a flat list.
        self._call_counter = itertools.count(1)

    def __getattr__(self, name: str):
        if name in self._registry:
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
        fn = self._registry.get(name)
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
            for param in sig.parameters.values():
                if param.kind == inspect.Parameter.VAR_KEYWORD:
                    call_args.update(call_args.pop(param.name, {}))
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
        # MCP discovery has already populated the process registry before a
        # node Ctx is created. Copy it here so custom tools stay node-local:
        # parallel nodes may use the same custom name without colliding.
        self.tool_registry: dict[str, Callable] = dict(REGISTRY)
        self.tool_schemas: dict[str, dict] = dict(TOOL_SCHEMAS)
        self.tools = _ToolsProxy(
            self.tool_registry, self.tool_calls, self._on_event, self._lock
        )

    def register_node_tool(self, tool_class: type[NodeTool]) -> None:
        """Validate and register one embedded ``NodeTool`` subclass."""

        if not inspect.isclass(tool_class) or not issubclass(tool_class, NodeTool):
            raise TypeError("custom tools must subclass NodeTool")

        name = tool_class.__name__
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(
                f"custom tool class '{name}' must use lowercase snake_case"
            )
        if name in self.tool_registry:
            raise ValueError(f"custom tool '{name}' conflicts with an existing tool")

        description = getattr(tool_class, "description", "")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"custom tool '{name}' must define a description")

        parameters = getattr(tool_class, "parameters", None)
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(
                f"custom tool '{name}' parameters must be an object JSON schema"
            )
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict):
            raise ValueError(
                f"custom tool '{name}' parameters.properties must be an object"
            )
        if not isinstance(required, list) or any(
            not isinstance(item, str) or item not in properties for item in required
        ):
            raise ValueError(
                f"custom tool '{name}' parameters.required must name declared properties"
            )
        if "execute" not in tool_class.__dict__ or not callable(tool_class.execute):
            raise ValueError(f"custom tool '{name}' must define execute(self, ctx, ...)")
        try:
            json.dumps(parameters)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"custom tool '{name}' parameters must be JSON-serializable: {e}"
            ) from e

        execute_params = list(inspect.signature(tool_class.execute).parameters.values())
        if (
            len(execute_params) < 2
            or execute_params[0].name != "self"
            or execute_params[1].name != "ctx"
        ):
            raise ValueError(
                f"custom tool '{name}' execute signature must start with (self, ctx)"
            )
        tool_params = execute_params[2:]
        if any(
            p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL)
            for p in tool_params
        ):
            raise ValueError(
                f"custom tool '{name}' execute arguments must be named parameters"
            )
        has_var_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in tool_params
        )
        if not has_var_kwargs:
            declared = {
                p.name
                for p in tool_params
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            }
            signature_required = {
                p.name
                for p in tool_params
                if p.name in declared and p.default is inspect.Parameter.empty
            }
            if declared != set(properties):
                raise ValueError(
                    f"custom tool '{name}' schema properties must match its execute arguments"
                )
            if signature_required != set(required):
                raise ValueError(
                    f"custom tool '{name}' schema required fields must match "
                    "execute arguments without defaults"
                )

        def handler(*args, **arguments):
            return tool_class().execute(self, *args, **arguments)

        handler.__name__ = name
        # Make the wrapper look exactly like execute minus its injected
        # (self, ctx), so direct calls get native-style argument validation and
        # flat trace payloads while LLM dispatch can continue calling by kwargs.
        handler.__signature__ = inspect.Signature(tool_params)  # type: ignore[attr-defined]

        self.tool_registry[name] = handler
        self.tool_schemas[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description.strip(),
                "parameters": parameters,
            },
        }

    def _next_call_id(self) -> str:
        return f"call-{next(self._call_counter)}"

    def log(self, msg) -> None:
        s = str(msg)
        with self._lock:
            self.logs.append(s)
        self._on_event({"type": "log", "msg": s})

    def agent(
        self,
        model: str | None = None,
        prompt=None,
        tools=None,
        label: str | None = None,
        **opts,
    ) -> dict:
        m = model or self._default_model
        if not m:
            raise RuntimeError("agent: no model specified and no default configured")

        tool_names = list(tools or [])
        if any(not isinstance(name, str) for name in tool_names):
            raise TypeError("agent tools must be registered tool-name strings")

        call_id = self._next_call_id()
        call_label = (label or "").strip() or None
        started: dict = {
            "type": "llm_call_started",
            "call_id": call_id,
            "model": m,
            "tools": tool_names,
        }
        if call_label:
            started["label"] = call_label
        self._on_event(started)
        try:
            result = llm_mod.call_llm(
                m,
                prompt,
                tools=tool_names,
                tool_registry=self.tool_registry,
                tool_schemas_by_name=self.tool_schemas,
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

        # Strip attachment base64 from the recorded transcript: it only matters
        # for the in-process round and must never reach persistence. The caller
        # still gets the full result (a node may want the bytes).
        seed_msgs = _strip_message_attachments(result.get("messages") or [])
        record = {
            "call_id": call_id,
            "model": m,
            "prompt": prompt if isinstance(prompt, str) else "<messages>",
            "tools": tool_names,
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
            # Full conversation, for resuming this call as a chat. Stored
            # verbatim and uncapped — the run-persist step lifts it into its own
            # call_transcripts row (off the run-load hot path), and the
            # continue-chat path trims it to fit when it seeds a chat.
            "messages": seed_msgs,
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
