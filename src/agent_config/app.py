from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from agent_common.models import (
    FederationAuthType,
    HumanLoopMode,
    McpAuthType,
    NodeType,
    Protocol,
    TeamMode,
)
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


class FunctionCallingConfig(BaseModel):
    strict: bool = True
    parallel_tool_calls: bool = True
    mode: Literal['auto', 'none', 'required', 'force'] = 'auto'
    forced_tool_name: str | None = None
    allowed_tool_names: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_mode(self) -> FunctionCallingConfig:
        if self.mode == 'force' and not self.forced_tool_name:
            raise ValueError('function_calling.force mode requires forced_tool_name')
        return self


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
    function_calling: FunctionCallingConfig = Field(default_factory=lambda: FunctionCallingConfig())


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
    optional: bool = False


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
    executor: str | None = None

    @model_validator(mode='after')
    def validate_transport(self) -> McpServerConfig:
        if self.transport == 'http_sse' and not self.rpc_url:
            raise ValueError('http_sse transport requires rpc_url')
        if self.transport == 'streamable_http' and not self.url:
            raise ValueError('streamable_http transport requires url')
        if self.transport not in {'stdio', 'http_sse', 'streamable_http'}:
            raise ValueError(f'Unsupported MCP transport: {self.transport}')
        return self


class FederationOAuthConfig(BaseModel):
    issuer_url: str | None = None
    openid_config_url: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    jwks_url: str | None = None
    metadata_url: str | None = None
    audience: str | None = None
    scopes: list[str] = Field(default_factory=list)
    grant_type: Literal['client_credentials', 'authorization_code'] = 'client_credentials'
    client_id: str | None = None
    client_id_env: str | None = None
    client_secret: str | None = None
    client_secret_env: str | None = None
    redirect_uri: str = 'urn:ietf:wg:oauth:2.0:oob'
    use_pkce: bool = True
    token_refresh_skew_seconds: int = 60
    allowed_algorithms: list[str] = Field(default_factory=lambda: ['RS256'])

    @property
    def resolved_client_id(self) -> str | None:
        return self.client_id

    @property
    def resolved_client_secret(self) -> str | None:
        return self.client_secret


class FederationServerJwtConfig(BaseModel):
    enabled: bool = False
    issuer: str | None = None
    audience: str | None = None
    algorithm: str = 'RS256'
    allowed_algorithms: list[str] = Field(default_factory=lambda: ['RS256'])
    key_id: str = 'easy-agent-default'
    private_key_path: str | None = None
    public_key_path: str | None = None
    jwks_path: str = '/.well-known/jwks.json'
    token_ttl_seconds: int = 600
    leeway_seconds: int = 30
    tenant_claim: str = 'tenant'
    subject_claim: str = 'sub'
    task_scope_claim: str = 'task_ids'

    @model_validator(mode='after')
    def validate_jwt(self) -> FederationServerJwtConfig:
        if self.enabled and not self.private_key_path:
            raise ValueError('federation server jwt requires private_key_path when enabled')
        return self


class FederationMtlsConfig(BaseModel):
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    server_name: str | None = None
    insecure_skip_verify: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.client_cert and self.client_key)


class FederationAuthConfig(BaseModel):
    type: FederationAuthType = FederationAuthType.NONE
    token_env: str | None = None
    header_env: str | None = None
    header_name: str = 'Authorization'
    value_prefix: str = 'Bearer '
    oauth: FederationOAuthConfig = Field(default_factory=FederationOAuthConfig)
    mtls: FederationMtlsConfig = Field(default_factory=FederationMtlsConfig)

    @model_validator(mode='after')
    def validate_auth(self) -> FederationAuthConfig:
        if self.type is FederationAuthType.BEARER_ENV and not self.token_env:
            raise ValueError('bearer_env auth requires token_env')
        if self.type is FederationAuthType.HEADER_ENV and not self.header_env:
            raise ValueError('header_env auth requires header_env')
        if self.type in {FederationAuthType.OAUTH, FederationAuthType.OIDC}:
            if self.token_env or self.header_env:
                return self
            if not (self.oauth.client_id or self.oauth.client_id_env):
                raise ValueError('oauth/oidc federation auth requires client_id or client_id_env when env headers are not used')
            if self.oauth.grant_type == 'client_credentials' and not (
                self.oauth.client_secret or self.oauth.client_secret_env
            ):
                raise ValueError('client_credentials federation auth requires client_secret or client_secret_env')
        if self.type is FederationAuthType.MTLS and not self.mtls.enabled:
            raise ValueError('mtls federation auth requires client_cert and client_key')
        return self


