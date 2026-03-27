from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_integrations.sandbox import (
    PreparedSubprocess,
    SandboxManager,
    SandboxRequest,
    SandboxResult,
    SandboxTarget,
)
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
    branch_parent_session_id: str | None = None
    expires_at: str | None = None


class WorkbenchManager:
    def __init__(
        self,
        store: SQLiteRunStore,
        sandbox_manager: SandboxManager,
        base_root: Path,
        *,
        default_executor: str = 'process',
        session_ttl_seconds: int = 3600,
    ) -> None:
        self.store = store
        self.sandbox_manager = sandbox_manager
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
        }

    def ensure_session(
        self,
        owner_run_id: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
        seed_session_id: str | None = None,
    ) -> WorkbenchSession:
        existing = self.store.load_workbench_session_by_owner(owner_run_id, name)
        if existing is not None and existing['status'] == 'active':
            self.store.touch_workbench_session(existing['session_id'], self._expires_at())
            return self._row_to_session(existing)
        session_id = uuid.uuid4().hex
        root_path = self.base_root / session_id
        root_path.mkdir(parents=True, exist_ok=True)
        if seed_session_id is not None:
            source = self.load_session(seed_session_id)
            self._copy_root(source.root_path, root_path)
        payload = metadata or {}
        self.store.create_workbench_session(
            session_id=session_id,
            owner_run_id=owner_run_id,
            name=name,
            root_path=str(root_path),
            executor_name=self.default_executor,
            metadata=payload,
            expires_at=self._expires_at(),
            branch_parent_session_id=seed_session_id,
        )
        return self.load_session(session_id)

    def load_session(self, session_id: str) -> WorkbenchSession:
        row = self.store.load_workbench_session(session_id)
        return self._row_to_session(row)

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
        session = self.load_session(session_id)
        self.store.touch_workbench_session(session_id, self._expires_at())
        return self.sandbox_manager.prepare(
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
        session_id: str,
        command: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        target: SandboxTarget,
    ) -> SandboxResult:
        prepared = self.prepare_subprocess(
            session_id,
            command,
            env=env,
            timeout_seconds=timeout_seconds,
            target=target,
        )
        result = self.sandbox_manager.run(
            SandboxRequest(
                command=prepared.command,
                cwd=prepared.cwd,
                env=prepared.env,
                timeout_seconds=timeout_seconds,
                target=target,
            )
        )
        self.store.record_workbench_execution(
            session_id=session_id,
            command=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return result

    def snapshot_manifest(self, owner_run_id: str) -> dict[str, Any]:
        sessions = self.list_sessions(owner_run_id=owner_run_id)
        return {
            'sessions': [
                {
                    'session_id': item.session_id,
                    'name': item.name,
                    'root_path': str(item.root_path),
                    'executor_name': item.executor_name,
                    'metadata': item.metadata,
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
            )
            cloned.append(
                {
                    'session_id': session.session_id,
                    'name': session.name,
                    'root_path': str(session.root_path),
                    'executor_name': session.executor_name,
                    'metadata': session.metadata,
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
            if session.root_path.exists():
                shutil.rmtree(session.root_path, ignore_errors=True)
            self.store.update_workbench_session_status(session.session_id, 'expired')
            removed.append(session.session_id)
        return removed

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
            branch_parent_session_id=row.get('branch_parent_session_id'),
            expires_at=row.get('expires_at'),
        )
