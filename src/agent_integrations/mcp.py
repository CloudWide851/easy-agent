from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import mcp.types as mcp_types
from anyio.streams.text import TextReceiveStream
from httpx_sse import aconnect_sse
from mcp import ClientSession
from mcp.client.stdio import get_default_environment
from mcp.os.posix.utilities import terminate_posix_process_tree
from mcp.os.win32.utilities import (
    _create_windows_fallback_process,
    create_windows_process,
    get_windows_executable_command,
    terminate_windows_process_tree,
)
from mcp.shared.message import SessionMessage

from agent_common.models import RunContext, ToolSpec
from agent_config.app import McpServerConfig
from agent_integrations.sandbox import SandboxManager, SandboxRequest, SandboxTarget
from agent_integrations.storage import SQLiteRunStore


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    import re

    combined = f'mcp__{server_name}__{tool_name}'
    return re.sub(r'[^a-zA-Z0-9_-]', '_', combined)


class BaseMcpClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config

    async def start(self) -> None:
        raise NotImplementedError

    async def list_tools(self) -> list[ToolSpec]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


class StdioMcpClient(BaseMcpClient):
    def __init__(self, config: McpServerConfig, sandbox_manager: SandboxManager) -> None:
        super().__init__(config)
        self._sandbox_manager = sandbox_manager
        self._session: ClientSession | None = None
        self._process: Any = None
        self._task_group: Any = None
        self._read_stream: Any = None
        self._read_stream_writer: Any = None
        self._write_stream: Any = None
        self._write_stream_reader: Any = None

    async def start(self) -> None:
        if not self.config.command:
            raise ValueError('stdio MCP transport requires a command')
        prepared = self._sandbox_manager.prepare(
            SandboxRequest(
                command=self.config.command,
                cwd=Path.cwd(),
                env=self.config.env,
                timeout_seconds=self.config.timeout_seconds,
                target=SandboxTarget.STDIO_MCP,
            )
        )
        environment = (
            {**get_default_environment(), **prepared.env}
            if prepared.env is not None
            else get_default_environment()
        )
        self._process = await self._open_process(
            command=prepared.command[0],
            args=prepared.command[1:],
            env=environment,
            cwd=prepared.cwd,
        )
        self._read_stream_writer, self._read_stream = anyio.create_memory_object_stream(0)
        self._write_stream, self._write_stream_reader = anyio.create_memory_object_stream(0)
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._stdout_reader)
        self._task_group.start_soon(self._stdin_writer)
        session = ClientSession(self._read_stream, self._write_stream)
        self._session = await session.__aenter__()
        await self._session.initialize()

    async def _open_process(
        self,
        command: str,
        args: list[str],
        env: dict[str, str],
        cwd: str | Path | None,
    ) -> Any:
        if sys.platform == 'win32':
            resolved_command = get_windows_executable_command(command)
            try:
                return await create_windows_process(resolved_command, args, env, sys.stderr, cwd)
            except (OSError, PermissionError):
                return await _create_windows_fallback_process(resolved_command, args, env, sys.stderr, cwd)
        return await anyio.open_process(
            [command, *args],
            env=env,
            stderr=sys.stderr,
            cwd=cwd,
            start_new_session=True,
        )

    async def _stdout_reader(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        assert self._read_stream_writer is not None
        try:
            async with self._read_stream_writer:
                buffer = ''
                async for chunk in TextReceiveStream(self._process.stdout, encoding='utf-8', errors='strict'):
                    lines = (buffer + chunk).split('\n')
                    buffer = lines.pop()
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            message = mcp_types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            await self._read_stream_writer.send(exc)
                            continue
                        await self._read_stream_writer.send(SessionMessage(message=message))
                if buffer.strip():
                    try:
                        message = mcp_types.JSONRPCMessage.model_validate_json(buffer)
                    except Exception as exc:
                        await self._read_stream_writer.send(exc)
                    else:
                        await self._read_stream_writer.send(SessionMessage(message=message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def _stdin_writer(self) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._write_stream_reader is not None
        try:
            async with self._write_stream_reader:
                async for session_message in self._write_stream_reader:
                    payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    await self._process.stdin.send(payload.encode('utf-8') + b'\n')
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def list_tools(self) -> list[ToolSpec]:
        if self._session is None:
            raise RuntimeError('MCP stdio session is not running')
        result = await self._session.list_tools()
        return [
            ToolSpec(
                name=item.name,
                description=item.description or '',
                input_schema=item.inputSchema or {'type': 'object'},
            )
            for item in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError('MCP stdio session is not running')
        result = await self._session.call_tool(name, arguments)
        if result.structuredContent is not None:
            return result.structuredContent
        return [item.model_dump(by_alias=True, exclude_none=True) for item in result.content]

    async def aclose(self) -> None:
        errors: list[Exception] = []
        with anyio.CancelScope(shield=True):
            if self._session is not None:
                task_group = getattr(self._session, '_task_group', None)
                if task_group is not None:
                    try:
                        task_group.cancel_scope.cancel()
                        await anyio.lowlevel.checkpoint()
                    except Exception:
                        pass
                self._session = None
            if self._write_stream_reader is not None:
                try:
                    await self._write_stream_reader.aclose()
                except Exception:
                    pass
            if self._read_stream_writer is not None:
                try:
                    await self._read_stream_writer.aclose()
                except Exception:
                    pass
            if self._process is not None and getattr(self._process, 'stdin', None) is not None:
                try:
                    await self._process.stdin.aclose()
                except BaseException:
                    pass
            if self._process is not None:
                try:
                    with anyio.fail_after(2):
                        await self._process.wait()
                except TimeoutError:
                    await self._terminate_process()
                except ProcessLookupError:
                    pass
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        errors.append(exc)
            if self._task_group is not None:
                try:
                    self._task_group.cancel_scope.cancel()
                    await anyio.lowlevel.checkpoint()
                except Exception:
                    pass
            for stream_name in ('stdout', 'stdin'):
                stream = getattr(self._process, stream_name, None) if self._process is not None else None
                if stream is not None:
                    try:
                        await stream.aclose()
                    except BaseException:
                        pass
            stderr = getattr(self._process, 'stderr', None) if self._process is not None else None
            if stderr is not None and hasattr(stderr, 'close'):
                try:
                    stderr.close()
                except Exception:
                    pass
            if self._read_stream is not None:
                try:
                    await self._read_stream.aclose()
                except Exception:
                    pass
            if self._write_stream is not None:
                try:
                    await self._write_stream.aclose()
                except Exception:
                    pass
        self._process = None
        self._task_group = None
        self._read_stream = None
        self._read_stream_writer = None
        self._write_stream = None
        self._write_stream_reader = None
        if errors:
            raise errors[0]

    async def _terminate_process(self) -> None:
        if self._process is None:
            return
        if sys.platform == 'win32':
            await terminate_windows_process_tree(self._process, 2.0)
            return
        await terminate_posix_process_tree(self._process, 2.0)


class HttpSseMcpClient(BaseMcpClient):
    def __init__(self, config: McpServerConfig) -> None:
        super().__init__(config)
        self._client = httpx.AsyncClient(timeout=config.timeout_seconds)
        self.notifications: list[dict[str, Any]] = []
        self._sse_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.config.sse_url:
            self._sse_task = asyncio.create_task(self._consume_sse(self.config.sse_url))

    async def _consume_sse(self, url: str) -> None:
        async with aconnect_sse(self._client, 'GET', url) as event_source:
            async for event in event_source.aiter_sse():
                if event.data:
                    self.notifications.append(cast(dict[str, Any], json.loads(event.data)))
                    return

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.config.rpc_url:
            raise ValueError('http_sse transport requires rpc_url')
        response = await self._client.post(
            self.config.rpc_url,
            json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['result'])

    async def list_tools(self) -> list[ToolSpec]:
        result = await self._rpc('tools/list', {})
        return [
            ToolSpec(
                name=item['name'],
                description=item.get('description', ''),
                input_schema=item.get('inputSchema', {'type': 'object'}),
            )
            for item in result.get('tools', [])
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._rpc('tools/call', {'name': name, 'arguments': arguments})
        return result.get('content', result)

    async def aclose(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
        await self._client.aclose()


class McpClientManager:
    def __init__(self, configs: list[McpServerConfig], sandbox_manager: SandboxManager, store: SQLiteRunStore | None = None) -> None:
        self._sandbox_manager = sandbox_manager
        self._store = store
        self._clients: dict[str, BaseMcpClient] = {}
        self._started = False
        self._tool_cache: dict[str, list[ToolSpec]] = {}
        for config in configs:
            self.add_server(config)

    def add_server(self, config: McpServerConfig) -> None:
        if self._started:
            raise RuntimeError('MCP servers cannot be added after manager.start()')
        self._clients[config.name] = self._build_client(config)

    def _build_client(self, config: McpServerConfig) -> BaseMcpClient:
        if config.transport == 'stdio':
            return StdioMcpClient(config, self._sandbox_manager)
        if config.transport == 'http_sse':
            return HttpSseMcpClient(config)
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
        try:
            result = await self._clients[server_name].call_tool(tool_name, arguments)
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