class FederationSecuritySchemeConfig(BaseModel):
    name: str
    type: Literal['none', 'bearer', 'header', 'oauth2', 'oidc', 'mtls'] = 'none'
    description: str = ''
    header_name: str = 'Authorization'
    bearer_format: str | None = None
    openid_config_url: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: dict[str, str] = Field(default_factory=dict)
    audience: str | None = None

    @model_validator(mode='after')
    def validate_scheme(self) -> FederationSecuritySchemeConfig:
        if self.type == 'header' and not self.header_name:
            raise ValueError('header security schemes require header_name')
        if self.type == 'oauth2' and not self.token_url:
            raise ValueError('oauth2 security schemes require token_url')
        if self.type == 'oidc' and not self.openid_config_url:
            raise ValueError('oidc security schemes require openid_config_url')
        return self


class FederationPushSecurityConfig(BaseModel):
    callback_url_policy: Literal['compat', 'public_only', 'allowlist'] = 'compat'
    callback_allowlist_hosts: list[str] = Field(default_factory=list)
    token_env: str | None = None
    token_header: str = 'X-A2A-Notification-Token'
    signature_secret_env: str | None = None
    signature_header: str = 'X-A2A-Signature'
    signature_algorithm: str = 'hmac-sha256'
    timestamp_header: str = 'X-A2A-Timestamp'
    audience: str | None = None
    audience_header: str = 'X-A2A-Callback-Audience'
    require_signature: bool = False
    require_audience: bool = False
    timestamp_tolerance_seconds: int = 300
    jws_enabled: bool = False
    jws_header: str = 'X-A2A-JWS'
    jwks_url: str | None = None
    allowed_algorithms: list[str] = Field(default_factory=lambda: ['RS256'])

    @model_validator(mode='after')
    def validate_push_security(self) -> FederationPushSecurityConfig:
        if self.callback_url_policy == 'allowlist' and not self.callback_allowlist_hosts:
            raise ValueError('allowlist callback policy requires callback_allowlist_hosts')
        if self.require_signature and not self.signature_secret_env:
            raise ValueError('push signature requires signature_secret_env')
        if self.require_audience and not self.audience:
            raise ValueError('push audience validation requires audience')
        if self.jws_enabled and not self.jwks_url:
            raise ValueError('push JWS verification requires jwks_url')
        return self


class FederationRemoteConfig(BaseModel):
    name: str
    base_url: str
    discovery_url: str | None = None
    auth: FederationAuthConfig = Field(default_factory=FederationAuthConfig)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    poll_seconds: float = 0.2
    push_preference: Literal['auto', 'sse', 'poll'] = 'auto'
    sse_reconnect_seconds: float = 0.5


class FederationExportConfig(BaseModel):
    name: str
    target_type: Literal['agent', 'team', 'harness']
    target: str
    description: str = ''
    tags: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ['text'])
    output_modes: list[str] = Field(default_factory=lambda: ['text'])
    default_input_modes: list[str] = Field(default_factory=list)
    default_output_modes: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=lambda: ['text'])
    capabilities: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    parts: list[dict[str, Any]] = Field(default_factory=list)
    notification_compatibility: dict[str, Any] = Field(default_factory=dict)


class FederationServerConfig(BaseModel):
    enabled: bool = False
    host: str = '127.0.0.1'
    port: int = 8787
    base_path: str = '/a2a'
    public_url: str | None = None
    protocol_version: str = '0.3'
    card_schema_version: str = '1.0'
    subscription_lease_seconds: int = 300
    retry_max_attempts: int = 4
    retry_initial_backoff_seconds: float = 0.5
    retry_backoff_multiplier: float = 2.0
    well_known_path: str = '/.well-known/agent-card.json'
    legacy_well_known_path: str = '/.well-known/agent.json'
    security_schemes: list[FederationSecuritySchemeConfig] = Field(default_factory=list)
    security_requirements: list[dict[str, list[str]]] = Field(default_factory=list)
    push_security: FederationPushSecurityConfig = Field(default_factory=FederationPushSecurityConfig)
    jwt: FederationServerJwtConfig = Field(default_factory=FederationServerJwtConfig)


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

    @property
    def security_scheme_map(self) -> dict[str, FederationSecuritySchemeConfig]:
        return {item.name: item for item in self.server.security_schemes}


class ContainerExecutorOptions(BaseModel):
    executable: str = 'podman'
    image: str = 'docker.io/library/python:3.12-slim'
    workdir: str = '/workspace'
    keepalive_command: list[str] = Field(default_factory=lambda: ['sleep', 'infinity'])
    run_args: list[str] = Field(default_factory=list)
    exec_args: list[str] = Field(default_factory=list)
    bootstrap_context: str | None = None
    bootstrap_containerfile: str | None = None
    image_archive: str | None = None
    auto_build: bool = True
    auto_load: bool = True
    checkpoint_enabled: bool = False
    memory_mb: int | None = None
    cpus: float | None = None


class MicrovmExecutorOptions(BaseModel):
    provider: Literal['qemu', 'podman_machine'] = 'qemu'
    executable: str = 'qemu-system-x86_64'
    base_image: str | None = None
    ssh_user: str = 'agent'
    ssh_private_key: str | None = None
    guest_workdir: str = '/workspace'
    ssh_port_base: int = 22000
    memory_mb: int = 1024
    cpus: int = 1
    extra_args: list[str] = Field(default_factory=list)
    machine_name: str = 'podman-machine-default'
    checkpoint_enabled: bool = False
    disk_size_gb: int | None = None


