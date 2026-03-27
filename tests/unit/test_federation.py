from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_config.app import AppConfig, ModelConfig
from agent_integrations.federation import FederationClientManager, FederationServer
from agent_integrations.storage import SQLiteRunStore


class FakeRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.config = AppConfig.model_validate(
            {
                'model': ModelConfig().model_dump(),
                'graph': {
                    'entrypoint': 'coordinator',
                    'agents': [{'name': 'coordinator'}],
                    'teams': [],
                    'nodes': [],
                },
                'federation': {
                    'server': {'enabled': True, 'host': '127.0.0.1', 'port': 0, 'base_path': '/a2a'},
                    'exports': [
                        {
                            'name': 'local_echo',
                            'target_type': 'agent',
                            'target': 'coordinator',
                            'description': 'Echo target',
                        }
                    ],
                },
                'storage': {'path': str(tmp_path), 'database': 'state.db'},
            }
        )
        self.store = SQLiteRunStore(tmp_path, 'state.db')

    async def run_federated_export(self, export_name: str, input_text: str, *, session_id: str | None = None) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {
            'run_id': 'local-run',
            'status': 'succeeded',
            'export': export_name,
            'session_id': session_id,
            'result': {'echo': input_text.upper()},
        }

    def interrupt_run(self, run_id: str, payload: dict[str, Any] | None = None) -> None:
        del run_id, payload
        return None


@pytest.mark.asyncio
async def test_federation_loopback_server_and_client(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f"http://127.0.0.1:{status['port']}"
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
            }
        ).federation
    )
    await manager.start()
    try:
        remote = await manager.inspect_remote('loopback')
        result = await manager.run_remote('loopback', 'local_echo', 'hello', session_id='demo-session')
        events = await manager.stream_remote('loopback', 'local_echo', 'streamed')
        tasks = await manager.list_tasks('loopback')
    finally:
        await manager.aclose()
        server.stop()

    assert remote['card']['exports'][0]['name'] == 'local_echo'
    assert result['result']['echo'] == 'HELLO'
    assert events[-1]['task']['status'] == 'succeeded'
    assert len(tasks) >= 2


@pytest.mark.asyncio
async def test_federation_cancel_marks_task(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f"http://127.0.0.1:{status['port']}"
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
            }
        ).federation
    )
    await manager.start()
    try:
        client = manager._client('loopback')
        response = await client.post('/a2a/tasks/send', json={'target': 'local_echo', 'input': 'cancel-me'})
        task_id = response.json()['task']['task_id']
        cancelled = await manager.cancel_task('loopback', task_id)
    finally:
        await manager.aclose()
        server.stop()

    assert cancelled['status'] == 'cancelled'
