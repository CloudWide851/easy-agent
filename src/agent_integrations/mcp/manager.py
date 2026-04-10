from __future__ import annotations

from typing import Any

from agent_common.models import RunContext, ToolSpec
from agent_config.app import McpServerConfig
from agent_integrations.human_loop import HumanLoopManager
from agent_integrations.sandbox import SandboxManager
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager

from .clients import (
    BaseMcpClient,
    CallbackHandler,
    LegacyHttpSseRpcClient,
    RedirectHandler,
    StdioMcpClient,
    StreamableHttpMcpClient,
    _DefaultSamplingModelClient,
)


class McpClientManager:
    def __init__(
        self,
        configs: list[McpServerConfig],
        sandbox_manager: SandboxManager,
        workbench_manager: WorkbenchManager | None = None,
        store: SQLiteRunStore | None = None,
        model_client: Any | None = None,
        human_loop: HumanLoopManager | None = None,
    ) -> None:
        self._sandbox_manager = sandbox_manager
        self._workbench_manager = workbench_manager
        self._store = store
        self._model_client = model_client or _DefaultSamplingModelClient()
        self._human_loop = human_loop
        self._clients: dict[str, BaseMcpClient] = {}
        self._started = False
        self._tool_cache: dict[str, list[ToolSpec]] = {}
        self._redirect_handler: RedirectHandler | None = None
        self._callback_handler: CallbackHandler | None = None
        for config in configs:
            self.add_server(config)

    def set_oauth_handlers(
        self,
        redirect_handler: RedirectHandler | None,
        callback_handler: CallbackHandler | None,
    ) -> None:
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler
        for client in self._clients.values():
            client._redirect_handler = redirect_handler
            client._callback_handler = callback_handler

    def add_server(self, config: McpServerConfig) -> None:
        if self._started:
            raise RuntimeError('MCP servers cannot be added after manager.start()')
        self._clients[config.name] = self._build_client(config)

    def _build_client(self, config: McpServerConfig) -> BaseMcpClient:
        if config.transport == 'stdio':
            return StdioMcpClient(
                config,
                self._sandbox_manager,
                self._workbench_manager,
                self._store,
                self._model_client,
                self._human_loop,
                self._redirect_handler,
                self._callback_handler,
            )
        if config.transport == 'http_sse':
            return LegacyHttpSseRpcClient(
                config,
                self._store,
                self._model_client,
                self._human_loop,
                self._redirect_handler,
                self._callback_handler,
            )
        if config.transport == 'streamable_http':
            return StreamableHttpMcpClient(
                config,
                self._store,
                self._model_client,
                self._human_loop,
                self._redirect_handler,
                self._callback_handler,
            )
        raise ValueError(f'Unsupported MCP transport: {config.transport}')

    async def start(self) -> None:
        for client in self._clients.values():
            await client.start()
        self._started = True
        await self.refresh_tools()

    async def refresh_tools(self) -> dict[str, list[ToolSpec]]:
        result: dict[str, list[ToolSpec]] = {}
        for name, client in self._clients.items():
            result[name] = await client.list_tools()
        self._tool_cache = result
        return result

    async def list_servers(self) -> dict[str, list[ToolSpec]]:
        if not self._tool_cache:
            return await self.refresh_tools()
        return self._tool_cache

    def capability_summary(self) -> dict[str, dict[str, Any]]:
        return {name: client.capabilities for name, client in self._clients.items()}

    async def list_roots(self, server_name: str) -> list[dict[str, Any]]:
        return await self._clients[server_name].list_roots()

    async def refresh_roots(self, server_name: str) -> dict[str, Any]:
        return await self._clients[server_name].refresh_roots()

    async def authorize(self, server_name: str) -> None:
        await self._clients[server_name].authorize()

    def auth_status(self, server_name: str) -> dict[str, Any]:
        return self._clients[server_name].auth_status()

    async def logout(self, server_name: str) -> None:
        await self._clients[server_name].logout()

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: RunContext | None = None,
    ) -> Any:
        if context is not None and self._store is not None:
            self._store.record_event(
                context.run_id,
                'mcp_call_started',
                {'server': server_name, 'tool': tool_name, 'arguments': arguments},
                scope='mcp',
                node_id=context.node_id,
                span_id=f'mcp:{server_name}:{tool_name}',
            )
        client = self._clients[server_name]
        token = client.bind_run_context(context)
        try:
            result = await client.call_tool(tool_name, arguments)
        except Exception as exc:
            if context is not None and self._store is not None:
                self._store.record_event(
                    context.run_id,
                    'mcp_call_failed',
                    {'server': server_name, 'tool': tool_name, 'arguments': arguments, 'error': str(exc)},
                    scope='mcp',
                    node_id=context.node_id,
                    span_id=f'mcp:{server_name}:{tool_name}',
                )
            raise
        finally:
            client.reset_run_context(token)
        if context is not None and self._store is not None:
            self._store.record_event(
                context.run_id,
                'mcp_call_succeeded',
                {'server': server_name, 'tool': tool_name, 'arguments': arguments, 'result': result},
                scope='mcp',
                node_id=context.node_id,
                span_id=f'mcp:{server_name}:{tool_name}',
            )
        return result

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._started = False
        self._tool_cache = {}
