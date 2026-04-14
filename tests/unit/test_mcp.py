from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

import mcp.types as mcp_types
import pytest

from agent_common.models import HumanLoopMode, HumanRequestStatus, RunContext
from agent_config.app import McpRootConfig, McpServerConfig
from agent_integrations.mcp import BaseMcpClient, McpClientManager, build_mcp_tool_name
from agent_integrations.mcp.clients import SessionBackedMcpClient
from agent_integrations.sandbox import SandboxManager, SandboxMode, SandboxTarget
from agent_integrations.storage import SQLiteRunStore

STDIO_SERVER = r"""
import asyncio
import json
import sys

async def main():
    while True:
        line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        request = json.loads(line)
        method = request.get('method')
        if method == 'initialize':
            payload = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'mock', 'version': '0.1.0'}}}
        elif method == 'notifications/initialized':
            continue
        elif method == 'tools/list':
            payload = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'tools': [{'name': 'echo', 'description': 'Echo', 'inputSchema': {'type': 'object'}}]}}
        elif method == 'tools/call':
            payload = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(request['params']['arguments'])}]}}
        else:
            payload = {'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'method not found'}}
        sys.stdout.write(json.dumps(payload) + '\n')
        sys.stdout.flush()

asyncio.run(main())
"""


class McpHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != '/sse':
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(b'data: {"status":"ready"}\n\n')

    def do_POST(self) -> None:  # noqa: N802
        if self.path != '/rpc':
            self.send_response(404)
            self.end_headers()
            return
        content_length = int(self.headers['Content-Length'])
        payload = json.loads(self.rfile.read(content_length))
        if payload['method'] == 'tools/list':
            result = {'tools': [{'name': 'remote', 'description': 'Remote tool', 'inputSchema': {'type': 'object'}}]}
        else:
            result = {'content': payload['params']['arguments']}
        body = json.dumps({'jsonrpc': '2.0', 'id': payload['id'], 'result': result}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        del format, args
        return


class _RecordingHumanLoop:
    def __init__(self, *, response_payload: dict[str, Any] | None = None) -> None:
        self.config = type(
            'Cfg',
            (),
            {'approve_mcp_sampling': True, 'approve_mcp_elicitation': True},
        )()
        self.response_payload = response_payload or {}
        self.calls: list[tuple[RunContext, dict[str, Any]]] = []

    async def require_approval(self, context: RunContext, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((context, kwargs))
        return dict(self.response_payload)

    def stable_key(self, *parts: Any) -> str:
        return 'stable-key'


class _ModelClient:
    async def complete(self, messages: list[Any], tools: list[Any]) -> Any:
        del messages, tools
        return type('Response', (), {'text': 'approved', 'tool_calls': [], 'model_name': 'stub'})()


class _DummyMcpClient(BaseMcpClient):
    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {'name': name, 'arguments': arguments}

    async def list_resources(self) -> dict[str, Any]:
        return {'resources': []}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return {'contents': [{'uri': uri, 'text': 'stub'}]}

    async def list_resource_templates(self) -> dict[str, Any]:
        return {'resourceTemplates': []}

    async def subscribe_resource(self, uri: str) -> dict[str, Any]:
        return {'uri': uri, 'status': 'active'}

    async def unsubscribe_resource(self, uri: str) -> dict[str, Any]:
        return {'uri': uri, 'status': 'inactive'}

    async def list_prompts(self) -> dict[str, Any]:
        return {'prompts': []}

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        return {'name': name, 'arguments': arguments or {}}

    async def aclose(self) -> None:
        return None


class _RecordingSession:
    def __init__(self) -> None:
        self.notifications = 0
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def send_roots_list_changed(self) -> None:
        self.notifications += 1

    async def list_tools(self) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name='echo', description='Echo', inputSchema={'type': 'object'})]
        )

    async def list_resources(self) -> mcp_types.ListResourcesResult:
        return mcp_types.ListResourcesResult(
            resources=[
                mcp_types.Resource(
                    name='notes',
                    uri=cast(Any, 'file:///notes.txt'),
                    description='Notes',
                    mimeType='text/plain',
                )
            ]
        )

    async def read_resource(self, uri: str) -> mcp_types.ReadResourceResult:
        return mcp_types.ReadResourceResult(
            contents=[mcp_types.TextResourceContents(uri=cast(Any, uri), mimeType='text/plain', text='hello world')]
        )

    async def list_resource_templates(self) -> mcp_types.ListResourceTemplatesResult:
        return mcp_types.ListResourceTemplatesResult(
            resourceTemplates=[
                mcp_types.ResourceTemplate(
                    name='note',
                    uriTemplate='file:///notes/{name}.txt',
                    description='Note template',
                    mimeType='text/plain',
                )
            ]
        )

    async def subscribe_resource(self, uri: str) -> mcp_types.EmptyResult:
        self.subscribed.append(uri)
        return mcp_types.EmptyResult()

    async def unsubscribe_resource(self, uri: str) -> mcp_types.EmptyResult:
        self.unsubscribed.append(uri)
        return mcp_types.EmptyResult()

    async def list_prompts(self) -> mcp_types.ListPromptsResult:
        return mcp_types.ListPromptsResult(
            prompts=[mcp_types.Prompt(name='summarize', description='Summarize content')]
        )

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(
            description='Prompt body',
            messages=[
                mcp_types.PromptMessage(
                    role='user',
                    content=mcp_types.TextContent(type='text', text=f'{name}:{(arguments or {}).get("topic", "")}'),
                )
            ],
        )