class ExecutorConfig(BaseModel):
    name: str = 'process'
    kind: Literal['process', 'container', 'microvm'] = 'process'
    default_timeout_seconds: float = 30.0
    container: ContainerExecutorOptions | None = None
    microvm: MicrovmExecutorOptions | None = None

    @model_validator(mode='after')
    def validate_executor(self) -> ExecutorConfig:
        if self.kind == 'container' and self.container is None:
            raise ValueError(f"executor '{self.name}' requires container options")
        if self.kind == 'microvm' and self.microvm is None:
            raise ValueError(f"executor '{self.name}' requires microvm options")
        return self


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


class PublicEvalWebSearchConfig(BaseModel):
    provider: Literal['serpapi', 'replay_only'] = 'serpapi'
    endpoint_url: str = 'https://serpapi.com/search.json'
    api_key_env: str = 'SERPAPI_API_KEY'
    engine: str = 'google'
    google_domain: str = 'google.com'
    hl: str = 'en'
    gl: str = 'us'
    timeout_seconds: float = 20.0
    hourly_limit: int = 50
    daily_limit: int = 300
    usage_path: str = '.easy-agent/public-eval-web-search-usage.json'
    quota_policy: Literal['resume_later', 'replay', 'fail'] = 'resume_later'


class PublicEvalOfficialDatasetConfig(BaseModel):
    cache_dir: str = '.easy-agent/public-eval-cache'
    manifest_path: str = '.easy-agent/public-eval-cache/bfcl_v4_manifest.json'
    source_url: str | None = None
    checkpoint_path: str = '.easy-agent/public-eval-progress.json'
    resume: bool = True
    suite_allowlist: list[str] = Field(default_factory=list)
    case_allowlist: list[str] = Field(default_factory=list)
    max_cases: int | None = None


class PublicEvalConfig(BaseModel):
    profile: Literal['subset', 'full_v4', 'official_full_v4'] = 'full_v4'
    bfcl_version: str = 'v4'
    enable_full_bfcl: bool = True
    web_search: PublicEvalWebSearchConfig = Field(default_factory=PublicEvalWebSearchConfig)
    official_dataset: PublicEvalOfficialDatasetConfig = Field(default_factory=PublicEvalOfficialDatasetConfig)


class RealNetworkLatencyBudgetConfig(BaseModel):
    container_warm_start_seconds: float = 35.0
    microvm_warm_start_seconds: float = 25.0
    container_snapshot_restore_seconds: float = 8.0
    microvm_snapshot_restore_seconds: float = 12.0


class RealNetworkEvalConfig(BaseModel):
    history_path: str = '.easy-agent/real-network-history.jsonl'
    latency_budgets: RealNetworkLatencyBudgetConfig = Field(
        default_factory=RealNetworkLatencyBudgetConfig
    )
    snapshot_drift_ratio_limit: float = 0.5


class EvaluationConfig(BaseModel):
    public_eval: PublicEvalConfig = Field(default_factory=PublicEvalConfig)
    real_network: RealNetworkEvalConfig = Field(default_factory=RealNetworkEvalConfig)


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
            'USERPROFILE',
            'HOME',
            'APPDATA',
            'LOCALAPPDATA',
            'USERNAME',
            'HOMEDRIVE',
            'HOMEPATH',
            'PROGRAMDATA',
            'PUBLIC',
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
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
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
        for server in self.mcp:
            if server.executor is not None and server.executor not in self.executor_map:
                raise ValueError(f"mcp server '{server.name}' references unknown executor '{server.executor}'")
        return self

    @model_validator(mode='after')
    def validate_federation(self) -> AppConfig:
        remote_names = [remote.name for remote in self.federation.remotes]
        export_names = [export.name for export in self.federation.exports]
        security_scheme_names = [item.name for item in self.federation.server.security_schemes]
        if len(remote_names) != len(set(remote_names)):
            raise ValueError('federation remote names must be unique')
        if len(export_names) != len(set(export_names)):
            raise ValueError('federation export names must be unique')
        if len(security_scheme_names) != len(set(security_scheme_names)):
            raise ValueError('federation security scheme names must be unique')
        for export in self.federation.exports:
            if export.target_type == 'agent' and export.target not in self.agent_map:
                raise ValueError(f"federation export '{export.name}' references unknown agent '{export.target}'")
            if export.target_type == 'team' and export.target not in self.team_map:
                raise ValueError(f"federation export '{export.name}' references unknown team '{export.target}'")
            if export.target_type == 'harness' and export.target not in self.harness_map:
                raise ValueError(f"federation export '{export.name}' references unknown harness '{export.target}'")
        for requirement in self.federation.server.security_requirements:
            for scheme_name in requirement:
                if scheme_name not in self.federation.security_scheme_map:
                    raise ValueError(f"federation security requirement references unknown scheme '{scheme_name}'")
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

