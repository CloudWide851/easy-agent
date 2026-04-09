from __future__ import annotations

import asyncio
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_config.app import AppConfig, ModelConfig
from agent_integrations.federation import FederationClientManager, FederationServer
from agent_integrations.federation_security import (
    build_server_jwks,
    sign_server_token,
    verify_callback_headers,
    verify_callback_jws,
)
from agent_integrations.storage import SQLiteRunStore


def _write_rsa_keypair(tmp_path: Path) -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_path = tmp_path / 'federation-private.pem'
    public_path = tmp_path / 'federation-public.pem'
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return str(private_path), str(public_path)


class FakeRuntime:
    def __init__(
        self,
        tmp_path: Path,
        *,
        server_overrides: dict[str, Any] | None = None,
        exports: list[dict[str, Any]] | None = None,
    ) -> None:
        server_config = {
            'enabled': True,
            'host': '127.0.0.1',
            'port': 0,
            'base_path': '/a2a',
            'retry_max_attempts': 3,
            'retry_initial_backoff_seconds': 0.1,
            'retry_backoff_multiplier': 1.0,
            'subscription_lease_seconds': 30,
        }
        if server_overrides:
            server_config.update(server_overrides)
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
                    'server': server_config,
                    'exports': exports
                    or [
                        {
                            'name': 'local_echo',
                            'target_type': 'agent',
                            'target': 'coordinator',
                            'description': 'Echo target',
                            'modalities': ['text'],
                            'capabilities': ['streaming', 'interrupts'],
                            'artifacts': [{'name': 'result_text', 'modality': 'text/plain'}],
                            'parts': [{'name': 'text', 'modality': 'text/plain'}],
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


class CallbackCollector:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                del format, args
                return None

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get('Content-Length', '0') or '0')
                raw = self.rfile.read(length) if length else b''
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                collector.attempts += 1
                collector.requests.append(
                    {
                        'path': self.path,
                        'payload': payload,
                        'raw': raw,
                        'headers': {key: value for key, value in self.headers.items()},
                    }
                )
                status = HTTPStatus.INTERNAL_SERVER_ERROR if collector.fail_first and collector.attempts == 1 else HTTPStatus.OK
                self.send_response(status)
                self.end_headers()

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f'http://127.0.0.1:{port}/callback'

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


@pytest.mark.asyncio
async def test_federation_loopback_server_and_client(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url, 'push_preference': 'sse'}]},
            }
        ).federation
    )
    await manager.start()
    try:
        result = await manager.run_remote('loopback', 'local_echo', 'hello', session_id='demo-session')
        remote = await manager.inspect_remote('loopback')
        stream_events = await manager.stream_remote('loopback', 'local_echo', 'streamed')
        tasks_page_one = await manager.list_tasks('loopback', page_size=1)
        next_page = await manager.list_tasks('loopback', page_token=tasks_page_one['nextPageToken'], page_size=1)
        task_id = str(tasks_page_one['tasks'][0]['task_id'])
        task_events_page_one = await manager.list_task_events('loopback', task_id, page_size=1)
        task_events_page_two = await manager.list_task_events(
            'loopback',
            task_id,
            page_token=task_events_page_one['nextPageToken'],
            page_size=1,
        )
        streamed_task_events = await manager.stream_task_events('loopback', task_id)
        resubscribe = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
        invalid_page = await manager._client('loopback').get('/a2a/tasks', params={'pageToken': 'not-a-valid-token'})
    finally:
        await manager.aclose()
        server.stop()

    assert remote['card']['exports'][0]['name'] == 'local_echo'
    assert remote['card']['well_known_url'].endswith('/.well-known/agent-card.json')
    assert remote['card']['protocol_version'] == '0.3'
    assert remote['card']['exports'][0]['capabilities']['modalities'] == ['text']
    assert remote['card']['exports'][0]['artifacts'][0]['name'] == 'result_text'
    assert remote['card']['exports'][0]['parts'][0]['name'] == 'text'
    assert remote['card']['defaultInputModes'] == ['text']
    assert remote['card']['notificationCompatibility']['pushNotificationConfig'] is True
    assert remote['extended_card']['capabilities']['push_delivery']['sse_events'] is True
    assert remote['extended_card']['capabilities']['pagination']['pageToken'] is True
    assert remote['extended_card']['retry_policy']['max_attempts'] == 3
    assert result['result']['echo'] == 'HELLO'
    assert stream_events[-1]['task']['status'] == 'succeeded'
    assert len(tasks_page_one['tasks']) == 1
    assert tasks_page_one['nextPageToken']
    assert len(next_page['tasks']) == 1
    assert task_events_page_one['events']
    assert task_events_page_one['nextPageToken']
    assert len(task_events_page_two['events']) == 1
    assert any(event['event_name'] == 'task_succeeded' for event in streamed_task_events)
    assert resubscribe['events'][-1]['event_kind'] == 'task_succeeded'
    assert invalid_page.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.asyncio
