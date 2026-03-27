from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio

from agent_common.models import HumanLoopMode, HumanRequestStatus, ToolSpec
from agent_common.tools import ToolHandler, ToolRegistry
from agent_config.app import AppConfig, FederationExportConfig, McpServerConfig, load_config
from agent_graph import AgentOrchestrator, GraphScheduler
from agent_integrations.federation import FederationClientManager, FederationServer
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.human_loop import HumanLoopManager, InlineApprovalResolver
from agent_integrations.mcp import McpClientManager, build_mcp_tool_name
from agent_integrations.plugins import InlineRuntimePlugin, RuntimePlugin, RuntimePluginHost
from agent_integrations.sandbox import SandboxManager, SandboxMode
from agent_integrations.skills import SkillLoader, SkillMetadata
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager
from agent_protocols.client import HttpModelClient
from agent_runtime.harness import HarnessRuntime


class EasyAgentRuntime:
    def __init__(
        self,
        config: AppConfig,
        model_client: Any,
        registry: ToolRegistry,
        store: SQLiteRunStore,
        sandbox_manager: SandboxManager,
        workbench_manager: WorkbenchManager,
        mcp_manager: McpClientManager,
        federation_manager: FederationClientManager,
        guardrail_engine: GuardrailEngine,
        human_loop: HumanLoopManager,
        orchestrator: AgentOrchestrator,
        scheduler: GraphScheduler,
        harness_runtime: HarnessRuntime,
        skills: list[SkillMetadata] | None = None,
    ) -> None:
        self.config = config
        self.model_client = model_client
        self.registry = registry
        self.store = store
        self.sandbox_manager = sandbox_manager
        self.workbench_manager = workbench_manager
        self.mcp_manager = mcp_manager
        self.federation_manager = federation_manager
        self.guardrail_engine = guardrail_engine
        self.human_loop = human_loop
        self.orchestrator = orchestrator
        self.scheduler = scheduler
        self.harness_runtime = harness_runtime
        self.skills = skills or []
        self.loaded_sources: list[str] = []
        self._loaded_skill_paths: set[Path] = set()
        self._plugin_host = RuntimePluginHost(self)
        self._started = False
        self._bound_mcp_tools: set[str] = set()
        self._federation_server = FederationServer(self)

    def load(self, source: str | Path | RuntimePlugin) -> EasyAgentRuntime:
        descriptor = self._plugin_host.load(source)
        if descriptor not in self.loaded_sources:
            self.loaded_sources.append(descriptor)
        return self

    def list_harnesses(self) -> list[Any]:
        return self.harness_runtime.list_harnesses()

    def set_inline_approval_resolver(self, resolver: InlineApprovalResolver | None) -> None:
        self.human_loop.set_inline_resolver(resolver)

    def register_skill_path(self, path: Path) -> list[SkillMetadata]:
        if self._started:
            raise RuntimeError('Skills must be registered before runtime.start()')
        resolved_path = path.resolve()
        if resolved_path in self._loaded_skill_paths:
            return []
        loader = SkillLoader(
            [resolved_path],
            self.config.security.allowed_commands,
            self.sandbox_manager,
            self.workbench_manager,
        )
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
        if self._started:
            return
        await self.federation_manager.start()
        self.federation_manager.register_tools(self.registry)
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

    async def run(
        self,
        input_text: str,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.scheduler.run(input_text, session_id=session_id, approval_mode=approval_mode)

    async def run_harness(
        self,
        name: str,
        input_text: str,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.harness_runtime.run(name, input_text, session_id=session_id, approval_mode=approval_mode)

    async def run_federated_export(
        self,
        export_name: str,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        if not self._started:
            await self.start()
        export = self._resolve_export(export_name)
        if export.target_type == 'agent':
            return await self.scheduler.run_agent_target(export.target, input_text, session_id=session_id, approval_mode=approval_mode)
        if export.target_type == 'team':
            return await self.scheduler.run_team_target(export.target, input_text, session_id=session_id, approval_mode=approval_mode)
        if export.target_type == 'harness':
            return await self.harness_runtime.run(export.target, input_text, session_id=session_id, approval_mode=approval_mode)
        raise RuntimeError(f'Unsupported federation export type: {export.target_type}')

    async def stream(
        self,
        input_text: str,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self.start()
        stream = self.store.subscribe_events()
        result: dict[str, Any] | None = None
        error: Exception | None = None
        selected_run_id: str | None = None

        async def _runner() -> None:
            nonlocal result, error
            try:
                result = await self.scheduler.run(input_text, session_id=session_id, approval_mode=approval_mode)
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
                        if event['kind'] in {'run_succeeded', 'run_failed', 'run_interrupted', 'run_waiting_approval'} and event['run_id'] == selected_run_id:
                            break
        if error is not None:
            raise error
        if result is not None:
            return

    async def stream_harness(
        self,
        name: str,
        input_text: str,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self.start()
        stream = self.store.subscribe_events()
        result: dict[str, Any] | None = None
        error: Exception | None = None
        selected_run_id: str | None = None

        async def _runner() -> None:
            nonlocal result, error
            try:
                result = await self.harness_runtime.run(name, input_text, session_id=session_id, approval_mode=approval_mode)
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
                        if event['kind'] in {'run_succeeded', 'run_failed', 'run_interrupted', 'run_waiting_approval'} and event['run_id'] == selected_run_id:
                            break
        if error is not None:
            raise error
        if result is not None:
            return

    async def resume(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.scheduler.resume(run_id, checkpoint_id, fork=fork, approval_mode=approval_mode)

    async def replay(self, run_id: str, checkpoint_id: int) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.scheduler.replay(run_id, checkpoint_id)

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        return self.scheduler.list_checkpoints(run_id)

    async def resume_harness(
        self,
        run_id: str,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.harness_runtime.resume(run_id, approval_mode=approval_mode)

    async def resume_stream(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self.start()
        stream = self.store.subscribe_events()
        error: Exception | None = None
        target_run_id: str | None = None

        async def _runner() -> None:
            nonlocal error, target_run_id
            try:
                result = await self.scheduler.resume(run_id, checkpoint_id, fork=fork, approval_mode=approval_mode)
                target_run_id = str(result['run_id'])
            except Exception as exc:
                error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_runner)
            async with stream:
                async for event in stream:
                    if target_run_id is None and event['kind'] == 'run_resumed':
                        target_run_id = str(event['run_id'])
                    if target_run_id is None or event['run_id'] == target_run_id:
                        yield event
                        if event['kind'] in {'run_succeeded', 'run_failed', 'run_interrupted', 'run_waiting_approval'}:
                            break
        if error is not None:
            raise error

    async def resume_harness_stream(
        self,
        run_id: str,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self.start()
        stream = self.store.subscribe_events()
        error: Exception | None = None

        async def _runner() -> None:
            nonlocal error
            try:
                await self.harness_runtime.resume(run_id, approval_mode=approval_mode)
            except Exception as exc:
                error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_runner)
            async with stream:
                async for event in stream:
                    if event['run_id'] == run_id:
                        yield event
                        if event['kind'] in {'run_succeeded', 'run_failed', 'run_interrupted', 'run_waiting_approval'}:
                            break
        if error is not None:
            raise error

    def list_human_requests(self, status: HumanRequestStatus | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.store.list_human_requests(status=status, run_id=run_id)]

    def load_human_request(self, request_id: str) -> dict[str, Any]:
        return self.store.load_human_request(request_id).model_dump()

    def approve_human_request(self, request_id: str, response_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.store.resolve_human_request(
            request_id,
            status=HumanRequestStatus.APPROVED,
            response_payload=response_payload,
        ).model_dump()

    def reject_human_request(self, request_id: str, response_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.store.resolve_human_request(
            request_id,
            status=HumanRequestStatus.REJECTED,
            response_payload=response_payload,
        ).model_dump()

    def interrupt_run(self, run_id: str, payload: dict[str, Any] | None = None) -> None:
        self.store.request_interrupt(run_id, payload or {'reason': 'user requested interrupt'})
        self.store.record_event(
            run_id,
            'interrupt_requested',
            payload or {'reason': 'user requested interrupt'},
            scope='human',
            span_id=f'human:interrupt:{run_id}',
        )

    async def list_remotes(self) -> list[dict[str, Any]]:
        if not self._started:
            await self.start()
        return await self.federation_manager.list_remotes()

    async def inspect_remote(self, remote_name: str) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return await self.federation_manager.inspect_remote(remote_name)

    def serve_federation(self) -> dict[str, Any]:
        return self._federation_server.start()

    def stop_federation(self) -> None:
        self._federation_server.stop()

    def list_workbench_sessions(self, owner_run_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                'session_id': item.session_id,
                'owner_run_id': item.owner_run_id,
                'name': item.name,
                'root_path': str(item.root_path),
                'status': item.status,
                'executor_name': item.executor_name,
                'branch_parent_session_id': item.branch_parent_session_id,
                'expires_at': item.expires_at,
                'metadata': item.metadata,
            }
            for item in self.workbench_manager.list_sessions(owner_run_id=owner_run_id)
        ]

    def gc_workbench_sessions(self) -> list[str]:
        return self.workbench_manager.gc_expired()

    async def aclose(self) -> None:
        self.stop_federation()
        await self.federation_manager.aclose()
        await self.mcp_manager.aclose()
        await self.model_client.aclose()
        self._started = False

    def _resolve_export(self, export_name: str) -> FederationExportConfig:
        try:
            return self.config.federation_export_map[export_name]
        except KeyError as exc:
            raise RuntimeError(f'Unknown federation export: {export_name}') from exc



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
    workbench_manager = WorkbenchManager(
        store,
        sandbox_manager,
        Path(config.workbench.root),
        default_executor=config.workbench.default_executor,
        session_ttl_seconds=config.workbench.session_ttl_seconds,
    )
    guardrail_engine = GuardrailEngine(
        tool_input_hooks=config.guardrails.tool_input_hooks,
        final_output_hooks=config.guardrails.final_output_hooks,
    )
    human_loop = HumanLoopManager(store, config.security.human_loop)
    model_client = HttpModelClient(config.model)
    federation_manager = FederationClientManager(config.federation, store=store)
    mcp_manager = McpClientManager(
        config.mcp,
        sandbox_manager,
        workbench_manager=workbench_manager,
        store=store,
        model_client=model_client,
        human_loop=human_loop,
    )
    orchestrator = AgentOrchestrator(config, model_client, registry, store, guardrail_engine, human_loop)
    scheduler = GraphScheduler(
        config,
        registry,
        orchestrator,
        store,
        mcp_manager,
        guardrail_engine,
        human_loop,
        workbench_manager=workbench_manager,
        federation_manager=federation_manager,
    )
    harness_runtime = HarnessRuntime(
        config,
        orchestrator,
        store,
        guardrail_engine,
        human_loop,
        workbench_manager=workbench_manager,
    )
    runtime = EasyAgentRuntime(
        config,
        model_client,
        registry,
        store,
        sandbox_manager,
        workbench_manager,
        mcp_manager,
        federation_manager,
        guardrail_engine,
        human_loop,
        orchestrator,
        scheduler,
        harness_runtime,
    )
    for plugin_source in config.plugins:
        runtime.load(plugin_source)
    if config.skills:
        runtime.load(InlineRuntimePlugin(skill_paths=[Path(item.path) for item in config.skills]))
    orchestrator.register_subagent_tools()
    return runtime



def build_runtime(config_path: str | Path) -> EasyAgentRuntime:
    return build_runtime_from_config(load_config(config_path))
