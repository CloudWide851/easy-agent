from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent_common.models import RunContext, ToolSpec
from agent_common.tools import ToolHandler, ToolRegistry
from agent_integrations.sandbox import SandboxManager, SandboxRequest, SandboxTarget
from agent_integrations.workbench import WorkbenchManager


class SkillMetadata(BaseModel):
    name: str
    description: str
    entry_type: str
    hook: str | None = None
    command: list[str] = Field(default_factory=list)
    args_template: list[str] = Field(default_factory=list)
    env_passthrough: list[str] = Field(default_factory=list)
    timeout_seconds: float = 15.0
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {'type': 'object', 'properties': {}, 'additionalProperties': True}
    )



def _token_allowed(tokens: list[str], allowed_prefixes: list[list[str]]) -> bool:
    return any(tokens[: len(prefix)] == prefix for prefix in allowed_prefixes)


class SkillLoader:
    def __init__(
        self,
        skill_paths: list[Path],
        allowed_commands: list[list[str]],
        sandbox_manager: SandboxManager,
        workbench_manager: WorkbenchManager | None = None,
    ) -> None:
        self.skill_paths = skill_paths
        self.allowed_commands = allowed_commands
        self.sandbox_manager = sandbox_manager
        self.workbench_manager = workbench_manager

    def discover(self) -> list[tuple[SkillMetadata, Path]]:
        discovered: list[tuple[SkillMetadata, Path]] = []
        for root in self.skill_paths:
            if root.is_file():
                continue
            if (root / 'skill.yaml').exists():
                candidates = [root]
            else:
                candidates = [path for path in root.iterdir() if path.is_dir()]
            for candidate in candidates:
                manifest_path = candidate / 'skill.yaml'
                if not manifest_path.exists():
                    continue
                with manifest_path.open('r', encoding='utf-8') as handle:
                    payload = yaml.safe_load(handle) or {}
                discovered.append((SkillMetadata.model_validate(payload), candidate))
        return discovered

    def register(self, registry: ToolRegistry) -> list[SkillMetadata]:
        registered: list[SkillMetadata] = []
        for metadata, base_path in self.discover():
            registry.register(
                ToolSpec(
                    name=metadata.name,
                    description=metadata.description,
                    input_schema=metadata.input_schema,
                ),
                self._make_handler(metadata, base_path),
            )
            registered.append(metadata)
        return registered

    def _make_handler(self, metadata: SkillMetadata, base_path: Path) -> ToolHandler:
        if metadata.entry_type == 'python':
            return self._python_handler(metadata, base_path)
        if metadata.entry_type == 'command':
            return self._command_handler(metadata, base_path)
        raise ValueError(f'Unsupported skill entry_type: {metadata.entry_type}')

    def _python_handler(self, metadata: SkillMetadata, base_path: Path) -> ToolHandler:
        if not metadata.hook:
            raise ValueError(f"Python skill '{metadata.name}' requires a hook")
        module_name, function_name = metadata.hook.split(':')
        module_path = base_path / module_name
        spec = importlib.util.spec_from_file_location(
            f'agent_skill_{metadata.name}',
            module_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f'Failed to load skill module: {module_path}')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = getattr(module, function_name)

        async def _run(arguments: dict[str, Any], context: RunContext) -> Any:
            result = handler(arguments, context)
            if inspect.isawaitable(result):
                return await result
            return result

        return _run

    def _command_handler(self, metadata: SkillMetadata, base_path: Path) -> ToolHandler:
        if not metadata.command:
            raise ValueError(f"Command skill '{metadata.name}' requires a command")

        def _run(arguments: dict[str, Any], context: RunContext) -> Any:
            rendered_args = [token.format(**arguments) for token in metadata.args_template]
            tokens = metadata.command + rendered_args
            if not _token_allowed(tokens, self.allowed_commands):
                raise PermissionError(f'Command is not allowed by whitelist: {tokens}')
            env = {key: os.environ[key] for key in metadata.env_passthrough if key in os.environ}
            if self.workbench_manager is not None:
                session = self.workbench_manager.ensure_session(
                    context.run_id,
                    f'skill-{metadata.name}',
                    metadata={'skill': metadata.name, 'base_path': str(base_path)},
                )
                result = self.workbench_manager.run_command(
                    session.session_id,
                    tokens,
                    env=env,
                    timeout_seconds=metadata.timeout_seconds,
                    target=SandboxTarget.COMMAND_SKILL,
                )
            else:
                result = self.sandbox_manager.run(
                    SandboxRequest(
                        command=tokens,
                        cwd=base_path,
                        env=env,
                        timeout_seconds=metadata.timeout_seconds,
                        target=SandboxTarget.COMMAND_SKILL,
                    )
                )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    tokens,
                    output=result.stdout,
                    stderr=result.stderr,
                )
            return {'stdout': result.stdout, 'stderr': result.stderr}

        return _run
