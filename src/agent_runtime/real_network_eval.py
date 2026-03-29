from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent_common.models import HumanLoopMode, ToolSpec
from agent_config.app import (
    AppConfig,
    ContainerExecutorOptions,
    ExecutorConfig,
    MicrovmExecutorOptions,
)
from agent_integrations.executors import build_executor_backends
from agent_integrations.federation import FederationClientManager, FederationServer
from agent_integrations.sandbox import SandboxManager, SandboxMode, SandboxTarget
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager
from agent_runtime.runtime import build_runtime_from_config


@dataclass(slots=True)
class RealNetworkRecord:
    scenario: str
    transport: str
    live_model: bool
    host_dependency: str
    status: str
    duration_seconds: float
    notes: str


class _CallbackCollector:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self.deliveries: list[dict[str, Any]] = []
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
                payload = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                collector.attempts += 1
                collector.deliveries.append(payload)
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


class _FakeRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.config = AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'teams': [], 'nodes': []},
                'federation': {
                    'server': {
                        'enabled': True,
                        'host': '127.0.0.1',
                        'port': 0,
                        'base_path': '/a2a',
                        'retry_max_attempts': 3,
                        'retry_initial_backoff_seconds': 0.1,
                        'retry_backoff_multiplier': 1.0,
                        'subscription_lease_seconds': 30,
                    },
                    'exports': [
                        {
                            'name': 'local_echo',
                            'target_type': 'agent',
                            'target': 'coordinator',
                            'description': 'Echo target',
                            'modalities': ['text'],
                            'capabilities': ['streaming', 'interrupts'],
                        }
                    ],
                },
                'storage': {'path': str(tmp_path), 'database': 'state.db'},
            }
        )
        self.store = SQLiteRunStore(tmp_path, 'state.db')

    async def run_federated_export(self, export_name: str, input_text: str, *, session_id: str | None = None, approval_mode: HumanLoopMode = HumanLoopMode.HYBRID) -> dict[str, Any]:
        del approval_mode
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


_CROSS_PROCESS_SERVER = textwrap.dedent(
    """
    import json
    import sys
    import threading
    import time
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    tasks = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return None

        def _json(self):
            length = int(self.headers.get('Content-Length', '0') or '0')
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode('utf-8'))

        def _write(self, payload, status=200):
            data = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/.well-known/agent-card.json':
                self._write({'name': 'subproc', 'url': f'http://127.0.0.1:{self.server.server_address[1]}/a2a', 'protocol_version': '0.3', 'exports': [{'name': 'remote_echo', 'capabilities': {'modalities': ['text']}}]})
                return
            if self.path == '/a2a/agent-card':
                self._write({'name': 'subproc', 'url': f'http://127.0.0.1:{self.server.server_address[1]}/a2a', 'protocol_version': '0.3', 'exports': [{'name': 'remote_echo', 'capabilities': {'modalities': ['text']}}]})
                return
            if self.path in {'/a2a/extendedAgentCard', '/a2a/agent-card/extended'}:
                self._write({'name': 'subproc', 'capabilities': {'push_delivery': {'sse_events': False}}, 'retry_policy': {'max_attempts': 1}})
                return
            if self.path == '/a2a/tasks':
                self._write({'tasks': list(tasks.values())})
                return
            if self.path.startswith('/a2a/tasks/'):
                task_id = self.path.split('/')[-1]
                self._write({'task': tasks[task_id]})
                return
            self._write({'error': 'not_found'}, status=404)

        def do_POST(self):
            if self.path in {'/a2a/tasks/send', '/a2a/message:send'}:
                payload = self._json()
                task_id = uuid.uuid4().hex
                task = {'task_id': task_id, 'status': 'running', 'response_payload': None, 'error_message': None}
                tasks[task_id] = task
                def worker():
                    time.sleep(0.1)
                    task['status'] = 'succeeded'
                    task['response_payload'] = {'run_id': task_id, 'status': 'succeeded', 'result': {'echo': str(payload.get('input', '')).upper()}}
                threading.Thread(target=worker, daemon=True).start()
                self._write({'task': dict(task)}, status=202)
                return
            self._write({'error': 'not_found'}, status=404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
    """
)


