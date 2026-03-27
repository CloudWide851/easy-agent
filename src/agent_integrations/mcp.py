from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import mcp.types as mcp_types
from anyio.streams.text import TextReceiveStream
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment
from mcp.client.streamable_http import streamablehttp_client
from mcp.os.posix.utilities import terminate_posix_process_tree
from mcp.os.win32.utilities import (
    _create_windows_fallback_process,
    create_windows_process,
    get_windows_executable_command,
    terminate_windows_process_tree,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.message import SessionMessage

from agent_common.models import ChatMessage, McpAuthType, RunContext, ToolSpec
from agent_config.app import McpRootConfig, McpServerConfig
from agent_integrations.human_loop import HumanLoopManager
from agent_integrations.sandbox import SandboxManager, SandboxRequest, SandboxTarget
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager

RedirectHandler = Callable[[str], Awaitable[None]]
CallbackHandler = Callable[[], Awaitable[tuple[str, str | None]]]


class _DefaultSamplingModelClient:
    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> Any:
        del tools
        text = messages[-1].content if messages else ''
        return type('Response', (), {'text': text, 'tool_calls': []})()



def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    import re

    combined = f'mcp__{server_name}__{tool_name}'
    return re.sub(r'[^a-zA-Z0-9_-]', '_', combined)


class OAuthTokenStore:
    def __init__(self, store: SQLiteRunStore, server_name: str) -> None:
        self.store = store
        self.server_name = server_name

    async def get_tokens(self) -> OAuthToken | None:
        payload = self.store.load_oauth_tokens(self.server_name)
        if payload is None:
            return None
        return OAuthToken.model_validate(payload)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.store.save_oauth_tokens(self.server_name, tokens.model_dump(mode='json', exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        payload = self.store.load_oauth_client_info(self.server_name)
        if payload is None:
            return None
        return OAuthClientInformationFull.model_validate(payload)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_oauth_client_info(self.server_name, client_info.model_dump(mode='json', exclude_none=True))


class BaseMcpClient:
    def __init__(
        self,
        config: McpServerConfig,
        store: SQLiteRunStore | None,
        model_client: Any,
        human_loop: HumanLoopManager | None,
        redirect_handler: RedirectHandler | None,
        callback_handler: CallbackHandler | None,
    ) -> None:
        self.config = config
        self._store = store
        self._model_client = model_client or _DefaultSamplingModelClient()
        self._human_loop = human_loop
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler
        self.capabilities: dict[str, Any] = {}

    async def start(self) -> None:
        raise NotImplementedError

    async def list_tools(self) -> list[ToolSpec]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def list_roots(self) -> list[dict[str, Any]]:
        return [self._root_payload(item) for item in self._resolved_roots()]

    async def refresh_roots(self) -> None:
        return None

    async def authorize(self) -> None:
        return None

    def auth_status(self) -> dict[str, Any]:
        tokens = self._store.load_oauth_tokens(self.config.name) if self._store is not None else None
        return {
            'server': self.config.name,
            'auth_type': self.config.auth.type.value,
            'has_tokens': tokens is not None,
        }

    async def logout(self) -> None:
        if self._store is not None:
            self._store.clear_oauth_state(self.config.name)

    async def aclose(self) -> None:
        raise NotImplementedError

    def _build_headers(self) -> dict[str, str]:
        headers = dict(self.config.headers)
        auth = self.config.auth
        if auth.type is McpAuthType.BEARER_ENV and auth.token_env:
            token = os.environ.get(auth.token_env, '').strip()
            if token:
                headers[auth.header_name] = f'{auth.value_prefix}{token}'
        if auth.type is McpAuthType.HEADER_ENV and auth.header_env:
            raw = os.environ.get(auth.header_env, '').strip()
            if raw:
                headers[auth.header_name] = raw
        return headers

    def _build_auth(self) -> httpx.Auth | None:
        if self.config.auth.type is not McpAuthType.OAUTH:
            return None
        if self._store is None:
            raise RuntimeError('OAuth transport requires a run store')
        redirect_handler = self._redirect_handler or self._default_redirect_handler
        callback_handler = self._callback_handler or self._default_callback_handler
        return OAuthClientProvider(
            self.config.url or self.config.rpc_url or self.config.sse_url or '',
            OAuthClientMetadata(
                redirect_uris=[cast(Any, self.config.auth.redirect_uri)],
                grant_types=['authorization_code', 'refresh_token'],
                response_types=['code'],
                token_endpoint_auth_method='none',
                scope=' '.join(self.config.auth.scopes),
                client_name=self.config.auth.client_name,
            ),
            OAuthTokenStore(self._store, self.config.name),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

    async def _default_redirect_handler(self, url: str) -> None:
        raise RuntimeError(
            f'MCP server {self.config.name} requires OAuth login. '
            f'Use `easy-agent mcp auth login {self.config.name}`. URL: {url}'
        )

    async def _default_callback_handler(self) -> tuple[str, str | None]:
        raise RuntimeError(
            f'MCP server {self.config.name} requires OAuth login. '
            f'Use `easy-agent mcp auth login {self.config.name}`.'
        )

    async def _sampling_callback(
        self,
        context: Any,
        params: mcp_types.CreateMessageRequestParams,
    ) -> mcp_types.CreateMessageResult | mcp_types.ErrorData:
        del context
        if self._human_loop is not None and self._human_loop.config.approve_mcp_sampling:
            approval_context = RunContext(run_id='mcp-sampling', workdir=Path.cwd(), node_id=None)
            await self._human_loop.require_approval(
                approval_context,
                request_key=(
                    f'mcp_sampling:{self.config.name}:'
                    f'{self._human_loop.stable_key(params.model_dump(mode="json", exclude_none=True))}'
                ),
                kind='mcp_sampling',
                title=f'Approve MCP sampling request for {self.config.name}',
                payload={'server': self.config.name, 'sampling': params.model_dump(mode='json', exclude_none=True)},
            )
        messages: list[ChatMessage] = []
        if params.systemPrompt:
            messages.append(ChatMessage(role='system', content=params.systemPrompt))
        for item in params.messages:
            text = _sampling_message_to_text(item)
            if not text:
                return mcp_types.ErrorData(code=mcp_types.INVALID_REQUEST, message='Only text-first sampling is supported')
            if item.role == 'assistant':
                messages.append(ChatMessage(role='assistant', content=text))
            else:
                messages.append(ChatMessage(role='user', content=text))
        response = await self._model_client.complete(messages, [])
        return mcp_types.CreateMessageResult(
            role='assistant',
            content=mcp_types.TextContent(type='text', text=response.text),
            model=getattr(self._model_client, 'model_name', 'easy-agent'),
            stopReason='endTurn',
        )

    async def _elicitation_callback(
        self,
        context: Any,
        params: mcp_types.ElicitRequestParams,
    ) -> mcp_types.ElicitResult | mcp_types.ErrorData:
        del context
        if self._human_loop is None:
            return mcp_types.ErrorData(code=mcp_types.INVALID_REQUEST, message='Elicitation requires a human loop')
        approval_context = RunContext(run_id='mcp-elicitation', workdir=Path.cwd(), node_id=None)
        response_payload = await self._human_loop.require_approval(
            approval_context,
            request_key=(
                f'mcp_elicitation:{self.config.name}:'
                f'{self._human_loop.stable_key(params.model_dump(mode="json", exclude_none=True))}'
            ),
            kind='mcp_elicitation',
            title=f'Approve MCP elicitation request for {self.config.name}',
            payload={'server': self.config.name, 'elicitation': params.model_dump(mode='json', exclude_none=True)},
        )
        action = str(response_payload.get('action') or 'accept')
        if action not in {'accept', 'decline', 'cancel'}:
            action = 'accept'
        return mcp_types.ElicitResult(action=cast(Any, action), content=cast(dict[str, Any], response_payload.get('content', {})))

    async def _roots_callback(
        self,
        context: Any,
    ) -> mcp_types.ListRootsResult | mcp_types.ErrorData:
        del context
        return mcp_types.ListRootsResult(
            roots=[
                mcp_types.Root(uri=cast(Any, _root_to_uri(item.path)), name=item.name)
                for item in self._resolved_roots()
            ]
        )

    def _resolved_roots(self) -> list[McpRootConfig]:
        if self.config.roots:
            return list(self.config.roots)
        return self._infer_stdio_filesystem_roots()

    def _infer_stdio_filesystem_roots(self) -> list[McpRootConfig]:
        if self.config.transport != 'stdio' or not self.config.command:
            return []
        package_index = next((index for index, item in enumerate(self.config.command) if 'server-filesystem' in item), None)
        if package_index is None:
            return []
        roots: list[McpRootConfig] = []
        for raw in self.config.command[package_index + 1:]:
            if raw.startswith('-'):
                continue
            name = Path(raw).name or None
            roots.append(McpRootConfig(path=raw, name=name))
        return roots

    @staticmethod
    def _root_payload(root: Any) -> dict[str, Any]:
        return {'path': root.path, 'name': root.name, 'uri': _root_to_uri(root.path)}


class SessionBackedMcpClient(BaseMcpClient):
    def __init__(
        self,
        config: McpServerConfig,
        store: SQLiteRunStore | None,
        model_client: Any,
        human_loop: HumanLoopManager | None,
        redirect_handler: RedirectHandler | None,
        callback_handler: CallbackHandler | None,
    ) -> None:
        super().__init__(config, store, model_client, human_loop, redirect_handler, callback_handler)
        self._session: ClientSession | None = None
        self._transport_cm: Any = None

    async def start(self) -> None:
        read_stream, write_stream = await self._open_transport()
        session = ClientSession(
            read_stream,
            write_stream,
            sampling_callback=self._sampling_callback,
            elicitation_callback=self._elicitation_callback,
            list_roots_callback=self._roots_callback,
        )
        self._session = await session.__aenter__()
        result = await self._session.initialize()
        self.capabilities = result.capabilities.model_dump(mode='json', exclude_none=True)

    async def list_tools(self) -> list[ToolSpec]:
        if self._session is None:
            raise RuntimeError('MCP session is not running')
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
            raise RuntimeError('MCP session is not running')
        result = await self._session.call_tool(name, arguments)
        if result.structuredContent is not None:
            return result.structuredContent
        return [item.model_dump(by_alias=True, exclude_none=True) for item in result.content]

    async def refresh_roots(self) -> None:
        if self._session is not None:
            await self._session.send_roots_list_changed()

    async def authorize(self) -> None:
        await self.list_tools()

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._transport_cm is not None:
            await self._transport_cm.__aexit__(None, None, None)
            self._transport_cm = None

    async def _open_transport(self) -> tuple[Any, Any]:
        raise NotImplementedError


class StdioMcpClient(SessionBackedMcpClient):
    def __init__(
        self,
        config: McpServerConfig,
        sandbox_manager: SandboxManager,
        workbench_manager: WorkbenchManager | None,
        store: SQLiteRunStore | None,
        model_client: Any,
        human_loop: HumanLoopManager | None,
        redirect_handler: RedirectHandler | None,
        callback_handler: CallbackHandler | None,
    ) -> None:
        super().__init__(config, store, model_client, human_loop, redirect_handler, callback_handler)
        self._sandbox_manager = sandbox_manager
        self._workbench_manager = workbench_manager
        self._process: Any = None
        self._task_group: Any = None
        self._read_stream: Any = None
        self._read_stream_writer: Any = None
        self._write_stream: Any = None
        self._write_stream_reader: Any = None

    async def _open_transport(self) -> tuple[Any, Any]:
        if not self.config.command:
            raise ValueError('stdio MCP transport requires a command')
        if self._workbench_manager is not None:
            session = self._workbench_manager.ensure_session(
                f'mcp:{self.config.name}',
                f'mcp-{self.config.name}',
                metadata={'server': self.config.name, 'transport': self.config.transport},
            )
            prepared = self._workbench_manager.prepare_subprocess(
                session.session_id,
                self.config.command,
                env=self.config.env,
                timeout_seconds=self.config.timeout_seconds,
                target=SandboxTarget.STDIO_MCP,
            )
        else:
            prepared = self._sandbox_manager.prepare(
                SandboxRequest(
                    command=self.config.command,
                    cwd=Path.cwd(),
                    env=self.config.env,
                    timeout_seconds=self.config.timeout_seconds,
                    target=SandboxTarget.STDIO_MCP,
                )
            )
        environment = {**get_default_environment(), **prepared.env} if prepared.env is not None else get_default_environment()
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
        return self._read_stream, self._write_stream

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        with anyio.CancelScope(shield=True):
            if self._write_stream_reader is not None:
                await self._write_stream_reader.aclose()
            if self._read_stream_writer is not None:
                await self._read_stream_writer.aclose()
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
            if self._task_group is not None:
                self._task_group.cancel_scope.cancel()
            for stream_name in ('stdout', 'stdin'):
                stream = getattr(self._process, stream_name, None) if self._process is not None else None
                if stream is not None:
                    try:
                        await stream.aclose()
                    except BaseException:
                        pass
        self._process = None
        self._task_group = None
        self._read_stream = None
        self._read_stream_writer = None
        self._write_stream = None
        self._write_stream_reader = None

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

    async def _terminate_process(self) -> None:
        if self._process is None:
            return
        if sys.platform == 'win32':
            await terminate_windows_process_tree(self._process, 2.0)
            return
        await terminate_posix_process_tree(self._process, 2.0)


class LegacyHttpSseRpcClient(BaseMcpClient):
    def __init__(
        self,
        config: McpServerConfig,
        store: SQLiteRunStore | None,
        model_client: Any,
        human_loop: HumanLoopManager | None,
        redirect_handler: RedirectHandler | None,
        callback_handler: CallbackHandler | None,
    ) -> None:
        super().__init__(config, store, model_client, human_loop, redirect_handler, callback_handler)
        self._client = httpx.AsyncClient(timeout=config.timeout_seconds, headers=self._build_headers(), auth=self._build_auth())
        self._sse_task: Any = None

    async def start(self) -> None:
        self.capabilities = {'legacy_http_sse': True}

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

    async def authorize(self) -> None:
        await self.list_tools()

    async def aclose(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
        await self._client.aclose()


class StreamableHttpMcpClient(SessionBackedMcpClient):
    async def _open_transport(self) -> tuple[Any, Any]:
        self._transport_cm = streamablehttp_client(
            self.config.url or '',
            headers=self._build_headers(),
            timeout=self.config.timeout_seconds,
            auth=self._build_auth(),
        )
        read_stream, write_stream, _ = await self._transport_cm.__aenter__()
        return read_stream, write_stream


class LegacyHttpSseMcpClient(SessionBackedMcpClient):
    async def _open_transport(self) -> tuple[Any, Any]:
        self._transport_cm = sse_client(
            self.config.sse_url or '',
            headers=self._build_headers(),
            timeout=self.config.timeout_seconds,
            sse_read_timeout=max(self.config.timeout_seconds, 30),
            auth=self._build_auth(),
        )
        read_stream, write_stream = await self._transport_cm.__aenter__()
        return read_stream, write_stream


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

    async def refresh_roots(self, server_name: str) -> None:
        await self._clients[server_name].refresh_roots()

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



def _sampling_message_to_text(message: mcp_types.SamplingMessage) -> str:
    content = message.content
    if isinstance(content, list):
        parts = [_content_block_to_text(item) for item in content]
        if any(part is None for part in parts):
            return ''
        return '\n'.join(part for part in parts if part)
    single = _content_block_to_text(content)
    return single or ''



def _content_block_to_text(content: Any) -> str | None:
    if isinstance(content, mcp_types.TextContent):
        return content.text
    text = getattr(content, 'text', None)
    content_type = getattr(content, 'type', None)
    if isinstance(text, str) and content_type == 'text':
        return text
    return None



def _root_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()
