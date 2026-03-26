from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlparse

from agent_config.app import AppConfig, load_config
from agent_integrations.mcp import build_mcp_tool_name
from agent_runtime.runtime import build_runtime_from_config

AUDIT_TABLE = 'easy_agent_longrun_audit'


@dataclass(slots=True)
class LongRunRecord:
    mode: str
    cycle: int
    success: bool
    duration_seconds: float
    artifact_path: str
    redis_key: str
    run_key: str
    verification: dict[str, Any]
    result_summary: str
    error: str | None = None


@dataclass(slots=True)
class LongRunCase:
    mode: str
    config: AppConfig
    prompt_template: str


def _shared_payload(base: AppConfig) -> dict[str, Any]:
    return {
        'model': base.model.model_dump(),
        'plugins': list(base.plugins),
        'skills': [item.model_dump() for item in base.skills],
        'mcp': [item.model_dump() for item in base.mcp],
        'storage': base.storage.model_dump(),
        'logging': base.logging.model_dump(),
        'security': base.security.model_dump(),
    }


def _mcp_names() -> dict[str, str]:
    return {
        'fs_read': build_mcp_tool_name('filesystem', 'read_text_file'),
        'fs_info': build_mcp_tool_name('filesystem', 'get_file_info'),
        'redis_set': build_mcp_tool_name('redis', 'set'),
        'redis_get': build_mcp_tool_name('redis', 'get'),
        'pg_execute': build_mcp_tool_name('postgres', 'execute'),
        'pg_query': build_mcp_tool_name('postgres', 'query'),
    }