async def test_federation_cancel_marks_task(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
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


@pytest.mark.asyncio
async def test_federation_subscription_retry_lifecycle_and_signed_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('EASY_AGENT_PUSH_SECRET', 'unit-secret')
    monkeypatch.setenv('EASY_AGENT_PUSH_TOKEN', 'unit-token')
    private_key_path, public_key_path = _write_rsa_keypair(tmp_path)
    runtime = FakeRuntime(
        tmp_path,
        server_overrides={
            'jwt': {
                'enabled': True,
                'issuer': 'https://federation.example',
                'audience': 'easy-agent-tests',
                'private_key_path': private_key_path,
                'public_key_path': public_key_path,
            },
            'push_security': {
                'token_env': 'EASY_AGENT_PUSH_TOKEN',
                'signature_secret_env': 'EASY_AGENT_PUSH_SECRET',
                'require_signature': True,
                'audience': 'easy-agent-tests',
                'require_audience': True,
                'jws_enabled': True,
                'jwks_url': 'https://federation.example/.well-known/jwks.json',
            }
        },
    )
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    callback = CallbackCollector(fail_first=True)
    callback_url = callback.start()
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
        response = await client.post('/a2a/tasks/send', json={'target': 'local_echo', 'input': 'deliver-me'})
        task_id = str(response.json()['task']['task_id'])
        await asyncio.sleep(0.2)
        subscription = await manager.set_push_notification('loopback', task_id, callback_url, from_sequence=0)
        assert subscription['status'] in {'retrying', 'active', 'delivered'}
        deadline = asyncio.get_running_loop().time() + 5.0
        refreshed: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            subscriptions = await manager.list_push_notifications('loopback', task_id)
            if subscriptions and subscriptions[0]['status'] == 'delivered':
                refreshed = subscriptions[0]
                break
            await asyncio.sleep(0.2)
        assert refreshed is not None
        loaded = await manager.get_push_notification('loopback', task_id, str(refreshed['subscription_id']))
        replay = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
        renewed = await manager.renew_subscription('loopback', task_id, str(refreshed['subscription_id']), lease_seconds=60)
        cancelled = await manager.delete_push_notification('loopback', task_id, str(refreshed['subscription_id']))
    finally:
        await manager.aclose()
        callback.stop()
        server.stop()

    last_request = callback.requests[-1]
    verify_callback_headers(
        last_request['headers'],
        last_request['raw'],
        last_request['path'],
        runtime.config.federation.server.push_security,
        expected_secret='unit-secret',
        expected_audience='easy-agent-tests',
    )
    normalized_headers = {str(key).lower(): value for key, value in last_request['headers'].items()}
    verify_callback_jws(
        normalized_headers[runtime.config.federation.server.push_security.jws_header.lower()],
        payload_bytes=last_request['raw'],
        callback_path=last_request['path'],
        jwks=build_server_jwks(runtime.config.federation.server.jwt),
        audience='easy-agent-tests',
        issuer='https://federation.example',
        allowed_algorithms=['RS256'],
        leeway_seconds=30,
    )
    assert callback.attempts >= 2
    assert last_request['payload']['task_id'] == task_id
    assert last_request['payload']['events'][-1]['event_kind'] == 'task_succeeded'
    assert normalized_headers['x-a2a-notification-token'] == 'unit-token'
    assert refreshed['status'] == 'delivered'
    assert loaded['subscription_id'] == refreshed['subscription_id']
    assert replay['events'][-1]['event_kind'] == 'task_succeeded'
    assert renewed['status'] == 'active'
    assert cancelled['status'] == 'cancelled'


@pytest.mark.asyncio
@respx.mock
async def test_federation_client_credentials_auth_and_signed_card(tmp_path: Path) -> None:
    respx.route(host='127.0.0.1').pass_through()
    private_key_path, public_key_path = _write_rsa_keypair(tmp_path)
    runtime = FakeRuntime(
        tmp_path,
        server_overrides={
            'jwt': {
                'enabled': True,
                'issuer': 'https://issuer.example',
                'audience': 'easy-agent',
                'private_key_path': private_key_path,
                'public_key_path': public_key_path,
            },
            'security_schemes': [
                {
                    'name': 'oidc_main',
                    'type': 'oidc',
                    'openid_config_url': 'https://issuer.example/.well-known/openid-configuration',
                    'audience': 'easy-agent',
                }
            ],
            'security_requirements': [{'oidc_main': []}],
        },
    )
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    insecure_manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
            }
        ).federation
    )
    secure_manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {
                    'remotes': [
                        {
                            'name': 'loopback',
                            'base_url': base_url,
                            'auth': {
                                'type': 'oidc',
                                'oauth': {
                                    'audience': 'easy-agent',
                                    'openid_config_url': 'https://issuer.example/.well-known/openid-configuration',
                                    'client_id': 'client-id',
                                    'client_secret': 'client-secret',
                                    'grant_type': 'client_credentials',
                                },
                            },
                        }
                    ]
                },
            }
        ).federation
    )
    access_token = sign_server_token(
        runtime.config.federation.server.jwt,
        {'sub': 'svc-client', 'tenant': 'tenant-a', 'task_ids': []},
        expires_in_seconds=3600,
    )
    metadata_route = respx.get('https://issuer.example/.well-known/openid-configuration').mock(
        return_value=httpx.Response(
            200,
            json={
                'issuer': 'https://issuer.example',
                'token_endpoint': 'https://issuer.example/oauth/token',
                'jwks_uri': f'{base_url}/.well-known/jwks.json',
            },
        )
    )
    token_route = respx.post('https://issuer.example/oauth/token').mock(
        return_value=httpx.Response(
            200,
            json={'access_token': access_token, 'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'remote.run'},
        )
    )
    await insecure_manager.start()
    await secure_manager.start()
    try:
        with pytest.raises(RuntimeError, match='unsupported federation auth'):
            await insecure_manager.run_remote('loopback', 'local_echo', 'blocked')
        remote = await secure_manager.inspect_remote('loopback')
        allowed = await secure_manager.run_remote('loopback', 'local_echo', 'allowed')
    finally:
        await insecure_manager.aclose()
        await secure_manager.aclose()
        server.stop()

    assert metadata_route.called is True
    assert token_route.called is True
    assert remote['card']['signed_card']
    assert remote['card']['jwks_url'].endswith('/.well-known/jwks.json')
    assert allowed['result']['echo'] == 'ALLOWED'


@pytest.mark.asyncio
@respx.mock
async def test_federation_authorization_code_login_and_refresh(tmp_path: Path) -> None:
    respx.route(host='127.0.0.1').pass_through()
    private_key_path, public_key_path = _write_rsa_keypair(tmp_path)
    runtime = FakeRuntime(
        tmp_path,
        server_overrides={
            'jwt': {
                'enabled': True,
                'issuer': 'https://issuer.example',
                'audience': 'easy-agent',
                'private_key_path': private_key_path,
                'public_key_path': public_key_path,
            },
            'security_schemes': [
                {
                    'name': 'oidc_main',
                    'type': 'oidc',
                    'openid_config_url': 'https://issuer.example/.well-known/openid-configuration',
                    'audience': 'easy-agent',
                }
            ],
            'security_requirements': [{'oidc_main': []}],
        },
    )
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {
                    'remotes': [
                        {
                            'name': 'loopback',
                            'base_url': base_url,
                            'auth': {
                                'type': 'oidc',
                                'oauth': {
                                    'audience': 'easy-agent',
                                    'openid_config_url': 'https://issuer.example/.well-known/openid-configuration',
                                    'client_id': 'client-id',
                                    'client_secret': 'client-secret',
                                    'grant_type': 'authorization_code',
                                },
                            },
                        }
                    ]
                },
            }
        ).federation
    )
    metadata_route = respx.get('https://issuer.example/.well-known/openid-configuration').mock(
        return_value=httpx.Response(
            200,
            json={
                'issuer': 'https://issuer.example',
                'authorization_endpoint': 'https://issuer.example/oauth/authorize',
                'token_endpoint': 'https://issuer.example/oauth/token',
                'jwks_uri': f'{base_url}/.well-known/jwks.json',
            },
        )
    )
    token_calls = {'count': 0}

    def _token_handler(request: httpx.Request) -> httpx.Response:
        token_calls['count'] += 1
        body = request.content.decode('utf-8')
        if 'grant_type=authorization_code' in body:
            token = sign_server_token(
                runtime.config.federation.server.jwt,
                {'sub': 'user-1', 'tenant': 'tenant-a', 'task_ids': []},
                expires_in_seconds=1,
            )
            return httpx.Response(
                200,
                json={
                    'access_token': token,
                    'refresh_token': 'refresh-token-1',
                    'token_type': 'Bearer',
                    'expires_in': 1,
                },
            )
        refreshed = sign_server_token(
            runtime.config.federation.server.jwt,
            {'sub': 'user-1', 'tenant': 'tenant-a', 'task_ids': []},
            expires_in_seconds=3600,
        )
        return httpx.Response(
            200,
            json={
                'access_token': refreshed,
                'refresh_token': 'refresh-token-1',
                'token_type': 'Bearer',
                'expires_in': 3600,
            },
        )

    respx.post('https://issuer.example/oauth/token').mock(side_effect=_token_handler)
    redirects: list[str] = []

    async def _redirect(url: str) -> None:
        redirects.append(url)

    async def _callback() -> tuple[str, str | None]:
        parsed = httpx.URL(redirects[-1])
        return 'auth-code-1', parsed.params.get('state')

    await manager.start()
    manager.set_oauth_handlers(_redirect, _callback)
    try:
        status_payload = await manager.authorize('loopback')
        manager._auth_state('loopback')['tokens']['expires_at'] = time.time() - 1
        refreshed = await manager.run_remote('loopback', 'local_echo', 'refresh-me')
    finally:
        await manager.aclose()
        server.stop()

    assert metadata_route.called is True
    assert redirects and 'code_challenge=' in redirects[0]
    assert status_payload['authenticated'] is True
    assert status_payload['has_refresh_token'] is True
    assert token_calls['count'] >= 2
    assert refreshed['result']['echo'] == 'REFRESH-ME'