def _record(scenario: str, transport: str, host_dependency: str, runner: Any, *, live_model: bool = False) -> RealNetworkRecord:
    start = time.perf_counter()
    try:
        notes = runner()
        status = 'passed'
    except RuntimeError as exc:
        status = 'skipped'
        notes = str(exc)
    except Exception as exc:  # noqa: BLE001
        status = 'failed'
        notes = str(exc)
    return RealNetworkRecord(
        scenario=scenario,
        transport=transport,
        live_model=live_model,
        host_dependency=host_dependency,
        status=status,
        duration_seconds=round(time.perf_counter() - start, 4),
        notes=notes,
    )


def _run(command: list[str], *, timeout_seconds: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_seconds)


def _podman_machine_info(podman_executable: str) -> dict[str, Any]:
    listing = _run([podman_executable, 'machine', 'list', '--format', 'json'], timeout_seconds=30.0)
    if listing.returncode != 0:
        raise RuntimeError(listing.stderr.strip() or listing.stdout.strip() or 'podman machine list failed')
    machines = json.loads(listing.stdout or '[]')
    if not machines:
        raise RuntimeError('skipped: no podman machine is configured')
    machine = next((item for item in machines if item.get('Default')), machines[0])
    if not machine.get('Running'):
        started = _run([podman_executable, 'machine', 'start', str(machine['Name'])], timeout_seconds=180.0)
        if started.returncode != 0:
            raise RuntimeError(started.stderr.strip() or started.stdout.strip() or 'failed to start podman machine')
    inspect = _run([podman_executable, 'machine', 'inspect', str(machine['Name'])], timeout_seconds=30.0)
    if inspect.returncode != 0:
        raise RuntimeError(inspect.stderr.strip() or inspect.stdout.strip() or 'podman machine inspect failed')
    payload = json.loads(inspect.stdout or '[]')
    details = payload[0] if isinstance(payload, list) and payload else payload
    ssh_config = dict(details.get('SSHConfig', {}))
    return {
        'name': str(details.get('Name') or machine['Name']),
        'port': int(ssh_config.get('Port') or machine.get('Port') or 0),
        'user': str(ssh_config.get('RemoteUsername') or machine.get('RemoteUsername') or 'user'),
        'identity_path': str(ssh_config.get('IdentityPath') or machine.get('IdentityPath') or ''),
    }


def _ssh_base_args(info: dict[str, Any]) -> list[str]:
    return [
        'ssh',
        '-o',
        'BatchMode=yes',
        '-o',
        'StrictHostKeyChecking=no',
        '-o',
        'UserKnownHostsFile=NUL',
        '-i',
        str(info['identity_path']),
        '-p',
        str(info['port']),
        f"{info['user']}@127.0.0.1",
    ]


def _ensure_offline_container_archive(cache_root: Path, podman_executable: str) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    archive_path = cache_root / 'podman-machine-python-rootfs.tar'
    if archive_path.exists() and 0 < archive_path.stat().st_size < 500 * 1024 * 1024:
        return archive_path
    archive_path.unlink(missing_ok=True)
    info = _podman_machine_info(podman_executable)
    remote_command = [
        'sudo',
        'tar',
        '--numeric-owner',
        '-C',
        '/',
        '-cf',
        '-',
        'usr/bin/python3',
        'usr/bin/python3.14',
        'usr/lib64/python3.14',
        'usr/lib/python3.14',
        'lib64/libpython3.14.so.1.0',
        'lib64/libc.so.6',
        'lib64/libm.so.6',
        'lib64/ld-linux-x86-64.so.2',
    ]
    with archive_path.open('wb') as handle:
        process = subprocess.Popen(
            [*_ssh_base_args(info), *remote_command],
            stdout=handle,
            stderr=subprocess.PIPE,
            shell=False,
        )
        _, stderr = process.communicate(timeout=900.0)
    if process.returncode != 0:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(stderr.decode('utf-8', errors='ignore').strip() or 'failed to export podman-machine rootfs archive')
    if archive_path.stat().st_size <= 0:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError('failed to create offline rootfs archive')
    return archive_path


