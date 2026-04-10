from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def command_exists(executable: str) -> bool:
    return Path(executable).exists() or shutil.which(executable) is not None


def run_subprocess(
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


def run_json_command(command: list[str], *, timeout_seconds: float = 30.0) -> Any:
    result = run_subprocess(command, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'command failed: {command}')
    body = (result.stdout or '').strip()
    return json.loads(body) if body else None


def quote_remote_shell(token: str) -> str:
    return "'" + token.replace("'", "'\"'\"'") + "'"


def is_podman_command(executable: str) -> bool:
    resolved = Path(executable).name.lower() if Path(executable).suffix else executable.lower()
    return 'podman' in resolved


def pick_podman_machine(executable: str, machine_name: str | None = None) -> dict[str, Any]:
    machines = run_json_command([executable, 'machine', 'list', '--format', 'json'], timeout_seconds=30.0) or []
    if not machines:
        raise RuntimeError('no podman machine is configured')
    if machine_name:
        selected = next((item for item in machines if str(item.get('Name')) == machine_name), None)
        if selected is None:
            raise RuntimeError(f'podman machine {machine_name} is not configured')
        return dict(selected)
    return dict(next((item for item in machines if item.get('Default')), machines[0]))


def ensure_podman_machine_running(executable: str) -> None:
    if not is_podman_command(executable) or not command_exists(executable):
        return
    try:
        selected = pick_podman_machine(executable)
    except Exception:
        return
    if selected.get('Running'):
        return
    name = str(selected['Name'])
    result = run_subprocess([executable, 'machine', 'start', name], timeout_seconds=180.0)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'failed to start podman machine {name}')


def podman_machine_ssh_details(executable: str, machine_name: str) -> dict[str, Any]:
    selected = pick_podman_machine(executable, machine_name)
    ssh_port = int(selected.get('Port') or 0)
    ssh_user = str(selected.get('RemoteUsername') or 'user')
    ssh_key = str(selected.get('IdentityPath') or '')
    if not ssh_port:
        inspect = run_json_command([executable, 'machine', 'inspect', machine_name], timeout_seconds=30.0)
        machine = inspect[0] if isinstance(inspect, list) and inspect else inspect
        ssh_config = dict(machine.get('SSHConfig', {}))
        ssh_port = int(ssh_config.get('Port') or 0)
        ssh_user = str(ssh_config.get('RemoteUsername') or ssh_user)
        ssh_key = str(ssh_config.get('IdentityPath') or ssh_key)
    if not ssh_port:
        raise RuntimeError(f'podman machine {machine_name} did not expose an SSH port')
    if ssh_key and not Path(ssh_key).exists():
        raise RuntimeError(f'podman machine identity is missing: {ssh_key}')
    return {
        'machine_name': str(selected.get('Name') or machine_name),
        'ssh_port': ssh_port,
        'ssh_user': ssh_user,
        'ssh_private_key': ssh_key,
    }
