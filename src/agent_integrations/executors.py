from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_config.app import ExecutorConfig
from agent_integrations.sandbox import (
    PreparedSubprocess,
    SandboxManager,
    SandboxRequest,
    SandboxResult,
    SandboxTarget,
)


@dataclass(slots=True)
class ExecutorSession:
    session_id: str
    root_path: Path
    executor_name: str
    runtime_state: dict[str, Any]


class ExecutorBackend(Protocol):
    name: str
    kind: str

    def describe(self) -> dict[str, Any]: ...

    def ensure_session(self, session: ExecutorSession) -> dict[str, Any]: ...

    def prepare_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> PreparedSubprocess: ...

    def run_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> SandboxResult: ...

    def sync_to_host(self, session: ExecutorSession) -> dict[str, Any]: ...

    def shutdown_session(self, session: ExecutorSession) -> dict[str, Any]: ...


def _command_exists(executable: str) -> bool:
    return Path(executable).exists() or shutil.which(executable) is not None


def _run_subprocess(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 30.0,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        check=False,
        timeout=timeout_seconds,
        shell=False,
    )


def _run_json_command(command: list[str], *, timeout_seconds: float = 30.0) -> Any:
    result = _run_subprocess(command, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'command failed: {command}')
    body = (result.stdout or '').strip()
    return json.loads(body) if body else None


def _quote_remote_shell(token: str) -> str:
    return "'" + token.replace("'", "'\"'\"'") + "'"


def _is_podman_command(executable: str) -> bool:
    resolved = Path(executable).name.lower() if Path(executable).suffix else executable.lower()
    return 'podman' in resolved


def _ensure_podman_machine_running(executable: str) -> None:
    if not _is_podman_command(executable) or not _command_exists(executable):
        return
    try:
        machines = _run_json_command([executable, 'machine', 'list', '--format', 'json'], timeout_seconds=30.0) or []
    except Exception:  # noqa: BLE001
        return
    if not machines:
        raise RuntimeError('no podman machine is configured')
    selected = next((item for item in machines if item.get('Default')), machines[0])
    if selected.get('Running'):
        return
    name = str(selected['Name'])
    result = _run_subprocess([executable, 'machine', 'start', name], timeout_seconds=180.0)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'failed to start podman machine {name}')