def build_longrun_cases(base_config: AppConfig) -> list[LongRunCase]:
    tools = _mcp_names()
    shared = _shared_payload(base_config)
    single_agent = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'longrun-single-agent',
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'system_prompt': (
                            'You are a precise agent. Follow the requested tool order exactly, use the provided '
                            'path strings verbatim, and keep the final response compact.'
                        ),
                        'tools': [
                            'html_page_builder',
                            tools['fs_read'],
                            tools['fs_info'],
                            tools['redis_set'],
                            tools['redis_get'],
                            tools['pg_execute'],
                            tools['pg_query'],
                        ],
                        'sub_agents': [],
                        'max_iterations': 10,
                    }
                ],
                'nodes': [],
            },
        }
    )
    sub_agent = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'longrun-sub-agent',
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'system_prompt': (
                            'Delegate exactly once to the builder sub-agent, then verify the produced state through '
                            'Redis, PostgreSQL, and the generated file. Use the provided path strings verbatim.'
                        ),
                        'tools': [tools['redis_get'], tools['fs_read']],
                        'sub_agents': ['builder'],
                        'max_iterations': 10,
                    },
                    {
                        'name': 'builder',
                        'system_prompt': (
                            'Create the HTML artifact with the marker visibly present, store the handoff summary in '
                            'Redis, and insert one PostgreSQL audit row.'
                        ),
                        'tools': ['html_page_builder', tools['redis_set'], tools['pg_execute']],
                        'sub_agents': [],
                        'max_iterations': 8,
                    },
                ],
                'nodes': [],
            },
        }
    )
    multi_agent_graph = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'longrun-multi-agent-graph',
                'entrypoint': 'aggregate',
                'agents': [
                    {
                        'name': 'planner',
                        'system_prompt': 'Create a compact build plan, mention the marker, and store it in Redis.',
                        'tools': [tools['redis_set']],
                        'sub_agents': [],
                        'max_iterations': 6,
                    },
                    {
                        'name': 'builder',
                        'system_prompt': (
                            'Read the plan from Redis and build the HTML artifact with the marker visible in both '
                            'prompt and sections.'
                        ),
                        'tools': [tools['redis_get'], 'html_page_builder'],
                        'sub_agents': [],
                        'max_iterations': 8,
                    },
                    {
                        'name': 'reviewer',
                        'system_prompt': 'Read the generated file and persist a review note to PostgreSQL.',
                        'tools': [tools['fs_read'], tools['pg_execute']],
                        'sub_agents': [],
                        'max_iterations': 8,
                    },
                ],
                'nodes': [
                    {
                        'id': 'plan',
                        'type': 'agent',
                        'target': 'planner',
                        'input_template': 'Use the run briefing below and persist the plan into Redis before replying.\n\n{input}',
                    },
                    {
                        'id': 'build',
                        'type': 'agent',
                        'target': 'builder',
                        'deps': ['plan'],
                        'input_template': 'Use the run briefing below, read the Redis plan, then build the site.\n\n{input}',
                    },
                    {
                        'id': 'review',
                        'type': 'agent',
                        'target': 'reviewer',
                        'deps': ['build'],
                        'input_template': 'Use the run briefing below, inspect the HTML file, and write a PostgreSQL review row.\n\n{input}',
                    },
                    {
                        'id': 'aggregate',
                        'type': 'join',
                        'deps': ['plan', 'build', 'review'],
                    },
                ],
            },
        }
    )
    return [
        LongRunCase(
            mode='single_agent',
            config=single_agent,
            prompt_template=(
                'Run marker: {marker}. Use the path exactly as shown, with forward slashes only: {artifact_ref}. '
                'Execute these steps in order. '
                '1. Call html_page_builder with output_path "{artifact_ref}", title "{title}", headline "{title}", '
                'prompt "{marker}", sections ["{marker}", "verification-ready"], and footer "{run_key}". '
                '2. Call {fs_read} with path "{artifact_ref}". '
                '3. Call {redis_set} with key "{redis_key}" and value "{marker}|{artifact_ref}". '
                '4. Call {redis_get} with key "{redis_key}". '
                "5. Call {pg_execute} with SQL \"INSERT INTO {audit_table} (run_key, scenario, cycle, artifact_path, note) "
                "VALUES ('{run_key}', 'single_agent', {cycle}, '{artifact_ref}', 'single-agent-ok:{marker}')\". "
                "6. Call {pg_query} with SQL \"SELECT run_key, scenario, cycle, artifact_path, note FROM {audit_table} "
                "WHERE run_key = '{run_key}'\". "
                'Return a compact verification summary.'
            ),
        ),
        LongRunCase(
            mode='sub_agent',
            config=sub_agent,
            prompt_template=(
                'Run marker: {marker}. Use the path exactly as shown, with forward slashes only: {artifact_ref}. '
                'Delegate exactly once to the builder sub-agent. Tell the builder to do all of the following: '
                'call html_page_builder with output_path "{artifact_ref}", title "Sub Agent Cycle {cycle}", '
                'headline "Sub Agent Cycle {cycle}", prompt "{marker}", sections ["{marker}", "sub-agent-verification"], '
                'footer "{run_key}"; call {redis_set} with key "{redis_key}" and value "{marker}|builder"; '
                "call {pg_execute} with SQL \"INSERT INTO {audit_table} (run_key, scenario, cycle, artifact_path, note) "
                "VALUES ('{run_key}', 'sub_agent', {cycle}, '{artifact_ref}', 'sub-agent-ok:{marker}')\". "
                'After delegation, verify with {redis_get} and {fs_read}, then return a compact cross-agent summary.'
            ),
        ),
        LongRunCase(
            mode='multi_agent_graph',
            config=multi_agent_graph,
            prompt_template=(
                'Run marker: {marker}. Redis key: {redis_key}. Artifact path: {artifact_ref}. Use the path string '
                'verbatim and keep forward slashes. '
                'Planner must call {redis_set} with key "{redis_key}" and value '
                '"plan:{marker}|artifact:{artifact_ref}|run:{run_key}". '
                'Builder must call {redis_get} with key "{redis_key}", then call html_page_builder with output_path '
                '"{artifact_ref}", title "Multi Agent Graph Cycle {cycle}", headline "Multi Agent Graph Cycle {cycle}", '
                'prompt "{marker}", sections ["{marker}", "graph-verification"], and footer "{run_key}". '
                'Reviewer must call {fs_read} with path "{artifact_ref}", then call {pg_execute} with SQL '
                "\"INSERT INTO {audit_table} (run_key, scenario, cycle, artifact_path, note) VALUES "
                "('{run_key}', 'multi_agent_graph', {cycle}, '{artifact_ref}', 'graph-review-ok:{marker}')\". "
                'Every agent response should mention the marker and stop once its own work is complete.'
            ),
        ),
    ]


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


