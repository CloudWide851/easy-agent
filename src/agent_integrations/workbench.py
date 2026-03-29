from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_integrations.executors import ExecutorBackend, ExecutorSession
from agent_integrations.sandbox import PreparedSubprocess, SandboxResult, SandboxTarget
from agent_integrations.storage import SQLiteRunStore


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class WorkbenchSession:
    session_id: str
    owner_run_id: str
    name: str
    root_path: Path
    executor_name: str
    status: str
    metadata: dict[str, Any]
    runtime_state: dict[str, Any]
    branch_parent_session_id: str | None = None
    expires_at: str | None = None


class WorkbenchManager:
    def __init__(
        self,
        store: SQLiteRunStore,
        executors: dict[str, ExecutorBackend],
        base_root: Path,
        *,
        default_executor: str = 'process',
        session_ttl_seconds: int = 3600,
    ) -> None:
        self.store = store
        self.executors = executors
        self.base_root = base_root.resolve()
        self.base_root.mkdir(parents=True, exist_ok=True)
        self.default_executor = default_executor
        self.session_ttl_seconds = session_ttl_seconds

    def describe(self) -> dict[str, Any]:
        return {
            'base_root': str(self.base_root),
            'default_executor': self.default_executor,
            'session_ttl_seconds': self.session_ttl_seconds,
            'active_sessions': len(self.list_sessions()),
            'executors': {name: backend.describe() for name, backend in self.executors.items()},
        }

    def ensure_session(
        self,
        owner_run_id: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
        seed_session_id: str | None = None,
        executor_name: str | None = None,
    ) -> WorkbenchSession:
        existing = self.store.load_workbench_session_by_owner(owner_run_id, name)
        if existing is not None and existing['status'] == 'active':
            session = self._row_to_session(existing)
            session = self._ensure_executor_state(session)
            self.store.touch_workbench_session(session.session_id, self._expires_at(), runtime_state=session.runtime_state)
            return session
        resolved_executor = executor_name or self.default_executor
        session_id = uuid.uuid4().hex
        root_path = self.base_root / session_id
        root_path.mkdir(parents=True, exist_ok=True)
        if seed_session_id is not None:
            source = self.load_session(seed_session_id)
            self.sync_session(source.session_id)
            self._copy_root(source.root_path, root_path)
        payload = metadata or {}
        self.store.create_workbench_session(
            session_id=session_id,
            owner_run_id=owner_run_id,
            name=name,
            root_path=str(root_path),
            executor_name=resolved_executor,
            metadata=payload,
            runtime_state={},
            expires_at=self._expires_at(),
            branch_parent_session_id=seed_session_id,
        )
        return self._ensure_executor_state(self.load_session(session_id))

    def load_session(self, session_id: str) -> WorkbenchSession:
        return self._row_to_session(self.store.load_workbench_session(session_id))

    def list_sessions(self, owner_run_id: str | None = None) -> list[WorkbenchSession]:
        return [self._row_to_session(item) for item in self.store.list_workbench_sessions(owner_run_id=owner_run_id)]

    def prepare_subprocess(
        self,
        session_id: str,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> PreparedSubprocess:
        session = self._ensure_executor_state(self.load_session(session_id))
        prepared = self._backend(session).prepare_command(
            self._executor_session(session),
            command,
            env=env,
            timeout_seconds=timeout_seconds,
            target=target,
        )
        self.store.touch_workbench_session(session_id, self._expires_at(), runtime_state=session.runtime_state)
        return prepared

    def run_command(
        self,
        session_id: str,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> SandboxResult:
        session = self._ensure_executor_state(self.load_session(session_id))
        result = self._backend(session).run_command(
            self._executor_session(session),
            command,
            env=env,
            timeout_seconds=timeout_seconds,
            target=target,
        )
        self.store.record_workbench_execution(
            session_id=session_id,
            command=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        self.store.touch_workbench_session(session_id, self._expires_at(), runtime_state=session.runtime_state)
        return result

    def sync_session(self, session_id: str) -> WorkbenchSession:
        session = self.load_session(session_id)
        runtime_state = self._backend(session).sync_to_host(self._executor_session(session))
        session.runtime_state = runtime_state
        self.store.touch_workbench_session(session_id, self._expires_at(), runtime_state=runtime_state)
        return session

    def shutdown_session(self, session_id: str) -> WorkbenchSession:
        session = self.load_session(session_id)
        runtime_state = self._backend(session).shutdown_session(self._executor_session(session))
        session.runtime_state = runtime_state
        self.store.touch_workbench_session(session_id, self._expires_at(), runtime_state=runtime_state)
        return session

    def restart_session(self, session_id: str) -> WorkbenchSession:
        self.shutdown_session(session_id)
        return self._ensure_executor_state(self.load_session(session_id))

    def snapshot_manifest(self, owner_run_id: str) -> dict[str, Any]:
        sessions = [self.sync_session(item.session_id) for item in self.list_sessions(owner_run_id=owner_run_id)]
        return {
            'sessions': [
                {
                    'session_id': item.session_id,
                    'name': item.name,
                    'root_path': str(item.root_path),
                    'executor_name': item.executor_name,
                    'metadata': item.metadata,
                    'runtime_state': item.runtime_state,
                    'branch_parent_session_id': item.branch_parent_session_id,
                }
                for item in sessions
            ]
        }

    def clone_manifest(self, owner_run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        cloned: list[dict[str, Any]] = []
        for item in manifest.get('sessions', []):
            session = self.ensure_session(
                owner_run_id,
                str(item['name']),
                metadata=dict(item.get('metadata', {})),
                seed_session_id=str(item['session_id']),
                executor_name=str(item.get('executor_name') or self.default_executor),
            )
            cloned.append(
                {
                    'session_id': session.session_id,
                    'name': session.name,
                    'root_path': str(session.root_path),
                    'executor_name': session.executor_name,
                    'metadata': session.metadata,
                    'runtime_state': session.runtime_state,
                    'branch_parent_session_id': session.branch_parent_session_id,
                }
            )
        return {'sessions': cloned}

    def gc_expired(self) -> list[str]:
        removed: list[str] = []
        now = _now().isoformat()
        for item in self.store.list_workbench_sessions():
            expires_at = item.get('expires_at')
            if expires_at is None or expires_at > now:
                continue
            session = self._row_to_session(item)
            runtime_state = self._backend(session).shutdown_session(self._executor_session(session))
            if session.root_path.exists():
                shutil.rmtree(session.root_path, ignore_errors=True)
            self.store.update_workbench_session_status(session.session_id, 'expired', runtime_state=runtime_state)
            removed.append(session.session_id)
        return removed

    def _ensure_executor_state(self, session: WorkbenchSession) -> WorkbenchSession:
        runtime_state = self._backend(session).ensure_session(self._executor_session(session))
        session.runtime_state = runtime_state
        self.store.touch_workbench_session(session.session_id, self._expires_at(), runtime_state=runtime_state)
        return session

    def _backend(self, session: WorkbenchSession) -> ExecutorBackend:
        return self.executors[session.executor_name]

    @staticmethod
    def _executor_session(session: WorkbenchSession) -> ExecutorSession:
        return ExecutorSession(
            session_id=session.session_id,
            root_path=session.root_path,
            executor_name=session.executor_name,
            runtime_state=session.runtime_state,
        )

    def _expires_at(self) -> str:
        return (_now() + timedelta(seconds=self.session_ttl_seconds)).isoformat()

    @staticmethod
    def _copy_root(source: Path, destination: Path) -> None:
        for child in source.iterdir() if source.exists() else []:
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)

    @staticmethod
    def _row_to_session(row: dict[str, Any]) -> WorkbenchSession:
        return WorkbenchSession(
            session_id=str(row['session_id']),
            owner_run_id=str(row['owner_run_id']),
            name=str(row['name']),
            root_path=Path(str(row['root_path'])).resolve(),
            executor_name=str(row['executor_name']),
            status=str(row['status']),
            metadata=dict(row.get('metadata', {})),
            runtime_state=dict(row.get('runtime_state', {})),
            branch_parent_session_id=row.get('branch_parent_session_id'),
            expires_at=row.get('expires_at'),
        )
