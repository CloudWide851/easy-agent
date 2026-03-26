from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from agent_common.models import NodeType, Protocol, TeamMode
from agent_integrations.sandbox import SandboxMode, SandboxTarget

_LOADED_ENV_FILES: set[Path] = set()


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export ') :].strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = os.path.expandvars(value)
    return values


def load_local_env(config_path: str | Path | None = None) -> None:
    candidates: list[Path] = [Path.cwd() / '.env.local']
    if config_path is not None:
        resolved = Path(config_path).resolve()
        candidates.append(resolved.parent / '.env.local')
        candidates.append(resolved.parent / f'.env.{resolved.stem}.local')

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or resolved in _LOADED_ENV_FILES or not resolved.is_file():
            continue
        for key, value in _parse_env_file(resolved).items():
            os.environ.setdefault(key, value)
        _LOADED_ENV_FILES.add(resolved)
        seen.add(resolved)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


class ModelConfig(BaseModel):
    provider: str = 'deepseek'
    protocol: Protocol = Protocol.AUTO
    model: str = 'deepseek-chat'
    base_url: str = 'https://api.deepseek.com'
    api_key_env: str = 'DEEPSEEK_API_KEY'
    timeout_seconds: float = 60.0
    max_tokens: int = 2048
    temperature: float = 0.1
    extra_headers: dict[str, str] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    name: str
    description: str = ''
    system_prompt: str = ''
    tools: list[str] = Field(default_factory=list)
    sub_agents: list[str] = Field(default_factory=list)
    max_iterations: int = 6


class TeamConfig(BaseModel):
    name: str
    mode: TeamMode
    members: list[str] = Field(default_factory=list)
    max_turns: int = 8
    termination_text: str = 'TERMINATE'
    allow_repeated_speaker: bool = False
    selector_prompt: str | None = None


class GraphNodeConfig(BaseModel):
    id: str
    type: NodeType
    target: str | None = None
    deps: list[str] = Field(default_factory=list)
    input_template: str = '{input}'
    retries: int = 0
    timeout_seconds: float = 30.0
    arguments: dict[str, Any] = Field(default_factory=dict)


class GraphConfig(BaseModel):
    name: str = 'default'
    entrypoint: str
    agents: list[AgentConfig] = Field(default_factory=list)
    teams: list[TeamConfig] = Field(default_factory=list)
    nodes: list[GraphNodeConfig] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_graph(self) -> GraphConfig:
        node_ids = {node.id for node in self.nodes}
        agent_names = [agent.name for agent in self.agents]
        team_names = [team.name for team in self.teams]
        all_names = agent_names + team_names + list(node_ids)
        if len(all_names) != len(set(all_names)):
            raise ValueError('agent names, team names, and node ids must be unique')
        agent_name_set = set(agent_names)
        for team in self.teams:
            if not team.members:
                raise ValueError(f"team '{team.name}' must declare at least one member")
            for member in team.members:
                if member not in agent_name_set:
                    raise ValueError(f"team '{team.name}' references unknown member '{member}'")
            if team.mode in (TeamMode.SELECTOR, TeamMode.SWARM):
                missing = [agent.name for agent in self.agents if agent.name in team.members and not agent.description.strip()]
                if missing:
                    joined = ', '.join(sorted(missing))
                    raise ValueError(
                        f"team '{team.name}' requires non-empty agent descriptions for selector/swarm members: {joined}"
                    )
        valid_entrypoints = node_ids | agent_name_set | set(team_names)
        if self.entrypoint not in valid_entrypoints:
            raise ValueError('graph.entrypoint must match a node id, agent name, or team name')
        return self


class SkillSourceConfig(BaseModel):
    path: str


class McpServerConfig(BaseModel):
    name: str
    transport: str
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    rpc_url: str | None = None
    sse_url: str | None = None
    timeout_seconds: float = 15.0


class StorageConfig(BaseModel):
    path: str = '.easy-agent'
    database: str = 'state.db'


class LoggingConfig(BaseModel):
    level: str = 'INFO'


class GuardrailConfig(BaseModel):
    tool_input_hooks: list[str] = Field(default_factory=lambda: ['block_shell_metacharacters'])
    final_output_hooks: list[str] = Field(
        default_factory=lambda: ['require_non_empty_output', 'block_secret_leaks']
    )


class ObservabilityConfig(BaseModel):
    enable_event_stream: bool = True
    stream_format: Literal['pretty', 'ndjson'] = 'pretty'


class SandboxConfig(BaseModel):
    mode: SandboxMode = SandboxMode.AUTO
    targets: list[SandboxTarget] = Field(
        default_factory=lambda: [SandboxTarget.COMMAND_SKILL, SandboxTarget.STDIO_MCP]
    )
    env_allowlist: list[str] = Field(
        default_factory=lambda: [
            'PATH',
            'PATHEXT',
            'SYSTEMROOT',
            'WINDIR',
            'COMSPEC',
            'TEMP',
            'TMP',
            'DEEPSEEK_API_KEY',
        ]
    )
    working_root: str | None = None
    windows_sandbox_fallback: SandboxMode = SandboxMode.PROCESS


class SecurityConfig(BaseModel):
    allowed_commands: list[list[str]] = Field(default_factory=list)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


class AppConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    graph: GraphConfig
    plugins: list[str] = Field(default_factory=list)
    skills: list[SkillSourceConfig] = Field(default_factory=list)
    mcp: list[McpServerConfig] = Field(default_factory=list)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @property
    def agent_map(self) -> dict[str, AgentConfig]:
        return {agent.name: agent for agent in self.graph.agents}

    @property
    def team_map(self) -> dict[str, TeamConfig]:
        return {team.name: team for team in self.graph.teams}


def load_config(path: str | Path) -> AppConfig:
    load_local_env(path)
    config_path = Path(path)
    with config_path.open('r', encoding='utf-8') as handle:
        raw = yaml.safe_load(handle) or {}
    expanded = _expand_env(raw)
    graph = expanded.setdefault('graph', {})
    graph.setdefault('teams', [])
    return AppConfig.model_validate(expanded)


load_local_env()
