from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Any

from agent_common.models import HumanLoopMode
from agent_runtime.runtime import EasyAgentRuntime, build_runtime


class AgentApp:
    """Small Python facade over EasyAgentRuntime for product-style embedding."""

    def __init__(self, runtime: EasyAgentRuntime) -> None:
        self.runtime = runtime

    @classmethod
    def from_config(cls, config: str | Path = 'easy-agent.yml') -> AgentApp:
        return cls(build_runtime(config))

    @classmethod
    def from_runtime(cls, runtime: EasyAgentRuntime) -> AgentApp:
        return cls(runtime)

    async def arun(
        self,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return await self.runtime.run(input_text, session_id=session_id, approval_mode=approval_mode)

    def run(
        self,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return asyncio.run(self.arun(input_text, session_id=session_id, approval_mode=approval_mode))

    async def aresume(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return await self.runtime.resume(run_id, checkpoint_id, fork=fork, approval_mode=approval_mode)

    def resume(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.aresume(run_id, checkpoint_id, fork=fork, approval_mode=approval_mode)
        )

    async def aclose(self) -> None:
        await self.runtime.aclose()

    def close(self) -> None:
        asyncio.run(self.aclose())

    async def __aenter__(self) -> AgentApp:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