class ProcessExecutorBackend:
    kind = 'process'

    def __init__(self, config: ExecutorConfig, sandbox_manager: SandboxManager) -> None:
        self.name = config.name
        self._sandbox_manager = sandbox_manager

    def describe(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'kind': self.kind,
            'available': True,
            'details': {'mode': self._sandbox_manager.mode.value},
        }

    def ensure_session(self, session: ExecutorSession) -> dict[str, Any]:
        del session
        return {}

    def prepare_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> PreparedSubprocess:
        return self._sandbox_manager.prepare(
            SandboxRequest(
                command=command,
                cwd=session.root_path,
                env=env,
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )

    def run_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> SandboxResult:
        return self._sandbox_manager.run(
            SandboxRequest(
                command=command,
                cwd=session.root_path,
                env=env,
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )

    def sync_to_host(self, session: ExecutorSession) -> dict[str, Any]:
        return dict(session.runtime_state)

    def shutdown_session(self, session: ExecutorSession) -> dict[str, Any]:
        return dict(session.runtime_state)


class ContainerExecutorBackend:
    kind = 'container'

    def __init__(self, config: ExecutorConfig, sandbox_manager: SandboxManager) -> None:
        if config.container is None:
            raise ValueError(f"executor '{config.name}' requires container config")
        self.name = config.name
        self._config = config
        self._sandbox_manager = sandbox_manager
        self._container = config.container

    def describe(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'kind': self.kind,
            'available': _command_exists(self._container.executable),
            'details': {
                'executable': self._container.executable,
                'image': self._container.image,
                'image_archive': self._container.image_archive or '',
                'bootstrap_context': self._container.bootstrap_context or '',
                'checkpoint_enabled': self._container.checkpoint_enabled,
                'memory_mb': self._container.memory_mb,
                'cpus': self._container.cpus,
                'workdir': self._container.workdir,
            },
        }

    def ensure_session(self, session: ExecutorSession) -> dict[str, Any]:
        state = dict(session.runtime_state)
        container_name = str(state.get('container_name') or f'easy-agent-{self.name}-{session.session_id[:12]}')
        state['container_name'] = container_name
        if not _command_exists(self._container.executable):
            state.update({'status': 'unavailable', 'last_error': f"missing executable: {self._container.executable}"})
            return state
        _ensure_podman_machine_running(self._container.executable)
        session.root_path.mkdir(parents=True, exist_ok=True)
        image = self._resolve_image(session, state)
        if self._container.checkpoint_enabled:
            state['snapshot_image'] = self._snapshot_image_name(session)
        if state.get('status') == 'running' and self._container_exists(container_name):
            state['image'] = image
            return state
        if self._container_exists(container_name):
            _run_subprocess([self._container.executable, 'rm', '--force', container_name], timeout_seconds=20.0)
        command = [
            self._container.executable,
            'run',
            '--detach',
            '--rm',
            '--name',
            container_name,
            '--workdir',
            self._container.workdir,
            '--volume',
            f'{session.root_path.resolve()}:{self._container.workdir}',
            *self._resource_args(),
            *self._container.run_args,
            image,
            *self._container.keepalive_command,
        ]
        result = _run_subprocess(command, timeout_seconds=max(60.0, self._config.default_timeout_seconds))
        if result.returncode != 0:
            state.update({'status': 'failed', 'last_error': result.stderr.strip() or result.stdout.strip()})
            return state
        state.update(
            {
                'status': 'running',
                'image': image,
                'container_id': result.stdout.strip() or container_name,
                'started_at': time.time(),
            }
        )
        return state

    def prepare_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> PreparedSubprocess:
        state = self.ensure_session(session)
        if state.get('status') != 'running':
            raise RuntimeError(str(state.get('last_error') or 'container executor is unavailable'))
        wrapped = [
            self._container.executable,
            'exec',
            *self._container.exec_args,
            *self._env_args(env),
            str(state['container_name']),
            *command,
        ]
        return self._sandbox_manager.prepare(
            SandboxRequest(
                command=wrapped,
                cwd=session.root_path,
                env={},
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )

    def run_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> SandboxResult:
        prepared = self.prepare_command(
            session,
            command,
            env=env,
            timeout_seconds=timeout_seconds,
            target=target,
        )
        return self._sandbox_manager.run(
            SandboxRequest(
                command=prepared.command,
                cwd=prepared.cwd,
                env=prepared.env,
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )

    def sync_to_host(self, session: ExecutorSession) -> dict[str, Any]:
        return dict(session.runtime_state)

    def shutdown_session(self, session: ExecutorSession) -> dict[str, Any]:
        state = dict(session.runtime_state)
        container_name = str(state.get('container_name') or '')
        if not container_name or not _command_exists(self._container.executable):
            return state
        if self._container.checkpoint_enabled and state.get('status') == 'running' and self._container_exists(container_name):
            snapshot_image = str(state.get('snapshot_image') or self._snapshot_image_name(session))
            commit = _run_subprocess(
                [self._container.executable, 'commit', container_name, snapshot_image],
                timeout_seconds=max(60.0, self._config.default_timeout_seconds),
            )
            if commit.returncode == 0:
                state['snapshot_image'] = snapshot_image
                state['status'] = 'checkpointed'
            else:
                state['last_error'] = commit.stderr.strip() or commit.stdout.strip()
        if self._container_exists(container_name):
            _run_subprocess([self._container.executable, 'rm', '--force', container_name], timeout_seconds=20.0)
        if state.get('status') != 'checkpointed':
            state['status'] = 'stopped'
        return state

    def _container_exists(self, container_name: str) -> bool:
        result = _run_subprocess(
            [self._container.executable, 'container', 'exists', container_name],
            timeout_seconds=20.0,
        )
        return result.returncode == 0

    def _image_exists(self, image: str) -> bool:
        result = _run_subprocess([self._container.executable, 'image', 'exists', image], timeout_seconds=20.0)
        return result.returncode == 0

    def _resolve_image(self, session: ExecutorSession, state: dict[str, Any]) -> str:
        snapshot_image = str(state.get('snapshot_image') or self._snapshot_image_name(session))
        if self._container.checkpoint_enabled and self._image_exists(snapshot_image):
            return snapshot_image
        self._ensure_image_available(self._container.image)
        return self._container.image

    def _ensure_image_available(self, image: str) -> None:
        if self._image_exists(image):
            return
        if self._container.auto_load and self._container.image_archive:
            archive = Path(self._container.image_archive)
            if archive.exists():
                load_result = _run_subprocess([self._container.executable, 'load', '-i', str(archive)], timeout_seconds=1200.0)
                if load_result.returncode != 0:
                    import_result = _run_subprocess(
                        [self._container.executable, 'import', str(archive), image],
                        timeout_seconds=1200.0,
                    )
                    if import_result.returncode != 0:
                        raise RuntimeError(
                            import_result.stderr.strip()
                            or import_result.stdout.strip()
                            or load_result.stderr.strip()
                            or load_result.stdout.strip()
                            or f'failed to load image archive {archive}'
                        )
                if self._image_exists(image):
                    return
        if self._container.auto_build:
            context_root = Path(self._container.bootstrap_context or '.').resolve()
            containerfile = self._container.bootstrap_containerfile
            command = [self._container.executable, 'build', '-t', image]
            if containerfile:
                command.extend(['-f', str(Path(containerfile).resolve())])
            command.append(str(context_root))
            result = _run_subprocess(command, timeout_seconds=600.0)
            if result.returncode == 0 and self._image_exists(image):
                return
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'failed to build image {image}')
        raise RuntimeError(f'container image is unavailable: {image}')

    def _snapshot_image_name(self, session: ExecutorSession) -> str:
        base = self._container.image
        if ':' in base.rsplit('/', 1)[-1]:
            repository, _ = base.rsplit(':', 1)
            return f'{repository}:snapshot-{session.session_id[:12]}'
        return f'{base}:snapshot-{session.session_id[:12]}'

    def _resource_args(self) -> list[str]:
        arguments: list[str] = []
        if self._container.memory_mb is not None:
            arguments.extend(['--memory', f'{int(self._container.memory_mb)}m'])
        if self._container.cpus is not None:
            arguments.extend(['--cpus', str(self._container.cpus)])
        return arguments

    @staticmethod
    def _env_args(env: dict[str, str]) -> list[str]:
        arguments: list[str] = []
        for key, value in env.items():
            arguments.extend(['--env', f'{key}={value}'])
        return arguments


class MicrovmExecutorBackend:
    kind = 'microvm'

    def __init__(self, config: ExecutorConfig, sandbox_manager: SandboxManager) -> None:
        if config.microvm is None:
            raise ValueError(f"executor '{config.name}' requires microvm config")
        self.name = config.name
        self._config = config
        self._sandbox_manager = sandbox_manager
        self._microvm = config.microvm

    def describe(self) -> dict[str, Any]:
        available = False
        if self._microvm.provider == 'podman_machine':
            available = _command_exists(self._microvm.executable) and _command_exists('ssh') and _command_exists('scp')
        else:
            available = (
                _command_exists(self._microvm.executable)
                and _command_exists('ssh')
                and _command_exists('scp')
                and bool(self._microvm.base_image)
            )
        return {
            'name': self.name,
            'kind': self.kind,
            'available': available,
            'details': {
                'provider': self._microvm.provider,
                'executable': self._microvm.executable,
                'base_image': self._microvm.base_image or '',
                'machine_name': self._microvm.machine_name,
                'ssh_user': self._microvm.ssh_user,
                'ssh_port_base': self._microvm.ssh_port_base,
                'guest_workdir': self._microvm.guest_workdir,
                'memory_mb': self._microvm.memory_mb,
                'cpus': self._microvm.cpus,
                'checkpoint_enabled': self._microvm.checkpoint_enabled,
            },
        }

    def ensure_session(self, session: ExecutorSession) -> dict[str, Any]:
        if self._microvm.provider == 'podman_machine':
            return self._ensure_podman_machine_session(session)
        return self._ensure_qemu_session(session)

    def prepare_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> PreparedSubprocess:
        state = self.ensure_session(session)
        if state.get('status') != 'running':
            raise RuntimeError(str(state.get('last_error') or 'microvm executor is unavailable'))
        self._sync_to_guest(session.root_path, state)
        wrapped = self._build_ssh_command(state, command, env)
        return self._sandbox_manager.prepare(
            SandboxRequest(
                command=wrapped,
                cwd=session.root_path,
                env={},
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )

    def run_command(
        self,
        session: ExecutorSession,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> SandboxResult:
        prepared = self.prepare_command(
            session,
            command,
            env=env,
            timeout_seconds=timeout_seconds,
            target=target,
        )
        result = self._sandbox_manager.run(
            SandboxRequest(
                command=prepared.command,
                cwd=prepared.cwd,
                env=prepared.env,
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )
        state = dict(session.runtime_state)
        if state.get('status') == 'running':
            self._sync_to_host(session.root_path, state)
        return result

    def sync_to_host(self, session: ExecutorSession) -> dict[str, Any]:
        state = dict(session.runtime_state)
        if state.get('status') == 'running':
            self._sync_to_host(session.root_path, state)
        return state

    def shutdown_session(self, session: ExecutorSession) -> dict[str, Any]:
        state = dict(session.runtime_state)
        if state.get('status') == 'running':
            self._sync_to_host(session.root_path, state)
        if self._microvm.provider == 'qemu':
            process_id = state.get('process_id')
            if process_id:
                try:
                    os.kill(int(process_id), 15)
                except OSError:
                    pass
        state['status'] = 'checkpointed' if self._microvm.checkpoint_enabled else 'stopped'
        return state

    def _ensure_podman_machine_session(self, session: ExecutorSession) -> dict[str, Any]:
        state = dict(session.runtime_state)
        if not _command_exists(self._microvm.executable):
            state.update({'status': 'unavailable', 'last_error': f"missing executable: {self._microvm.executable}"})
            return state
        if not _command_exists('ssh') or not _command_exists('scp'):
            state.update({'status': 'unavailable', 'last_error': 'ssh/scp executables are required'})
            return state
        _ensure_podman_machine_running(self._microvm.executable)
        inspect = _run_json_command([self._microvm.executable, 'machine', 'inspect', self._microvm.machine_name], timeout_seconds=30.0)
        machine = inspect[0] if isinstance(inspect, list) and inspect else inspect
        ssh_config = dict(machine.get('SSHConfig', {}))
        ssh_port = int(ssh_config.get('Port') or self._microvm.ssh_port_base)
        ssh_user = str(ssh_config.get('RemoteUsername') or self._microvm.ssh_user)
        ssh_key = str(ssh_config.get('IdentityPath') or self._microvm.ssh_private_key or '')
        guest_workdir = self._guest_session_root(session.session_id)
        state.update(
            {
                'status': 'running',
                'provider': 'podman_machine',
                'machine_name': self._microvm.machine_name,
                'ssh_port': ssh_port,
                'ssh_user': ssh_user,
                'ssh_private_key': ssh_key,
                'guest_workdir': guest_workdir,
                'started_at': state.get('started_at') or time.time(),
            }
        )
        self._wait_for_ssh(ssh_port)
        self._sync_to_guest(session.root_path, state)
        return state

    def _ensure_qemu_session(self, session: ExecutorSession) -> dict[str, Any]:
        state = dict(session.runtime_state)
        if state.get('status') == 'running':
            return state
        if not _command_exists(self._microvm.executable):
            state.update({'status': 'unavailable', 'last_error': f"missing executable: {self._microvm.executable}"})
            return state
        if not self._microvm.base_image:
            state.update({'status': 'unavailable', 'last_error': 'microvm base_image is required'})
            return state
        if not _command_exists('ssh') or not _command_exists('scp'):
            state.update({'status': 'unavailable', 'last_error': 'ssh/scp executables are required'})
            return state
        overlay_path = session.root_path / 'guest-overlay.qcow2'
        port = int(state.get('ssh_port') or self._allocate_port())
        launch_args = self._build_launch_command(overlay_path, port)
        process = subprocess.Popen(
            launch_args,
            cwd=session.root_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        state.update(
            {
                'status': 'starting',
                'provider': 'qemu',
                'process_id': process.pid,
                'ssh_port': port,
                'ssh_user': self._microvm.ssh_user,
                'ssh_private_key': self._microvm.ssh_private_key or '',
                'guest_workdir': self._microvm.guest_workdir,
                'overlay_path': str(overlay_path),
                'started_at': time.time(),
            }
        )
        self._wait_for_ssh(port)
        self._sync_to_guest(session.root_path, state)
        state['status'] = 'running'
        return state

    def _build_launch_command(self, overlay_path: Path, ssh_port: int) -> list[str]:
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        qemu_img = self._qemu_img_executable()
        if qemu_img and not overlay_path.exists():
            _run_subprocess(
                [
                    qemu_img,
                    'create',
                    '-f',
                    'qcow2',
                    '-F',
                    'qcow2',
                    '-b',
                    str(self._microvm.base_image),
                    str(overlay_path),
                ],
                timeout_seconds=15.0,
            )
        drive_path = overlay_path if overlay_path.exists() else Path(str(self._microvm.base_image))
        return [
            self._microvm.executable,
            '-machine',
            'microvm,accel=tcg',
            '-m',
            str(self._microvm.memory_mb),
            '-smp',
            str(self._microvm.cpus),
            '-display',
            'none',
            '-nodefaults',
            '-no-user-config',
            '-nic',
            f'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{ssh_port}-:22',
            '-drive',
            f'if=virtio,format=qcow2,file={drive_path}',
            *[item.format(ssh_port=ssh_port, overlay_path=str(overlay_path)) for item in self._microvm.extra_args],
        ]

    def _build_ssh_command(self, state: dict[str, Any], command: list[str], env: dict[str, str]) -> list[str]:
        env_prefix = ' '.join(f'{key}={_quote_remote_shell(value)}' for key, value in env.items())
        remote_command = ' '.join(_quote_remote_shell(token) for token in command)
        shell_command = f"mkdir -p {_quote_remote_shell(str(state['guest_workdir']))} && cd {_quote_remote_shell(str(state['guest_workdir']))} && {env_prefix + ' ' if env_prefix else ''}{remote_command}"
        return [
            'ssh',
            '-o',
            'BatchMode=yes',
            '-o',
            'StrictHostKeyChecking=no',
            '-o',
            'UserKnownHostsFile=NUL',
            '-p',
            str(state['ssh_port']),
            *self._identity_args(str(state.get('ssh_private_key') or '')),
            f"{state['ssh_user']}@127.0.0.1",
            shell_command,
        ]

    def _sync_to_guest(self, root_path: Path, state: dict[str, Any]) -> None:
        root_path.mkdir(parents=True, exist_ok=True)
        remote_dir = str(state['guest_workdir'])
        mkdir = self._build_ssh_command(state, ['mkdir', '-p', remote_dir], {})
        mkdir_result = _run_subprocess(mkdir, timeout_seconds=30.0)
        if mkdir_result.returncode != 0:
            raise RuntimeError(mkdir_result.stderr.strip() or mkdir_result.stdout.strip() or 'failed to prepare guest workdir')
        tar_pack = subprocess.Popen(
            ['tar', '-cf', '-', '-C', str(root_path), '.'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        ssh_unpack = subprocess.Popen(
            [
                'ssh',
                '-o',
                'BatchMode=yes',
                '-o',
                'StrictHostKeyChecking=no',
                '-o',
                'UserKnownHostsFile=NUL',
                '-p',
                str(state['ssh_port']),
                *self._identity_args(str(state.get('ssh_private_key') or '')),
                f"{state['ssh_user']}@127.0.0.1",
                f"mkdir -p {_quote_remote_shell(remote_dir)} && tar -xf - -C {_quote_remote_shell(remote_dir)}",
            ],
            stdin=tar_pack.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
        )
        assert tar_pack.stdout is not None
        tar_pack.stdout.close()
        tar_stderr = tar_pack.stderr.read() if tar_pack.stderr is not None else b''
        ssh_stderr = ssh_unpack.communicate(timeout=180.0)[1] or b''
        tar_returncode = tar_pack.wait(timeout=30.0)
        if tar_returncode != 0 or ssh_unpack.returncode != 0:
            raise RuntimeError(
                ssh_stderr.decode('utf-8', errors='ignore').strip()
                or tar_stderr.decode('utf-8', errors='ignore').strip()
                or 'failed to sync workbench to guest'
            )

    def _sync_to_host(self, root_path: Path, state: dict[str, Any]) -> None:
        root_path.mkdir(parents=True, exist_ok=True)
        ssh_pack = subprocess.Popen(
            [
                'ssh',
                '-o',
                'BatchMode=yes',
                '-o',
                'StrictHostKeyChecking=no',
                '-o',
                'UserKnownHostsFile=NUL',
                '-p',
                str(state['ssh_port']),
                *self._identity_args(str(state.get('ssh_private_key') or '')),
                f"{state['ssh_user']}@127.0.0.1",
                f"mkdir -p {_quote_remote_shell(str(state['guest_workdir']))} && tar -cf - -C {_quote_remote_shell(str(state['guest_workdir']))} .",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        tar_unpack = subprocess.Popen(
            ['tar', '-xf', '-', '-C', str(root_path)],
            stdin=ssh_pack.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
        )
        assert ssh_pack.stdout is not None
        ssh_pack.stdout.close()
        ssh_stderr = ssh_pack.stderr.read() if ssh_pack.stderr is not None else b''
        tar_stderr = tar_unpack.communicate(timeout=180.0)[1] or b''
        ssh_returncode = ssh_pack.wait(timeout=30.0)
        if ssh_returncode != 0 or tar_unpack.returncode != 0:
            raise RuntimeError(
                ssh_stderr.decode('utf-8', errors='ignore').strip()
                or tar_stderr.decode('utf-8', errors='ignore').strip()
                or 'failed to sync workbench to host'
            )

    def _wait_for_ssh(self, ssh_port: int) -> None:
        deadline = time.monotonic() + self._config.default_timeout_seconds
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', ssh_port), timeout=1):
                    return
            except OSError:
                time.sleep(0.5)
        raise TimeoutError(f'microvm ssh port {ssh_port} did not become ready')

    def _qemu_img_executable(self) -> str | None:
        executable_path = Path(self._microvm.executable)
        if executable_path.exists():
            candidate = executable_path.with_name('qemu-img.exe')
            if candidate.exists():
                return str(candidate)
        return shutil.which('qemu-img')

    def _allocate_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = int(sock.getsockname()[1])
        return max(port, self._microvm.ssh_port_base)

    def _guest_session_root(self, session_id: str) -> str:
        return f"{self._microvm.guest_workdir.rstrip('/')}/{session_id}"

    @staticmethod
    def _identity_args(private_key: str) -> list[str]:
        if not private_key:
            return []
        return ['-i', private_key]


def build_executor_backends(configs: list[ExecutorConfig], sandbox_manager: SandboxManager) -> dict[str, ExecutorBackend]:
    backends: dict[str, ExecutorBackend] = {}
    for config in configs:
        if config.kind == 'process':
            backends[config.name] = ProcessExecutorBackend(config, sandbox_manager)
        elif config.kind == 'container':
            backends[config.name] = ContainerExecutorBackend(config, sandbox_manager)
        elif config.kind == 'microvm':
            backends[config.name] = MicrovmExecutorBackend(config, sandbox_manager)
        else:
            raise ValueError(f'Unsupported executor kind: {config.kind}')
    return backends
