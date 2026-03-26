from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio

from agent_common.models import ToolSpec
from agent_common.tools import ToolHandler, ToolRegistry
from agent_config.app import AppConfig, McpServerConfig, load_config
from agent_graph import AgentOrchestrator, GraphScheduler
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.mcp import McpClientManager, build_mcp_tool_name
from agent_integrations.plugins import InlineRuntimePlugin, RuntimePlugin, RuntimePluginHost
from agent_integrations.sandbox import SandboxManager, SandboxMode
from agent_integrations.skills import SkillLoader, SkillMetadata
from agent_integrations.storage import SQLiteRunStore
from agent_protocols.client import HttpModelClient


class EasyAgentRuntime:
    def __init__(
        self,
        config: AppConfig,
        model_client: Any,
        registry: ToolRegistry,
        store: SQLiteRunStore,
        sandbox_manager: SandboxManager,
        mcp_manager: McpClientManager,
        guardrail_engine: GuardrailEngine,
        orchestrator: AgentOrchestrator,
        scheduler: GraphScheduler,
        skills: list[SkillMetadata] | None = None,
    ) -> None:
        self.config = config
        self.model_client = model_client
        self.registry = registry
        self.store = store
        self.sandbox_manager = sandbox_manager
        self.mcp_manager = mcp_manager
        self.guardrail_engine = guardrail_engine
        self.orchestrator = orchestrator
        self.scheduler = scheduler
        self.skills = skills or []
        self.loaded_sources: list[str] = []
        self._loaded_skill_paths: set[Path] = set()
        self._plugin_host = RuntimePluginHost(self)
        self._started = False
        self._bound_mcp_tools: set[str] = set()

    def load(self, source: str | Path | RuntimePlugin) -> EasyAgentRuntime:
        descriptor = self._plugin_host.load(source)
        if descriptor not in self.loaded_sources:
            self.loaded_sources.append(descriptor)
        return self

    def register_skill_path(self, path: Path) -> list[SkillMetadata]:
        if self._started:
            raise RuntimeError('Skills must be registered before runtime.start()')
        resolved_path = path.resolve()
        if resolved_path in self._loaded_skill_paths:
            return []
        loader = SkillLoader([resolved_path], self.config.security.allowed_commands, self.sandbox_manager)
        loaded = loader.register(self.registry)
        self.skills.extend(loaded)
        self._loaded_skill_paths.add(resolved_path)
        return loaded

    def register_mcp_server(self, config: McpServerConfig) -> None:
        if self._started:
            raise RuntimeError('MCP servers must be registered before runtime.start()')
        self.mcp_manager.add_server(config)

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.registry.register(spec, handler)

    def set_sandbox_mode(self, mode: str | SandboxMode) -> None:
        self.sandbox_manager.mode = SandboxMode(mode)

    async def start(self) -> None:
        await self.mcp_manager.start()
        await self._bind_mcp_tools()
        self._started = True

    async def _bind_mcp_tools(self) -> None:
        servers = await self.mcp_manager.list_servers()
        for server_name, tools in servers.items():
            for tool in tools:
                registry_name = build_mcp_tool_name(server_name, tool.name)
                if registry_name in self._bound_mcp_tools:
                    continue

                async def _handler(
                    arguments: dict[str, Any],
                    context: Any,
                    *,
                    bound_server: str = server_name,
                    bound_tool: str = tool.name,
                ) -> Any:
                    return await self.mcp_manager.call_tool(bound_server, bound_tool, arguments, context=context)

                self.registry.register(
                    ToolSpec(
                        name=registry_name,
                        description=f'MCP tool {server_name}/{tool.name}: {tool.description}',
                        input_schema=tool.input_schema,
                    ),
                    _handler,
                )
                self._bound_mcp_tools.add(registry_name)

    async def run(self, input_text: str, session_id: str | None = None) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.scheduler.run(input_text, session_id=session_id)

    async def stream(self, input_text: str, session_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self.start()
        stream = self.store.subscribe_events()
        result: dict[str, Any] | None = None
        error: Exception | None = None
        selected_run_id: str | None = None

        async def _runner() -> None:
            nonlocal result, error
            try:
                result = await self.scheduler.run(input_text, session_id=session_id)
            except Exception as exc:
                error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_runner)
            async with stream:
                async for event in stream:
                    if selected_run_id is None and event['kind'] == 'run_started':
                        selected_run_id = str(event['run_id'])
                    if selected_run_id is None or event['run_id'] == selected_run_id:
                        yield event
                        if event['kind'] in {'run_succeeded', 'run_failed', 'run_interrupted'} and event['run_id'] == selected_run_id:
                            break
        if error is not None:
            raise error
        if result is not None:
            return

    async def resume(self, run_id: str) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.scheduler.resume(run_id)

    async def resume_stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self.start()
        stream = self.store.subscribe_events()
        error: Exception | None = None

        async def _runner() -> None:
            nonlocal error
            try:
                await self.scheduler.resume(run_id)
            except Exception as exc:
                error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_runner)
            async with stream:
                async for event in stream:
                    if event['run_id'] == run_id:
                        yield event
                        if event['kind'] in {'run_succeeded', 'run_failed', 'run_interrupted'}:
                            break
        if error is not None:
            raise error

    async def aclose(self) -> None:
        await self.mcp_manager.aclose()
        await self.model_client.aclose()
        self._started = False


def build_runtime_from_config(config: AppConfig) -> EasyAgentRuntime:
    working_root = Path(config.security.sandbox.working_root) if config.security.sandbox.working_root else None
    sandbox_manager = SandboxManager(
        mode=config.security.sandbox.mode,
        targets=config.security.sandbox.targets,
        env_allowlist=config.security.sandbox.env_allowlist,
        working_root=working_root,
        windows_sandbox_fallback=config.security.sandbox.windows_sandbox_fallback,
    )
    registry = ToolRegistry()
    store = SQLiteRunStore(Path(config.storage.path), config.storage.database)
    guardrail_engine = GuardrailEngine(
        tool_input_hooks=config.guardrails.tool_input_hooks,
        final_output_hooks=config.guardrails.final_output_hooks,
    )
    mcp_manager = McpClientManager([], sandbox_manager, store=store)
    model_client = HttpModelClient(config.model)
    orchestrator = AgentOrchestrator(config, model_client, registry, store, guardrail_engine)
    scheduler = GraphScheduler(config, registry, orchestrator, store, mcp_manager, guardrail_engine)
    runtime = EasyAgentRuntime(
        config,
        model_client,
        registry,
        store,
        sandbox_manager,
        mcp_manager,
        guardrail_engine,
        orchestrator,
        scheduler,
    )
    for plugin_source in config.plugins:
        runtime.load(plugin_source)
    if config.skills:
        runtime.load(InlineRuntimePlugin(skill_paths=[Path(item.path) for item in config.skills]))
    if config.mcp:
        runtime.load(InlineRuntimePlugin(mcp_servers=config.mcp))
    orchestrator.register_subagent_tools()
    return runtime


def build_runtime(config_path: str | Path) -> EasyAgentRuntime:
    return build_runtime_from_config(load_config(config_path))
