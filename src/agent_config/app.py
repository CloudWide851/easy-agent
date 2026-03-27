from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from agent_common.models import HumanLoopMode, McpAuthType, NodeType, Protocol, TeamMode
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


class HarnessConfig(BaseModel):
    name: str
    initializer_agent: str
    worker_target: str
    evaluator_agent: str
    completion_contract: str
    artifacts_dir: str
    max_cycles: int = 8
    max_replans: int = 2


class SkillSourceConfig(BaseModel):
    path: str


class McpRootConfig(BaseModel):
    path: str
    name: str | None = None


class McpAuthConfig(BaseModel):
    type: McpAuthType = McpAuthType.NONE
    token_env: str | None = None
    header_env: str | None = None
    header_name: str = 'Authorization'
    value_prefix: str = 'Bearer '
    client_name: str = 'easy-agent'
    redirect_uri: str = 'urn:ietf:wg:oauth:2.0:oob'
    scopes: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_auth(self) -> McpAuthConfig:
        if self.type is McpAuthType.BEARER_ENV and not self.token_env:
            raise ValueError('bearer_env auth requires token_env')
        if self.type is McpAuthType.HEADER_ENV and not self.header_env:
            raise ValueError('header_env auth requires header_env')
        return self


class McpServerConfig(BaseModel):
    name: str
    transport: str
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    rpc_url: str | None = None
    sse_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    roots: list[McpRootConfig] = Field(default_factory=list)
    auth: McpAuthConfig = Field(default_factory=McpAuthConfig)
    timeout_seconds: float = 15.0

    @model_validator(mode='after')
    def validate_transport(self) -> McpServerConfig:
        if self.transport == 'http_sse' and not self.rpc_url:
            raise ValueError('http_sse transport requires rpc_url')
        if self.transport == 'streamable_http' and not self.url:
            raise ValueError('streamable_http transport requires url')
        if self.transport not in {'stdio', 'http_sse', 'streamable_http'}:
            raise ValueError(f'Unsupported MCP transport: {self.transport}')
        return self


class FederationAuthConfig(BaseModel):
    type: McpAuthType = McpAuthType.NONE
    token_env: str | None = None
    header_env: str | None = None
    header_name: str = 'Authorization'
    value_prefix: str = 'Bearer '

    @model_validator(mode='after')
    def validate_auth(self) -> FederationAuthConfig:
        if self.type is McpAuthType.OAUTH:
            raise ValueError('federation auth does not support oauth yet')
        if self.type is McpAuthType.BEARER_ENV and not self.token_env:
            raise ValueError('bearer_env auth requires token_env')
        if self.type is McpAuthType.HEADER_ENV and not self.header_env:
            raise ValueError('header_env auth requires header_env')
        return self


class FederationRemoteConfig(BaseModel):
    name: str
    base_url: str
    auth: FederationAuthConfig = Field(default_factory=FederationAuthConfig)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    poll_seconds: float = 0.2


class FederationExportConfig(BaseModel):
    name: str
    target_type: Literal['agent', 'team', 'harness']
    target: str
    description: str = ''
    tags: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ['text'])
    output_modes: list[str] = Field(default_factory=lambda: ['text'])


class FederationServerConfig(BaseModel):
    enabled: bool = False
    host: str = '127.0.0.1'
    port: int = 8787
    base_path: str = '/a2a'
    public_url: str | None = None


class FederationConfig(BaseModel):
    server: FederationServerConfig = Field(default_factory=FederationServerConfig)
    remotes: list[FederationRemoteConfig] = Field(default_factory=list)
    exports: list[FederationExportConfig] = Field(default_factory=list)

    @property
    def remote_map(self) -> dict[str, FederationRemoteConfig]:
        return {remote.name: remote for remote in self.remotes}

    @property
    def export_map(self) -> dict[str, FederationExportConfig]:
        return {item.name: item for item in self.exports}


class ExecutorConfig(BaseModel):
    name: str = 'process'
    kind: Literal['process'] = 'process'
    default_timeout_seconds: float = 30.0


class WorkbenchConfig(BaseModel):
    enabled: bool = True
    root: str = '.easy-agent/workbench'
    default_executor: str = 'process'
    session_ttl_seconds: int = 3600
    persistent_targets: list[SandboxTarget] = Field(
        default_factory=lambda: [SandboxTarget.COMMAND_SKILL, SandboxTarget.STDIO_MCP]
    )


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