class _DummySessionMcpClient(SessionBackedMcpClient):
    async def _open_transport(self) -> tuple[Any, Any]:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_mcp_manager_supports_stdio_and_http_sse() -> None:
    server = HTTPServer(('127.0.0.1', 0), McpHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rpc_port = server.server_address[1]
    sandbox_manager = SandboxManager(
        mode=SandboxMode.PROCESS,
        targets=[SandboxTarget.STDIO_MCP],
        env_allowlist=['PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP'],
    )
    manager = McpClientManager(
        [
            McpServerConfig(name='stdio', transport='stdio', command=[sys.executable, '-c', STDIO_SERVER]),
            McpServerConfig(
                name='remote',
                transport='http_sse',
                rpc_url=f'http://127.0.0.1:{rpc_port}/rpc',
                sse_url=f'http://127.0.0.1:{rpc_port}/sse',
            ),
        ],
        sandbox_manager,
    )

    await manager.start()
    try:
        servers = await manager.list_servers()
        echo_result = await manager.call_tool('stdio', 'echo', {'prompt': 'hello'})
        remote_result = await manager.call_tool('remote', 'remote', {'prompt': 'hi'})
    finally:
        await manager.aclose()
        server.shutdown()
        thread.join()

    assert servers['stdio'][0].name == 'echo'
    assert servers['remote'][0].name == 'remote'
    assert 'hello' in json.dumps(echo_result)
    assert remote_result['prompt'] == 'hi'



def test_build_mcp_tool_name_sanitizes_separator() -> None:
    assert build_mcp_tool_name('filesystem', 'read_text_file') == 'mcp__filesystem__read_text_file'
    assert build_mcp_tool_name('pg-server', 'query/sql') == 'mcp__pg-server__query_sql'


@pytest.mark.asyncio
async def test_mcp_manager_infers_filesystem_roots_from_stdio_command(tmp_path: Path) -> None:
    sandbox_manager = SandboxManager(
        mode=SandboxMode.PROCESS,
        targets=[SandboxTarget.STDIO_MCP],
        env_allowlist=['PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP'],
    )
    manager = McpClientManager(
        [
            McpServerConfig(
                name='filesystem',
                transport='stdio',
                command=['cmd', '/c', 'npx', '-y', '@modelcontextprotocol/server-filesystem', str(tmp_path)],
            )
        ],
        sandbox_manager,
    )

    roots = await manager.list_roots('filesystem')

    assert roots[0]['path'] == str(tmp_path)
    assert roots[0]['uri'].startswith('file:///')


@pytest.mark.asyncio
async def test_list_roots_initializes_snapshot_once(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    config = McpServerConfig(
        name='roots',
        transport='streamable_http',
        url='http://example.com',
        roots=[McpRootConfig(path=str(tmp_path / 'alpha'), name='alpha')],
    )
    client = _DummyMcpClient(
        config,
        store=store,
        model_client=_ModelClient(),
        human_loop=None,
        redirect_handler=None,
        callback_handler=None,
    )

    first = await client.list_roots()
    config.roots = [McpRootConfig(path=str(tmp_path / 'beta'), name='beta')]
    second = await client.list_roots()
    snapshot = store.load_mcp_root_snapshot('roots')

    assert first[0]['path'].endswith('alpha')
    assert second[0]['path'].endswith('beta')
    assert snapshot is not None
    assert snapshot['roots'] == first


@pytest.mark.asyncio
async def test_refresh_roots_reports_diff_and_sends_notification(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    config = McpServerConfig(
        name='remote',
        transport='streamable_http',
        url='http://example.com',
        roots=[McpRootConfig(path=str(tmp_path / 'alpha'), name='alpha')],
    )
    client = _DummySessionMcpClient(
        config,
        store=store,
        model_client=_ModelClient(),
        human_loop=None,
        redirect_handler=None,
        callback_handler=None,
    )
    session = _RecordingSession()
    client._session = cast(Any, session)

    await client.list_roots()
    config.roots = [
        McpRootConfig(path=str(tmp_path / 'alpha'), name='alpha'),
        McpRootConfig(path=str(tmp_path / 'beta'), name='beta'),
    ]

    result = await client.refresh_roots()
    snapshot = store.load_mcp_root_snapshot('remote')

    assert result['changed'] is True
    assert result['notification_sent'] is True
    assert result['reason'] == 'roots_list_changed_notified'
    assert result['diff']['added'][0]['path'].endswith('beta')
    assert session.notifications == 1
    assert snapshot is not None
    assert len(snapshot['roots']) == 2


@pytest.mark.asyncio
async def test_stdio_filesystem_refresh_tracks_diff_without_notification(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    config = McpServerConfig(
        name='filesystem',
        transport='stdio',
        command=['cmd', '/c', 'npx', '-y', '@modelcontextprotocol/server-filesystem', str(tmp_path / 'alpha')],
    )
    client = _DummySessionMcpClient(
        config,
        store=store,
        model_client=_ModelClient(),
        human_loop=None,
        redirect_handler=None,
        callback_handler=None,
    )
    client._session = cast(Any, _RecordingSession())

    await client.list_roots()
    config.command = ['cmd', '/c', 'npx', '-y', '@modelcontextprotocol/server-filesystem', str(tmp_path / 'beta')]

    result = await client.refresh_roots()

    assert result['changed'] is True
    assert result['notification_sent'] is False
    assert result['reason'] == 'server_roots_unsupported'
    assert result['diff']['added'][0]['path'].endswith('beta')
    assert result['diff']['removed'][0]['path'].endswith('alpha')



def test_stdio_filesystem_client_disables_server_roots_capability(tmp_path: Path) -> None:
    sandbox_manager = SandboxManager(
        mode=SandboxMode.PROCESS,
        targets=[SandboxTarget.STDIO_MCP],
        env_allowlist=['PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP'],
    )
    manager = McpClientManager(
        [
            McpServerConfig(
                name='filesystem',
                transport='stdio',
                command=['cmd', '/c', 'npx', '-y', '@modelcontextprotocol/server-filesystem', str(tmp_path)],
            )
        ],
        sandbox_manager,
    )

    client = manager._clients['filesystem']

    assert client._supports_server_roots() is False


@pytest.mark.asyncio
async def test_session_backed_client_tracks_resources_prompts_and_subscriptions(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    config = McpServerConfig(
        name='remote',
        transport='streamable_http',
        url='http://example.com',
    )
    client = _DummySessionMcpClient(
        config,
        store=store,
        model_client=_ModelClient(),
        human_loop=None,
        redirect_handler=None,
        callback_handler=None,
    )
    client._session = cast(Any, _RecordingSession())

    resources = await client.list_resources()
    contents = await client.read_resource('file:///notes.txt')
    templates = await client.list_resource_templates()
    prompts = await client.list_prompts()
    prompt = await client.get_prompt('summarize', {'topic': 'notes'})
    subscription = await client.subscribe_resource('file:///notes.txt')
    unsubscription = await client.unsubscribe_resource('file:///notes.txt')

    resource_snapshot = store.load_mcp_catalog_snapshot('remote', 'resources')
    template_snapshot = store.load_mcp_catalog_snapshot('remote', 'resource_templates')
    prompt_snapshot = store.load_mcp_catalog_snapshot('remote', 'prompts')
    prompt_detail_snapshot = store.load_mcp_catalog_snapshot('remote', 'prompt_details')
    saved_subscription = store.load_mcp_resource_subscription('remote', 'file:///notes.txt')

    assert resources['resources'][0]['uri'] == 'file:///notes.txt'
    assert contents['contents'][0]['text'] == 'hello world'
    assert templates['resourceTemplates'][0]['uriTemplate'] == 'file:///notes/{name}.txt'
    assert prompts['prompts'][0]['name'] == 'summarize'
    assert prompt['messages'][0]['content']['text'] == 'summarize:notes'
    assert subscription['status'] == 'active'
    assert unsubscription['status'] == 'inactive'
    assert resource_snapshot is not None
    assert resource_snapshot['entries'][0]['uri'] == 'file:///notes.txt'
    assert resource_snapshot['metadata']['last_refresh_reason'] == 'list_resources'
    assert resource_snapshot['metadata']['dirty'] is False
    assert template_snapshot is not None
    assert template_snapshot['entries'][0]['uriTemplate'] == 'file:///notes/{name}.txt'
    assert prompt_snapshot is not None
    assert prompt_snapshot['entries'][0]['name'] == 'summarize'
    assert prompt_detail_snapshot is not None
    assert prompt_detail_snapshot['entries'][0]['name'] == 'summarize'
    assert prompt_detail_snapshot['entries'][0]['stale'] is False
    assert saved_subscription is not None
    assert saved_subscription['status'] == 'inactive'


@pytest.mark.asyncio
async def test_message_handler_persists_catalog_change_and_resource_update(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    config = McpServerConfig(
        name='remote',
        transport='streamable_http',
        url='http://example.com',
    )
    client = _DummySessionMcpClient(
        config,
        store=store,
        model_client=_ModelClient(),
        human_loop=None,
        redirect_handler=None,
        callback_handler=None,
    )
    client._session = cast(Any, _RecordingSession())
    await client.subscribe_resource('file:///notes.txt')
    await client.get_prompt('summarize', {'topic': 'notes'})

    await client._message_handler(
        mcp_types.ServerNotification(root=mcp_types.ResourceListChangedNotification())
    )
    await client._message_handler(
        mcp_types.ServerNotification(root=mcp_types.ToolListChangedNotification())
    )
    await client._message_handler(
        mcp_types.ServerNotification(root=mcp_types.PromptListChangedNotification())
    )
    await client._message_handler(
        mcp_types.ServerNotification(
            root=mcp_types.ResourceUpdatedNotification(
                params=mcp_types.ResourceUpdatedNotificationParams(uri=cast(Any, 'file:///notes.txt'))
            )
        )
    )

    resource_snapshot = store.load_mcp_catalog_snapshot('remote', 'resources')
    template_snapshot = store.load_mcp_catalog_snapshot('remote', 'resource_templates')
    tool_snapshot = store.load_mcp_catalog_snapshot('remote', 'tools')
    prompt_snapshot = store.load_mcp_catalog_snapshot('remote', 'prompts')
    prompt_detail_snapshot = store.load_mcp_catalog_snapshot('remote', 'prompt_details')
    saved_subscription = store.load_mcp_resource_subscription('remote', 'file:///notes.txt')

    assert resource_snapshot is not None
    assert resource_snapshot['last_notified_at'] is not None
    assert resource_snapshot['metadata']['last_refresh_reason'] == 'notifications/resources/list_changed'
    assert template_snapshot is not None
    assert template_snapshot['last_notified_at'] is not None
    assert tool_snapshot is not None
    assert tool_snapshot['entries'][0]['name'] == 'echo'
    assert prompt_snapshot is not None
    assert prompt_snapshot['entries'][0]['name'] == 'summarize'
    assert prompt_detail_snapshot is not None
    assert prompt_detail_snapshot['entries'][0]['stale'] is True
    assert prompt_detail_snapshot['metadata']['dirty'] is True
    assert saved_subscription is not None
    assert saved_subscription['subscription']['last_update']['uri'] == 'file:///notes.txt'


@pytest.mark.asyncio
async def test_sampling_callback_uses_bound_run_context_and_forces_deferred_for_high_risk() -> None:
    human_loop = _RecordingHumanLoop()
    client = _DummyMcpClient(
        McpServerConfig(name='remote', transport='streamable_http', url='http://example.com'),
        store=None,
        model_client=_ModelClient(),
        human_loop=cast(Any, human_loop),
        redirect_handler=None,
        callback_handler=None,
    )
    run_context = RunContext(run_id='run-123', workdir=Path.cwd(), node_id='node-1', approval_mode=HumanLoopMode.INLINE)
    token = client.bind_run_context(run_context)
    try:
        result = await client._sampling_callback(
            None,
            mcp_types.CreateMessageRequestParams(
                messages=[mcp_types.SamplingMessage(role='user', content=mcp_types.TextContent(type='text', text='hello'))],
                maxTokens=32,
                tools=[mcp_types.Tool(name='remote_lookup', inputSchema={'type': 'object'})],
            ),
        )
    finally:
        client.reset_run_context(token)

    assert isinstance(result, mcp_types.CreateMessageResult)
    approval_context, payload = human_loop.calls[0]
    assert approval_context.run_id == 'run-123'
    assert approval_context.approval_mode is HumanLoopMode.DEFERRED
    assert payload['payload']['risk_level'] == 'high'
    assert payload['payload']['tool_names'] == ['remote_lookup']


@pytest.mark.asyncio
async def test_sampling_callback_keeps_inline_for_low_risk_request() -> None:
    human_loop = _RecordingHumanLoop()
    client = _DummyMcpClient(
        McpServerConfig(name='remote', transport='streamable_http', url='http://example.com'),
        store=None,
        model_client=_ModelClient(),
        human_loop=cast(Any, human_loop),
        redirect_handler=None,
        callback_handler=None,
    )
    run_context = RunContext(run_id='run-456', workdir=Path.cwd(), node_id='node-2', approval_mode=HumanLoopMode.INLINE)
    token = client.bind_run_context(run_context)
    try:
        await client._sampling_callback(
            None,
            mcp_types.CreateMessageRequestParams(
                messages=[mcp_types.SamplingMessage(role='user', content=mcp_types.TextContent(type='text', text='hello'))],
                maxTokens=16,
            ),
        )
    finally:
        client.reset_run_context(token)

    approval_context, payload = human_loop.calls[0]
    assert approval_context.run_id == 'run-456'
    assert approval_context.approval_mode is HumanLoopMode.INLINE
    assert payload['payload']['risk_level'] == 'low'


@pytest.mark.asyncio
async def test_elicitation_callback_validates_form_content_and_drops_unknown_keys() -> None:
    human_loop = _RecordingHumanLoop(response_payload={'action': 'accept', 'content': {'name': 'Alice', 'count': '2', 'ignored': 'x'}})
    client = _DummyMcpClient(
        McpServerConfig(name='remote', transport='streamable_http', url='http://example.com'),
        store=None,
        model_client=_ModelClient(),
        human_loop=cast(Any, human_loop),
        redirect_handler=None,
        callback_handler=None,
    )
    run_context = RunContext(run_id='run-form', workdir=Path.cwd(), node_id='node-3', approval_mode=HumanLoopMode.INLINE)
    token = client.bind_run_context(run_context)
    try:
        result = await client._elicitation_callback(
            None,
            mcp_types.ElicitRequestFormParams(
                message='Provide profile data',
                requestedSchema={
                    'type': 'object',
                    'properties': {'name': {'type': 'string'}, 'count': {'type': 'integer'}},
                    'required': ['name'],
                },
            ),
        )
    finally:
        client.reset_run_context(token)

    assert isinstance(result, mcp_types.ElicitResult)
    assert result.action == 'accept'
    assert result.content == {'name': 'Alice', 'count': 2}
    approval_context, payload = human_loop.calls[0]
    assert approval_context.run_id == 'run-form'
    assert payload['payload']['mode'] == 'form'


@pytest.mark.asyncio
async def test_elicitation_callback_for_url_mode_forces_deferred_and_omits_content() -> None:
    human_loop = _RecordingHumanLoop(response_payload={'action': 'accept', 'content': {'ignored': 'x'}})
    client = _DummyMcpClient(
        McpServerConfig(name='remote', transport='streamable_http', url='http://example.com'),
        store=None,
        model_client=_ModelClient(),
        human_loop=cast(Any, human_loop),
        redirect_handler=None,
        callback_handler=None,
    )
    run_context = RunContext(run_id='run-url', workdir=Path.cwd(), node_id='node-4', approval_mode=HumanLoopMode.INLINE)
    token = client.bind_run_context(run_context)
    try:
        result = await client._elicitation_callback(
            None,
            mcp_types.ElicitRequestURLParams(
                message='Complete remote login',
                url='https://example.com/oauth/start',
                elicitationId='eli-1',
            ),
        )
    finally:
        client.reset_run_context(token)

    assert isinstance(result, mcp_types.ElicitResult)
    assert result.action == 'accept'
    assert result.content is None
    approval_context, payload = human_loop.calls[0]
    assert approval_context.run_id == 'run-url'
    assert approval_context.approval_mode is HumanLoopMode.DEFERRED
    assert payload['payload']['mode'] == 'url'
    assert payload['payload']['url_host'] == 'example.com'


@pytest.mark.asyncio
async def test_elicitation_complete_updates_existing_approval_state(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_run('run-url', 'baseline', {'input': 'hello'})
    request = store.create_human_request(
        'run-url',
        'mcp_elicitation:remote:stable-key',
        'mcp_elicitation',
        'Approve login',
        {
            'server': 'remote',
            'mode': 'url',
            'elicitation_id': 'eli-1',
            'url': 'https://example.com/oauth/start',
        },
    )
    store.resolve_human_request(
        request.request_id,
        status=HumanRequestStatus.APPROVED,
        response_payload={'action': 'accept', 'completion': {'status': 'pending', 'elicitation_id': 'eli-1'}},
    )
    client = _DummySessionMcpClient(
        McpServerConfig(name='remote', transport='streamable_http', url='http://example.com'),
        store=store,
        model_client=_ModelClient(),
        human_loop=None,
        redirect_handler=None,
        callback_handler=None,
    )

    await client._handle_elicitation_complete(mcp_types.ElicitCompleteNotificationParams(elicitationId='eli-1'))

    updated = store.load_human_request(request.request_id)
    trace = store.load_trace('run-url')

    assert updated.response_payload is not None
    assert updated.response_payload['completion']['status'] == 'completed'
    assert updated.response_payload['completion']['elicitation_id'] == 'eli-1'
    assert trace['events'][-1]['kind'] == 'mcp_elicitation_completed'