def _postgres_config() -> dict[str, Any]:
    return {
        'host': os.environ.get('PG_HOST', '127.0.0.1'),
        'port': int(os.environ.get('PG_PORT', '5432')),
        'user': os.environ.get('PG_USER', 'postgres'),
        'password': _require_env('PG_PASSWORD'),
        'database': os.environ.get('PG_DATABASE', 'postgres'),
    }


def _redis_config() -> tuple[str, str, int]:
    url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
    parsed = urlparse(url)
    return url, parsed.hostname or '127.0.0.1', parsed.port or 6379


def _check_port(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=2):
        return


def preflight_longrun_environment() -> dict[str, Any]:
    if shutil.which('npx') is None:
        raise RuntimeError('npx is required for filesystem/postgres MCP servers')
    if shutil.which('uvx') is None:
        raise RuntimeError('uvx is required for the Redis MCP server')
    api_key_env = os.environ.get('DEEPSEEK_API_KEY')
    if not api_key_env:
        raise RuntimeError('Missing DEEPSEEK_API_KEY for real long-run validation')
    redis_url, redis_host, redis_port = _redis_config()
    pg = _postgres_config()
    _check_port(redis_host, redis_port)
    _check_port(str(pg['host']), int(pg['port']))
    return {
        'redis_url': redis_url,
        'redis_host': redis_host,
        'redis_port': redis_port,
        'pg_host': pg['host'],
        'pg_port': pg['port'],
        'pg_database': pg['database'],
    }


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if 'text' in item:
                    chunks.append(str(item['text']))
                else:
                    chunks.append(json.dumps(item, ensure_ascii=False))
            else:
                chunks.append(str(item))
        return '\n'.join(chunks)
    if isinstance(value, dict):
        if 'text' in value:
            return str(value['text'])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _summarize_result(result: Any) -> str:
    return _extract_text(result)[:240]


async def _prepare_services(runtime: Any) -> None:
    pg = _postgres_config()
    await runtime.mcp_manager.call_tool('postgres', 'connect_db', pg)
    await runtime.mcp_manager.call_tool(
        'postgres',
        'execute',
        {
            'sql': (
                f'CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} ('
                'run_key TEXT PRIMARY KEY, '
                'scenario TEXT NOT NULL, '
                'cycle INTEGER NOT NULL, '
                'artifact_path TEXT NOT NULL, '
                'note TEXT NOT NULL)'
            )
        },
    )


async def _verify_case(runtime: Any, artifact_path: Path, redis_key: str, run_key: str, marker: str) -> dict[str, Any]:
    artifact_exists = artifact_path.exists()
    file_payload = await runtime.mcp_manager.call_tool('filesystem', 'read_text_file', {'path': str(artifact_path)})
    redis_payload = await runtime.mcp_manager.call_tool('redis', 'get', {'key': redis_key})
    pg_payload = await runtime.mcp_manager.call_tool(
        'postgres',
        'query',
        {'sql': f"SELECT run_key, scenario, cycle, artifact_path, note FROM {AUDIT_TABLE} WHERE run_key = '{run_key}'"},
    )
    file_text = _extract_text(file_payload)
    redis_text = _extract_text(redis_payload)
    pg_text = _extract_text(pg_payload)
    return {
        'artifact_exists': artifact_exists,
        'artifact_contains_marker': marker in file_text,
        'redis_contains_marker': marker in redis_text,
        'postgres_contains_run_key': run_key in pg_text,
        'postgres_contains_marker': marker in pg_text or artifact_path.name in pg_text,
    }


