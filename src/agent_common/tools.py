from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from agent_common.models import RunContext, ToolSpec

ToolHandler = Callable[[dict[str, Any], RunContext], Any | Awaitable[Any]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._tools[spec.name] = (spec, handler)

    def has(self, name: str) -> bool:
        return name in self._tools

    def get_spec(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        return self._tools[name][0]

    def list_specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        if names is None:
            return [item[0] for item in self._tools.values()]
        return [self._tools[name][0] for name in names if name in self._tools]

    async def call(self, name: str, arguments: dict[str, Any], context: RunContext) -> Any:
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        _, handler = self._tools[name]
        result = handler(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result
