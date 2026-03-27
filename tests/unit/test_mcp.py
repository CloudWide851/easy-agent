from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_config.app import McpServerConfig
from agent_integrations.mcp import McpClientManager, build_mcp_tool_name
from agent_integrations.sandbox import SandboxManager, SandboxMode, SandboxTarget

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

