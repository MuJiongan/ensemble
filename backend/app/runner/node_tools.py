"""Node-embedded tool declarations.

Node source can define lowercase ``NodeTool`` subclasses alongside its
``run(inputs, ctx)`` function.  Classes are registered as they are created
while that source is executed inside :func:`node_tool_registration`.

The active registrar is a ContextVar rather than process-global mutable state:
independent nodes execute concurrently in worker threads and may legitimately
define tools with the same name.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator


RegisterFn = Callable[[type["NodeTool"]], None]
_CURRENT_REGISTRAR: ContextVar[RegisterFn | None] = ContextVar(
    "node_tool_registrar", default=None
)


class NodeTool:
    """Base class for a custom tool embedded in a node's Python source.

    Subclasses use their lowercase class name as the provider-facing tool name
    and declare ``description``, JSON-schema ``parameters``, and an
    ``execute(self, ctx, **arguments)`` implementation.
    """

    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        registrar = _CURRENT_REGISTRAR.get()
        if registrar is not None:
            registrar(cls)

    def execute(self, ctx, **arguments):  # pragma: no cover - contract stub
        raise NotImplementedError


@contextmanager
def node_tool_registration(register: RegisterFn) -> Iterator[None]:
    """Route ``NodeTool`` subclasses created in this scope to ``register``."""

    token = _CURRENT_REGISTRAR.set(register)
    try:
        yield
    finally:
        _CURRENT_REGISTRAR.reset(token)


def execute_node_source(code: str, register: RegisterFn) -> dict:
    """Execute a node code blob and return its namespace.

    Keeping registration wrapped around the ``exec`` means class creation is
    the declaration: no extra node field, ``NODE_TOOLS`` list, or namespace
    scan is needed.
    """

    namespace: dict = {}
    with node_tool_registration(register):
        exec(code or "", namespace, namespace)
    return namespace