class HumanLoopConfig(BaseModel):
    mode: HumanLoopMode = HumanLoopMode.HYBRID
    sensitive_tools: list[str] = Field(default_factory=list)
    approve_handoffs: bool = True
    approve_harness_resume: bool = True
    approve_mcp_sampling: bool = True
    approve_mcp_elicitation: bool = True
    interruptible: bool = True


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
    human_loop: HumanLoopConfig = Field(default_factory=HumanLoopConfig)


class AppConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    graph: GraphConfig
    harnesses: list[HarnessConfig] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    skills: list[SkillSourceConfig] = Field(default_factory=list)
    mcp: list[McpServerConfig] = Field(default_factory=list)
    federation: FederationConfig = Field(default_factory=FederationConfig)
    executors: list[ExecutorConfig] = Field(default_factory=lambda: [ExecutorConfig()])
    workbench: WorkbenchConfig = Field(default_factory=WorkbenchConfig)
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

    @property
    def harness_map(self) -> dict[str, HarnessConfig]:
        return {harness.name: harness for harness in self.harnesses}

    @property
    def mcp_map(self) -> dict[str, McpServerConfig]:
        return {server.name: server for server in self.mcp}

    @property
    def executor_map(self) -> dict[str, ExecutorConfig]:
        return {executor.name: executor for executor in self.executors}

    @property
    def federation_remote_map(self) -> dict[str, FederationRemoteConfig]:
        return {remote.name: remote for remote in self.federation.remotes}

    @property
    def federation_export_map(self) -> dict[str, FederationExportConfig]:
        return {item.name: item for item in self.federation.exports}

    @model_validator(mode='after')
    def validate_harnesses(self) -> AppConfig:
        harness_names = [harness.name for harness in self.harnesses]
        if len(harness_names) != len(set(harness_names)):
            raise ValueError('harness names must be unique')

        valid_workers = set(self.agent_map) | set(self.team_map)
        for harness in self.harnesses:
            if harness.initializer_agent not in self.agent_map:
                raise ValueError(
                    f"harness '{harness.name}' references unknown initializer_agent '{harness.initializer_agent}'"
                )
            if harness.evaluator_agent not in self.agent_map:
                raise ValueError(
                    f"harness '{harness.name}' references unknown evaluator_agent '{harness.evaluator_agent}'"
                )
            if harness.worker_target not in valid_workers:
                raise ValueError(
                    f"harness '{harness.name}' references unknown worker_target '{harness.worker_target}'"
                )
        return self

    @model_validator(mode='after')
    def validate_workbench(self) -> AppConfig:
        executor_names = [executor.name for executor in self.executors]
        if len(executor_names) != len(set(executor_names)):
            raise ValueError('executor names must be unique')
        if self.workbench.default_executor not in self.executor_map:
            raise ValueError('workbench.default_executor must reference a configured executor')
        return self

    @model_validator(mode='after')
    def validate_federation(self) -> AppConfig:
        remote_names = [remote.name for remote in self.federation.remotes]
        export_names = [export.name for export in self.federation.exports]
        if len(remote_names) != len(set(remote_names)):
            raise ValueError('federation remote names must be unique')
        if len(export_names) != len(set(export_names)):
            raise ValueError('federation export names must be unique')
        for export in self.federation.exports:
            if export.target_type == 'agent' and export.target not in self.agent_map:
                raise ValueError(f"federation export '{export.name}' references unknown agent '{export.target}'")
            if export.target_type == 'team' and export.target not in self.team_map:
                raise ValueError(f"federation export '{export.name}' references unknown team '{export.target}'")
            if export.target_type == 'harness' and export.target not in self.harness_map:
                raise ValueError(f"federation export '{export.name}' references unknown harness '{export.target}'")
        return self


def load_config(path: str | Path) -> AppConfig:
    load_local_env(path)
    config_path = Path(path)
    with config_path.open('r', encoding='utf-8') as handle:
        raw = yaml.safe_load(handle) or {}
    expanded = _expand_env(raw)
    graph = expanded.setdefault('graph', {})
    graph.setdefault('teams', [])
    expanded.setdefault('harnesses', [])
    expanded.setdefault('federation', {})
    expanded.setdefault('executors', [{'name': 'process', 'kind': 'process', 'default_timeout_seconds': 30.0}])
    expanded.setdefault('workbench', {})
    security = expanded.setdefault('security', {})
    security.setdefault('human_loop', {})
    return AppConfig.model_validate(expanded)


load_local_env()


