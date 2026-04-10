from __future__ import annotations

import contextvars
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
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

from agent_common.models import (
    ChatMessage,
    HumanLoopMode,
    HumanRequestStatus,
    McpAuthType,
    RunContext,
    ToolSpec,
)
from agent_config.app import McpRootConfig, McpServerConfig
from agent_integrations.human_loop import HumanLoopManager
from agent_integrations.sandbox import SandboxManager, SandboxRequest, SandboxTarget
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager

from .elicitation import (
    classify_elicitation_request,
    classify_sampling_request,
    coerce_elicitation_result,
    normalize_requested_schema,
    sampling_message_to_text,
    url_host,
)
from .roots import (
    diff_root_entries,
    infer_stdio_filesystem_roots,
    normalize_root_entries,
    root_payload,
    root_to_uri,
)

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


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


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
        self._run_context: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
            f'mcp_run_context_{config.name}',
            default=None,
        )

    async def start(self) -> None:
        raise NotImplementedError

    async def list_tools(self) -> list[ToolSpec]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def list_roots(self) -> list[dict[str, Any]]:
        roots = normalize_root_entries([root_payload(item) for item in self._resolved_roots()])
        self._ensure_root_snapshot(roots)
        return roots

    async def refresh_roots(self) -> dict[str, Any]:
        roots = normalize_root_entries([root_payload(item) for item in self._resolved_roots()])
        previous = self._load_root_snapshot()
        if previous is None:
            self._save_root_snapshot(roots)
            return {
                'server': self.config.name,
                'changed': False,
                'notification_sent': False,
                'reason': 'snapshot_initialized',
                'roots': roots,
                'diff': {'added': [], 'removed': [], 'changed': []},
            }
        diff = diff_root_entries(previous['roots'], roots)
        changed = bool(diff['added'] or diff['removed'] or diff['changed'])
        self._save_root_snapshot(roots)
        return {
            'server': self.config.name,
            'changed': changed,
            'notification_sent': False,
            'reason': 'notification_unsupported' if changed else 'unchanged',
            'roots': roots,
            'diff': diff,
        }

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

    def bind_run_context(self, context: RunContext | None) -> contextvars.Token[RunContext | None]:
        return self._run_context.set(context)

    def reset_run_context(self, token: contextvars.Token[RunContext | None]) -> None:
        self._run_context.reset(token)

    def _approval_run_context(self) -> RunContext:
        context = self._run_context.get()
        if context is not None:
            return context
        return RunContext(run_id=f'mcp-{self.config.name}', workdir=Path.cwd(), node_id=None)

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

    def _approval_context_for_risk(self, risk_level: str) -> RunContext:
        context = self._approval_run_context()
        if risk_level == 'high' and context.approval_mode is not HumanLoopMode.DEFERRED:
            return replace(context, approval_mode=HumanLoopMode.DEFERRED)
        return context

    def _resolved_roots(self) -> list[McpRootConfig]:
        if self.config.roots:
            return list(self.config.roots)
        return infer_stdio_filesystem_roots(self.config.transport, self.config.command)

    def _supports_server_roots(self) -> bool:
        return not self._is_stdio_filesystem_server()

    def _is_stdio_filesystem_server(self) -> bool:
        return self.config.transport == 'stdio' and any('server-filesystem' in item for item in self.config.command)

    def _load_root_snapshot(self) -> dict[str, Any] | None:
        if self._store is None:
            return None
        return self._store.load_mcp_root_snapshot(self.config.name)

    def _ensure_root_snapshot(self, roots: list[dict[str, Any]]) -> None:
        if self._store is None or self._store.load_mcp_root_snapshot(self.config.name) is not None:
            return
        self._store.save_mcp_root_snapshot(self.config.name, roots)

    def _save_root_snapshot(self, roots: list[dict[str, Any]], *, last_notified_at: str | None = None) -> None:
        if self._store is None:
            return
        self._store.save_mcp_root_snapshot(self.config.name, roots, last_notified_at=last_notified_at)

    def _sampling_approval_payload(
        self,
        params: mcp_types.CreateMessageRequestParams,
        risk_level: str,
        risk_reasons: list[str],
    ) -> dict[str, Any]:
        preview_parts = [sampling_message_to_text(item)[:200] for item in params.messages]
        tool_names = [str(item.name) for item in params.tools or []]
        return {
            'server': self.config.name,
            'risk_level': risk_level,
            'risk_reasons': risk_reasons,
            'include_context': params.includeContext,
            'tool_count': len(tool_names),
            'tool_names': tool_names,
            'text_preview': '\n'.join(part for part in preview_parts if part)[:500],
            'sampling': params.model_dump(mode='json', exclude_none=True),
        }

    def _elicitation_approval_payload(
        self,
        params: mcp_types.ElicitRequestParams,
        risk_level: str,
        risk_reasons: list[str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'server': self.config.name,
            'risk_level': risk_level,
            'risk_reasons': risk_reasons,
            'mode': params.mode,
            'message': params.message,
            'elicitation': params.model_dump(mode='json', exclude_none=True),
        }
        if params.mode == 'form':
            payload['requested_schema'] = normalize_requested_schema(params.requestedSchema)
        else:
            payload['url'] = params.url
            payload['elicitation_id'] = params.elicitationId
            payload['url_host'] = url_host(params.url)
        return payload

    async def _sampling_callback(
        self,
        context: Any,
        params: mcp_types.CreateMessageRequestParams,
    ) -> mcp_types.CreateMessageResult | mcp_types.ErrorData:
        del context
        risk_level, risk_reasons = classify_sampling_request(params)
        if self._human_loop is not None and self._human_loop.config.approve_mcp_sampling:
            approval_context = self._approval_context_for_risk(risk_level)
            await self._human_loop.require_approval(
                approval_context,
                request_key=(
                    f'mcp_sampling:{self.config.name}:'
                    f'{self._human_loop.stable_key(params.model_dump(mode="json", exclude_none=True), risk_level)}'
                ),
                kind='mcp_sampling',
                title=f'Approve MCP sampling request for {self.config.name}',
                payload=self._sampling_approval_payload(params, risk_level, risk_reasons),
            )
        messages: list[ChatMessage] = []
        if params.systemPrompt:
            messages.append(ChatMessage(role='system', content=params.systemPrompt))
        for item in params.messages:
            text = sampling_message_to_text(item)
            if not text:
                return mcp_types.ErrorData(code=mcp_types.INVALID_REQUEST, message='Only text-first sampling is supported')
            messages.append(ChatMessage(role='assistant' if item.role == 'assistant' else 'user', content=text))
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
        risk_level, risk_reasons = classify_elicitation_request(params)
        approval_context = self._approval_context_for_risk(risk_level)
        response_payload = await self._human_loop.require_approval(
            approval_context,
            request_key=(
                f'mcp_elicitation:{self.config.name}:'
                f'{self._human_loop.stable_key(params.model_dump(mode="json", exclude_none=True), risk_level)}'
            ),
            kind='mcp_elicitation',
            title=f'Approve MCP elicitation request for {self.config.name}',
            payload=self._elicitation_approval_payload(params, risk_level, risk_reasons),
        )
        return coerce_elicitation_result(params, response_payload)

    async def _roots_callback(
        self,
        context: Any,
    ) -> mcp_types.ListRootsResult | mcp_types.ErrorData:
        del context
        return mcp_types.ListRootsResult(
            roots=[mcp_types.Root(uri=cast(Any, root_to_uri(item.path)), name=item.name) for item in self._resolved_roots()]
        )


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
            list_roots_callback=self._roots_callback if self._supports_server_roots() else None,
            message_handler=self._message_handler,
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

    async def refresh_roots(self) -> dict[str, Any]:
        roots = normalize_root_entries([root_payload(item) for item in self._resolved_roots()])
        previous = self._load_root_snapshot()
        if previous is None:
            self._save_root_snapshot(roots)
            return {
                'server': self.config.name,
                'changed': False,
                'notification_sent': False,
                'reason': 'snapshot_initialized',
                'roots': roots,
                'diff': {'added': [], 'removed': [], 'changed': []},
            }
        diff = diff_root_entries(previous['roots'], roots)
        changed = bool(diff['added'] or diff['removed'] or diff['changed'])
        notification_sent = False
        reason = 'unchanged'
        last_notified_at: str | None = None
        if changed:
            if not self._supports_server_roots():
                reason = 'server_roots_unsupported'
            elif self._session is None:
                reason = 'session_unavailable'
            else:
                await self._session.send_roots_list_changed()
                notification_sent = True
                reason = 'roots_list_changed_notified'
                last_notified_at = _utcnow()
        self._save_root_snapshot(roots, last_notified_at=last_notified_at)
        return {
            'server': self.config.name,
            'changed': changed,
            'notification_sent': notification_sent,
            'reason': reason,
            'roots': roots,
            'diff': diff,
        }

    async def authorize(self) -> None:
        await self.list_tools()

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._transport_cm is not None:
            await self._transport_cm.__aexit__(None, None, None)
            self._transport_cm = None

    async def _message_handler(self, message: Any) -> None:
        if isinstance(message, Exception):
            return
        notification = message if isinstance(message, mcp_types.ServerNotification) else None
        if notification is None:
            return
        match notification.root:
            case mcp_types.ElicitCompleteNotification(params=params):
                await self._handle_elicitation_complete(params)
            case _:
                return

    async def _handle_elicitation_complete(self, params: mcp_types.ElicitCompleteNotificationParams) -> None:
        if self._store is None:
            return
        request = self._store.find_mcp_elicitation_request(self.config.name, params.elicitationId)
        if request is None or request.status is not HumanRequestStatus.APPROVED:
            return
        response_payload = dict(request.response_payload or {})
        if str(response_payload.get('action') or '').lower() != 'accept':
            return
        completion = response_payload.get('completion')
        if isinstance(completion, dict) and str(completion.get('status') or '').lower() == 'completed':
            return
        completion_payload = {
            'status': 'completed',
            'elicitation_id': params.elicitationId,
            'completed_at': _utcnow(),
        }
        response_payload['completion'] = completion_payload
        self._store.update_human_request_response(request.request_id, response_payload)
        self._store.record_event(
            request.run_id,
            'mcp_elicitation_completed',
            {
                'server': self.config.name,
                'request_id': request.request_id,
                'elicitation_id': params.elicitationId,
                'completion': completion_payload,
            },
            scope='mcp',
            span_id=f'mcp:elicitation:{self.config.name}:{params.elicitationId}',
        )

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
        return await anyio.open_process([command, *args], env=env, stderr=sys.stderr, cwd=cwd, start_new_session=True)

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
