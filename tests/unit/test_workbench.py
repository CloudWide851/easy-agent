from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from agent_integrations.sandbox import SandboxManager, SandboxMode, SandboxTarget
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager


def _manager(tmp_path: Path) -> tuple[SQLiteRunStore, WorkbenchManager]:
    store = SQLiteRunStore(tmp_path, 'state.db')
    sandbox = SandboxManager(
        mode=SandboxMode.PROCESS,
        targets=[SandboxTarget.COMMAND_SKILL, SandboxTarget.STDIO_MCP],
        env_allowlist=['PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP'],
        working_root=tmp_path,
    )
    manager = WorkbenchManager(store, sandbox, tmp_path / 'workbench', session_ttl_seconds=1)
    return store, manager



def test_workbench_clone_manifest_copies_files(tmp_path: Path) -> None:
    store, manager = _manager(tmp_path)
    original = manager.ensure_session('run-a', 'skill-echo')
    artifact = original.root_path / 'artifact.txt'
    artifact.write_text('hello workbench', encoding='utf-8')

    cloned = manager.clone_manifest('run-b', manager.snapshot_manifest('run-a'))
    cloned_session = manager.load_session(cloned['sessions'][0]['session_id'])

    assert (cloned_session.root_path / 'artifact.txt').read_text(encoding='utf-8') == 'hello workbench'
    assert cloned_session.branch_parent_session_id == original.session_id
    assert store.load_workbench_session_by_owner('run-b', 'skill-echo') is not None



def test_workbench_run_command_records_execution(tmp_path: Path) -> None:
    store, manager = _manager(tmp_path)
    session = manager.ensure_session('run-c', 'skill-cmd')

    result = manager.run_command(
        session.session_id,
        [sys.executable, '-c', "print('ok')"],
        env={},
        timeout_seconds=5,
        target=SandboxTarget.COMMAND_SKILL,
    )

    assert result.stdout == 'ok'
    with sqlite3.connect(tmp_path / 'state.db') as connection:
        count = connection.execute('SELECT COUNT(*) FROM workbench_executions').fetchone()[0]
    assert count == 1



def test_workbench_gc_expires_sessions(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    session = manager.ensure_session('run-d', 'skill-expire')
    removed_initial = manager.gc_expired()
    assert removed_initial == []
    manager.store.touch_workbench_session(session.session_id, '1970-01-01T00:00:00+00:00')

    removed = manager.gc_expired()

    assert session.session_id in removed
    assert not session.root_path.exists()