@pytest.mark.asyncio
async def test_federation_server_enforces_tenant_and_task_scope_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private_key_path, public_key_path = _write_rsa_keypair(tmp_path)
    runtime = FakeRuntime(
        tmp_path,
        server_overrides={
            'jwt': {
                'enabled': True,
                'issuer': 'https://issuer.example',
                'audience': 'easy-agent',
                'private_key_path': private_key_path,
                'public_key_path': public_key_path,
            },
            'security_schemes': [{'name': 'bearer_main', 'type': 'bearer'}],
            'security_requirements': [{'bearer_main': []}],
        },
    )
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    tenant_a_token = sign_server_token(
        runtime.config.federation.server.jwt,
        {'sub': 'user-a', 'tenant': 'tenant-a', 'task_ids': []},
        expires_in_seconds=3600,
    )
    tenant_b_token = sign_server_token(
        runtime.config.federation.server.jwt,
        {'sub': 'user-b', 'tenant': 'tenant-b', 'task_ids': []},
        expires_in_seconds=3600,
    )
    scoped_token = sign_server_token(
        runtime.config.federation.server.jwt,
        {'sub': 'user-a', 'tenant': 'tenant-a', 'task_ids': ['different-task']},
        expires_in_seconds=3600,
    )
    monkeypatch.setenv('TENANT_A_TOKEN', tenant_a_token)
    monkeypatch.setenv('TENANT_B_TOKEN', tenant_b_token)
    monkeypatch.setenv('TENANT_A_SCOPED_TOKEN', scoped_token)
    base_manager_config = {
        'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
        'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url, 'auth': {'type': 'bearer_env', 'token_env': 'TENANT_A_TOKEN'}}]},
    }
    manager_a = FederationClientManager(AppConfig.model_validate(base_manager_config).federation)
    manager_b = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url, 'auth': {'type': 'bearer_env', 'token_env': 'TENANT_B_TOKEN'}}]},
            }
        ).federation
    )
    manager_scoped = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url, 'auth': {'type': 'bearer_env', 'token_env': 'TENANT_A_SCOPED_TOKEN'}}]},
            }
        ).federation
    )
    created_items: list[dict[str, Any]] = []
    await manager_a.start()
    await manager_b.start()
    await manager_scoped.start()
    try:
        task = await manager_a.run_remote('loopback', 'local_echo', 'tenant-a-task')
        created_tasks = await manager_a.list_tasks('loopback')
        created_items = created_tasks['tasks']
        task_id = str(created_items[0]['task_id'])
        assert task['result']['echo'] == 'TENANT-A-TASK'
        tenant_b_tasks = await manager_b.list_tasks('loopback')
        scoped_tasks = await manager_scoped.list_tasks('loopback')
        with pytest.raises(httpx.HTTPStatusError):
            await manager_b.get_task('loopback', task_id)
        with pytest.raises(httpx.HTTPStatusError):
            await manager_scoped.get_task('loopback', task_id)
    finally:
        await manager_a.aclose()
        await manager_b.aclose()
        await manager_scoped.aclose()
        server.stop()

    assert len(created_items) == 1
    assert created_items[0]['tenant_id'] == 'tenant-a'
    assert tenant_b_tasks['tasks'] == []
    assert scoped_tasks['tasks'] == []