async def _run_case_once(case: LongRunCase, cycle: int, base_root: Path) -> LongRunRecord:
    marker = f'{case.mode}-cycle-{cycle}'
    artifact_path = (base_root / 'artifacts' / case.mode / f'cycle-{cycle}' / 'index.html').resolve()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_ref = artifact_path.as_posix()
    redis_key = f'easy-agent:longrun:{case.mode}:{cycle}'
    run_key = f'{case.mode}:{cycle}:{int(time.time())}'
    config = case.config.model_copy(deep=True)
    config.storage.path = str((base_root / 'storage' / case.mode / f'cycle-{cycle}').resolve())
    runtime = build_runtime_from_config(config)
    prompt = case.prompt_template.format(
        artifact_ref=artifact_ref,
        artifact_path=str(artifact_path),
        audit_table=AUDIT_TABLE,
        cycle=cycle,
        fs_info=_mcp_names()['fs_info'],
        fs_read=_mcp_names()['fs_read'],
        marker=marker,
        pg_execute=_mcp_names()['pg_execute'],
        pg_query=_mcp_names()['pg_query'],
        redis_get=_mcp_names()['redis_get'],
        redis_key=redis_key,
        redis_set=_mcp_names()['redis_set'],
        run_key=run_key,
        title=f'Longrun {case.mode} {cycle}',
    )
    start = time.perf_counter()
    try:
        await runtime.start()
        await _prepare_services(runtime)
        result = await runtime.run(prompt)
        verification = await _verify_case(runtime, artifact_path, redis_key, run_key, marker)
        duration = time.perf_counter() - start
        trace = runtime.store.load_trace(result['run_id'])
        success = all(verification.values())
        return LongRunRecord(
            mode=case.mode,
            cycle=cycle,
            success=success,
            duration_seconds=round(duration, 4),
            artifact_path=str(artifact_path),
            redis_key=redis_key,
            run_key=run_key,
            verification=verification,
            result_summary=_summarize_result(trace.get('output_payload')),
            error=None if success else json.dumps(verification, ensure_ascii=False),
        )
    except Exception as exc:
        duration = time.perf_counter() - start
        return LongRunRecord(
            mode=case.mode,
            cycle=cycle,
            success=False,
            duration_seconds=round(duration, 4),
            artifact_path=str(artifact_path),
            redis_key=redis_key,
            run_key=run_key,
            verification={},
            result_summary='',
            error=str(exc) or exc.__class__.__name__,
        )
    finally:
        try:
            await runtime.aclose()
        except Exception:
            pass


def build_longrun_report(records: list[LongRunRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in sorted({record.mode for record in records}):
        items = [record for record in records if record.mode == mode]
        summary[mode] = {
            'runs': len(items),
            'successes': sum(1 for item in items if item.success),
            'failures': sum(1 for item in items if not item.success),
            'average_duration_seconds': round(mean(item.duration_seconds for item in items), 4),
        }
    return {'records': [asdict(item) for item in records], 'summary': summary}


def run_longrun_suite(config_path: str | Path, cycles: int = 3, output_root: str | Path = '.easy-agent/longrun') -> dict[str, Any]:
    preflight = preflight_longrun_environment()
    base_root = Path(output_root)
    (base_root / 'artifacts').mkdir(parents=True, exist_ok=True)
    base_config = load_config(config_path)
    for server in base_config.mcp:
        if server.name == 'filesystem' and server.command:
            server.command[-1] = str((base_root / 'artifacts').resolve())
    cases = build_longrun_cases(base_config)
    base_root.mkdir(parents=True, exist_ok=True)
    records: list[LongRunRecord] = []
    for case in cases:
        for cycle in range(1, cycles + 1):
            records.append(asyncio.run(_run_case_once(case, cycle, base_root)))
    report = build_longrun_report(records)
    report['preflight'] = preflight
    return report