def _scenario_cross_process_federation() -> str:
    process = subprocess.Popen([sys.executable, '-c', _CROSS_PROCESS_SERVER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        port_line = process.stdout.readline().strip() if process.stdout is not None else ''
        if not port_line:
            raise RuntimeError('subprocess federation server did not report a port')

        async def _run_async() -> str:
            manager = FederationClientManager(
                AppConfig.model_validate(
                    {
                        'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                        'federation': {'remotes': [{'name': 'subproc', 'base_url': f'http://127.0.0.1:{port_line}', 'push_preference': 'poll'}]},
                    }
                ).federation
            )
            await manager.start()
            try:
                remote = await manager.inspect_remote('subproc')
                result = await manager.run_remote('subproc', 'remote_echo', 'cross-process')
            finally:
                await manager.aclose()
            if remote['card']['exports'][0]['name'] != 'remote_echo':
                raise AssertionError('unexpected remote export')
            if result['result']['echo'] != 'CROSS-PROCESS':
                raise AssertionError('unexpected remote result')
            return 'cross-process well-known discovery and send/poll federation passed'

        return asyncio.run(_run_async())
    finally:
        process.terminate()
        process.wait(timeout=5)


def _scenario_federation_retry_and_reconnect(tmp_path: Path) -> str:
    runtime = _FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    callback = _CallbackCollector(fail_first=True)
    callback_url = callback.start()
    status = server.start()
    base_url = f"http://127.0.0.1:{status['port']}"

    async def _run_async() -> str:
        manager = FederationClientManager(
            AppConfig.model_validate(
                {
                    'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                    'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
                }
            ).federation,
            store=runtime.store,
        )
        await manager.start()
        try:
            response = await manager.send_subscribe('loopback', 'local_echo', 'deliver-me', callback_url, from_sequence=0)
            task_id = str(response['task']['task_id'])
            subscriptions: list[dict[str, Any]] = []
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                subscriptions = await manager.list_push_notifications('loopback', task_id)
                if subscriptions and subscriptions[0]['status'] == 'delivered':
                    break
                await asyncio.sleep(0.2)
            if not subscriptions or subscriptions[0]['status'] != 'delivered':
                raise AssertionError(f"subscription not delivered: {subscriptions[0]['status'] if subscriptions else 'missing'}")
            replay = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
            renewed = await manager.renew_subscription('loopback', task_id, str(subscriptions[0]['subscription_id']), lease_seconds=60)
            cancelled = await manager.cancel_subscription('loopback', task_id, str(subscriptions[0]['subscription_id']))
            if not replay['events']:
                raise AssertionError('resubscribe did not return backlog events')
            if renewed['status'] != 'active' or cancelled['status'] != 'cancelled':
                raise AssertionError('subscription lifecycle mismatch')
            if callback.attempts < 2:
                raise AssertionError('retry backoff was not exercised')
            return 'callback retry, pushNotificationConfig, sendSubscribe, and resubscribe passed'
        finally:
            await manager.aclose()

    try:
        return asyncio.run(_run_async())
    finally:
        callback.stop()
        server.stop()


def _workbench_manager(base_path: Path, executors: list[ExecutorConfig], default_executor: str) -> WorkbenchManager:
    sandbox = SandboxManager(
        mode=SandboxMode.PROCESS,
        targets=[SandboxTarget.COMMAND_SKILL, SandboxTarget.STDIO_MCP],
        env_allowlist=['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'USERNAME', 'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA', 'PUBLIC'],
        working_root=base_path,
    )
    store = SQLiteRunStore(base_path / 'state', 'state.db')
    return WorkbenchManager(store, build_executor_backends(executors, sandbox), base_path / 'workbench', default_executor=default_executor, session_ttl_seconds=300)


def _scenario_process_workbench_reuse(tmp_path: Path) -> str:
    manager = _workbench_manager(tmp_path, [ExecutorConfig(name='process', kind='process')], 'process')
    first = manager.ensure_session('run-process', 'skill-echo')
    (first.root_path / 'artifact.txt').write_text('persisted', encoding='utf-8')
    second = manager.ensure_session('run-process', 'skill-echo')
    if first.session_id != second.session_id:
        raise AssertionError('expected workbench session reuse for same owner/name')
    if (second.root_path / 'artifact.txt').read_text(encoding='utf-8') != 'persisted':
        raise AssertionError('workbench artifact was not reused')
    return 'process workbench reused the same long-lived session root'


def _scenario_container_workbench_reuse(tmp_path: Path) -> str:
    podman_executable = os.environ.get('EASY_AGENT_PODMAN_EXE', 'podman')
    archive = _ensure_offline_container_archive(Path('.easy-agent/offline-images'), podman_executable)
    image = os.environ.get('EASY_AGENT_CONTAINER_IMAGE', 'localhost/easy-agent/offline-python:latest')
    manager = _workbench_manager(
        tmp_path,
        [
            ExecutorConfig(
                name='containerized',
                kind='container',
                default_timeout_seconds=120,
                container=ContainerExecutorOptions(
                    executable=podman_executable,
                    image=image,
                    image_archive=str(archive),
                    keepalive_command=['python3', '-c', 'import time; time.sleep(10**9)'],
                    auto_load=True,
                    auto_build=False,
                    checkpoint_enabled=True,
                    memory_mb=512,
                    cpus=1.0,
                ),
            )
        ],
        'containerized',
    )
    session = manager.ensure_session('run-container', 'skill-echo')
    result = manager.run_command(
        session.session_id,
        ['python3', '-c', "from pathlib import Path; Path('container-marker.txt').write_text('container-checkpoint', encoding='utf-8'); print('container-ok')"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result.returncode != 0 or 'container-ok' not in result.stdout:
        raise AssertionError(result.stderr or result.stdout)
    shutdown = manager.shutdown_session(session.session_id)
    if shutdown.runtime_state.get('status') != 'checkpointed':
        raise AssertionError('container checkpoint state was not recorded')
    restarted = manager.restart_session(session.session_id)
    result_after = manager.run_command(
        restarted.session_id,
        ['python3', '-c', "from pathlib import Path; print(Path('container-marker.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result_after.returncode != 0 or 'container-checkpoint' not in result_after.stdout:
        raise AssertionError(result_after.stderr or result_after.stdout)
    if restarted.runtime_state.get('image') != restarted.runtime_state.get('snapshot_image'):
        raise AssertionError('container restart did not restore from the snapshot image')
    return 'container executor loaded an offline image archive, enforced quotas, and resumed from a snapshot image'


def _scenario_microvm_workbench_reuse(tmp_path: Path) -> str:
    podman_executable = os.environ.get('EASY_AGENT_PODMAN_EXE', 'podman')
    machine = _podman_machine_info(podman_executable)
    manager = _workbench_manager(
        tmp_path,
        [
            ExecutorConfig(
                name='microvm-machine',
                kind='microvm',
                default_timeout_seconds=90,
                microvm=MicrovmExecutorOptions(
                    provider='podman_machine',
                    executable=podman_executable,
                    machine_name=machine['name'],
                    ssh_user=machine['user'],
                    ssh_private_key=machine['identity_path'],
                    guest_workdir='/tmp/easy-agent-workbench',
                    checkpoint_enabled=True,
                ),
            )
        ],
        'microvm-machine',
    )
    session = manager.ensure_session('run-microvm', 'skill-echo')
    result = manager.run_command(
        session.session_id,
        ['python3', '-c', "from pathlib import Path; Path('microvm-marker.txt').write_text('microvm-checkpoint', encoding='utf-8'); print('microvm-ok')"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result.returncode != 0 or 'microvm-ok' not in result.stdout:
        raise AssertionError(result.stderr or result.stdout)
    manager.shutdown_session(session.session_id)
    restarted = manager.restart_session(session.session_id)
    result_after = manager.run_command(
        restarted.session_id,
        ['python3', '-c', "from pathlib import Path; print(Path('microvm-marker.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result_after.returncode != 0 or 'microvm-checkpoint' not in result_after.stdout:
        raise AssertionError(result_after.stderr or result_after.stdout)
    return 'microvm executor reused the podman machine over SSH and recovered after a disconnect-style restart'


def _scenario_replay_resume_failure_injection(tmp_path: Path) -> str:
    config = AppConfig.model_validate(
        {
            'graph': {
                'name': 'resume-check',
                'entrypoint': 'review',
                'agents': [{'name': 'worker', 'description': 'worker'}],
                'teams': [],
                'nodes': [
                    {'id': 'prepare', 'type': 'join'},
                    {'id': 'review', 'type': 'tool', 'target': 'flaky_tool', 'deps': ['prepare']},
                ],
            },
            'storage': {'path': str(tmp_path / 'state'), 'database': 'state.db'},
        }
    )
    runtime = build_runtime_from_config(config)
    counter = {'calls': 0}

    async def flaky_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        del arguments, context
        counter['calls'] += 1
        if counter['calls'] == 1:
            raise RuntimeError('injected failure')
        return {'status': 'recovered'}

    runtime.register_tool(spec=ToolSpec(name='flaky_tool', description='flaky', input_schema={'type': 'object'}), handler=flaky_tool)

    async def _run_async() -> str:
        try:
            first_run_id: str | None = None
            try:
                result = await runtime.run('resume me')
                first_run_id = str(result['run_id'])
            except RuntimeError as exc:
                message = str(exc)
                if 'failed' not in message:
                    raise
                first_run_id = message.split('Run ', 1)[1].split(' failed', 1)[0]
            if not first_run_id:
                raise AssertionError('missing failed run id')
            checkpoints = runtime.list_checkpoints(first_run_id)
            if not checkpoints:
                raise AssertionError('expected checkpoints before resume')
            resumed = await runtime.resume(first_run_id)
            replay = await runtime.replay(first_run_id, checkpoints[-1]['checkpoint_id'])
            forked = await runtime.resume(first_run_id, checkpoint_id=checkpoints[-1]['checkpoint_id'], fork=True)
            if resumed['result']['status'] != 'recovered':
                raise AssertionError('resume did not recover')
            if replay['checkpoint_kind'] != 'graph':
                raise AssertionError('replay did not return graph checkpoint')
            if forked['run_id'] == first_run_id:
                raise AssertionError('fork resume should allocate a new run id')
            return 'resume, replay, and fork recovery passed under injected failure'
        finally:
            await runtime.aclose()

    return asyncio.run(_run_async())


def run_real_network_suite(config_path: str | Path = 'easy-agent.yml') -> dict[str, Any]:
    del config_path
    output_root = Path('.easy-agent')
    output_root.mkdir(parents=True, exist_ok=True)
    tmp_root = output_root / 'real-network-tmp' / str(int(time.time() * 1000))
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        records = [
            _record('cross_process_federation', 'http_poll', 'python subprocess', _scenario_cross_process_federation),
            _record('disconnect_retry_chaos', 'http_webhook', 'loopback callback server', lambda: _scenario_federation_retry_and_reconnect(tmp_root / 'federation')),
            _record('workbench_reuse_process', 'local_process', 'none', lambda: _scenario_process_workbench_reuse(tmp_root / 'process')),
            _record('workbench_reuse_container', 'podman_exec', 'podman machine rootfs import', lambda: _scenario_container_workbench_reuse(tmp_root / 'container')),
            _record('workbench_reuse_microvm', 'podman_machine_ssh', 'podman machine ssh', lambda: _scenario_microvm_workbench_reuse(tmp_root / 'microvm')),
            _record('replay_resume_failure_injection', 'sqlite_checkpoint', 'none', lambda: _scenario_replay_resume_failure_injection(tmp_root / 'resume')),
        ]
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    summary = {
        'runs': len(records),
        'passed': sum(1 for item in records if item.status == 'passed'),
        'failed': sum(1 for item in records if item.status == 'failed'),
        'skipped': sum(1 for item in records if item.status == 'skipped'),
    }
    report = {
        'records': [asdict(item) for item in records],
        'summary': summary,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    report_path = output_root / 'real-network-report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
