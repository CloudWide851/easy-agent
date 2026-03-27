from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_common.models import (
    AssistantResponse,
    ChatMessage,
    HumanRequestStatus,
    Protocol,
    RunContext,
    RunStatus,
    ToolCall,
    ToolSpec,
)
from agent_common.tools import ToolRegistry
from agent_config.app import AppConfig, ModelConfig
from agent_graph import AgentOrchestrator, GraphScheduler
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.storage import SQLiteRunStore


class StubModelClient:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantResponse:
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='1', name='python_echo', arguments={'prompt': 'from-tool'})],
                protocol=Protocol.OPENAI,
            )
        return AssistantResponse(text='done', protocol=Protocol.OPENAI)

    async def aclose(self) -> None:
        return None


class SessionAwareModelClient:
    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantResponse:
        del tools
        transcript = '\n'.join(f'{message.role}:{message.content}' for message in messages)
        if 'first-session-input' in transcript and messages[-1].content == 'second-session-input':
            return AssistantResponse(text='reused-session', protocol=Protocol.OPENAI)
        return AssistantResponse(text='fresh-session', protocol=Protocol.OPENAI)

    async def aclose(self) -> None:
        return None


class RepairModelClient:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantResponse:
        del tools
        self.calls += 1
        if self.calls == 1:
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='repair-1', name='math.gcd', arguments={'num1': 12})],
                protocol=Protocol.OPENAI,
            )
        if self.calls == 2:
            assert 'ValidationError' in messages[-1].content
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='repair-2', name='math.gcd', arguments={'num1': 12, 'num2': 18})],
                protocol=Protocol.OPENAI,
            )
        assert messages[-1].name == 'math.gcd'
        return AssistantResponse(text='repaired', protocol=Protocol.OPENAI)

    async def aclose(self) -> None:
        return None


class DummyMcpManager:
    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, str],
        context: RunContext | None = None,
    ) -> dict[str, str]:
        del context
        return {'server': server_name, 'tool': tool_name, **arguments}


@pytest.mark.asyncio
async def test_graph_scheduler_runs_agent_with_tool_loop(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'system_prompt': 'system',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                    }
                ],
                'nodes': [],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': [['cmd', '/c', 'echo']]},
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name='python_echo', description='Echo', input_schema={'type': 'object'}),
        lambda arguments, context: {'echo': arguments['prompt'], 'run_id': context.run_id},
    )
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = StubModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())

    result = await scheduler.run('hello')
    trace = store.load_trace(result['run_id'])

    assert result['result'] == 'done'
    assert any(event['kind'] == 'tool_call_succeeded' for event in trace['events'])
    assert any(event['kind'] == 'run_succeeded' for event in trace['events'])


@pytest.mark.asyncio
async def test_graph_scheduler_repairs_invalid_tool_arguments(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'system_prompt': 'system',
                        'tools': ['math.gcd'],
                        'sub_agents': [],
                    }
                ],
                'nodes': [],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name='math.gcd',
            description='gcd',
            input_schema={
                'type': 'object',
                'properties': {'num1': {'type': 'integer'}, 'num2': {'type': 'integer'}},
                'required': ['num1', 'num2'],
            },
        ),
        lambda arguments, context: {'gcd': 6, 'arguments': arguments, 'run_id': context.run_id},
    )
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = RepairModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())

    result = await scheduler.run('calculate gcd')
    trace = store.load_trace(result['run_id'])

    assert result['result'] == 'repaired'
    assert any(event['kind'] == 'tool_validation_failed' for event in trace['events'])
    successful = [event for event in trace['events'] if event['kind'] == 'tool_call_succeeded']
    assert successful[-1]['payload']['arguments'] == {'num1': 12, 'num2': 18}


@pytest.mark.asyncio
async def test_graph_scheduler_retries_failed_nodes(tmp_path: Path) -> None:
    attempts = {'count': 0}
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'prepare',
                'agents': [],
                'nodes': [
                    {
                        'id': 'prepare',
                        'type': 'skill',
                        'target': 'flaky',
                        'retries': 1,
                        'timeout_seconds': 5,
                    }
                ],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()

    async def flaky(arguments: dict[str, str], context: RunContext) -> str:
        del arguments, context
        attempts['count'] += 1
        if attempts['count'] == 1:
            raise RuntimeError('boom')
        return 'ok'

    registry.register(ToolSpec(name='flaky', description='Retry me', input_schema={'type': 'object'}), flaky)
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = StubModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())

    result = await scheduler.run('hello')

    assert result['result'] == 'ok'
    assert attempts['count'] == 2


@pytest.mark.asyncio
async def test_direct_agent_run_reuses_explicit_session_memory(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [{'name': 'coordinator', 'system_prompt': 'system', 'tools': [], 'sub_agents': []}],
                'teams': [],
                'nodes': [],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = SessionAwareModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())

    first = await scheduler.run('first-session-input', session_id='session-a')
    second = await scheduler.run('second-session-input', session_id='session-a')

    assert first['result'] == 'fresh-session'
    assert second['result'] == 'reused-session'
    assert second['session_id'] == 'session-a'


@pytest.mark.asyncio
async def test_graph_scheduler_resumes_from_checkpoint_without_rerunning_completed_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = {'prepare': 0, 'review': 0}
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'aggregate',
                'agents': [],
                'nodes': [
                    {
                        'id': 'prepare',
                        'type': 'skill',
                        'target': 'prepare',
                    },
                    {
                        'id': 'review',
                        'type': 'skill',
                        'target': 'review',
                        'deps': ['prepare'],
                        'input_template': 'use prior {prepare}',
                    },
                    {
                        'id': 'aggregate',
                        'type': 'join',
                        'deps': ['prepare', 'review'],
                    },
                ],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()

    async def prepare(arguments: dict[str, str], context: RunContext) -> dict[str, str]:
        del arguments, context
        counts['prepare'] += 1
        return {'step': 'prepare'}

    async def review(arguments: dict[str, str], context: RunContext) -> dict[str, str]:
        counts['review'] += 1
        if counts['review'] == 1:
            raise RuntimeError('review failed once')
        return {'step': 'review', 'from': context.shared_state['prepare']['step']}

    registry.register(ToolSpec(name='prepare', description='prepare', input_schema={'type': 'object'}), prepare)
    registry.register(ToolSpec(name='review', description='review', input_schema={'type': 'object'}), review)
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = StubModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())
    monkeypatch.setattr('agent_graph.scheduler.uuid.uuid4', lambda: SimpleNamespace(hex='graph-resume-run'))

    with pytest.raises(RuntimeError, match='Run graph-resume-run failed'):
        await scheduler.run('graph-input')

    resumed = await scheduler.resume('graph-resume-run')
    trace = store.load_trace('graph-resume-run')

    assert resumed['result']['review']['step'] == 'review'
    assert counts['prepare'] == 1
    assert counts['review'] == 2
    assert any(event['kind'] == 'run_resumed' for event in trace['events'])


@pytest.mark.asyncio
async def test_direct_agent_sensitive_tool_waits_for_approval_then_resumes(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'system_prompt': 'system',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                    }
                ],
                'nodes': [],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': [], 'human_loop': {'sensitive_tools': ['python_echo']}},
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name='python_echo', description='Echo', input_schema={'type': 'object'}),
        lambda arguments, context: {'echo': arguments['prompt'], 'run_id': context.run_id},
    )
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = StubModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())

    waiting = await scheduler.run('hello')
    request = store.load_human_request(waiting['request_id'])

    assert waiting['status'] == RunStatus.WAITING_APPROVAL.value
    assert request.status is HumanRequestStatus.PENDING

    store.resolve_human_request(request.request_id, status=HumanRequestStatus.APPROVED)
    resumed = await scheduler.resume(waiting['run_id'])
    trace = store.load_trace(waiting['run_id'])

    assert resumed['result'] == 'done'
    assert any(event['kind'] == 'human_request_created' for event in trace['events'])


@pytest.mark.asyncio
async def test_direct_agent_interrupts_at_safe_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [{'name': 'coordinator', 'system_prompt': 'system', 'tools': [], 'sub_agents': []}],
                'teams': [],
                'nodes': [],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = SessionAwareModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())
    monkeypatch.setattr('agent_graph.scheduler.uuid.uuid4', lambda: SimpleNamespace(hex='interrupt-run'))

    store.request_interrupt('interrupt-run', {'reason': 'manual stop'})
    result = await scheduler.run('stop now')
    trace = store.load_trace('interrupt-run')

    assert result['status'] == RunStatus.INTERRUPTED.value
    assert result['payload']['reason'] == 'manual stop'
    assert any(event['kind'] == 'run_interrupt_consumed' for event in trace['events'])


@pytest.mark.asyncio
async def test_graph_scheduler_replay_and_fork_resume_preserve_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    counts = {'prepare': 0, 'review': 0}
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'aggregate',
                'agents': [],
                'nodes': [
                    {
                        'id': 'prepare',
                        'type': 'skill',
                        'target': 'prepare',
                    },
                    {
                        'id': 'review',
                        'type': 'skill',
                        'target': 'review',
                        'deps': ['prepare'],
                        'input_template': 'use prior {prepare}',
                    },
                    {
                        'id': 'aggregate',
                        'type': 'join',
                        'deps': ['prepare', 'review'],
                    },
                ],
            },
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()

    async def prepare(arguments: dict[str, str], context: RunContext) -> dict[str, str]:
        del arguments, context
        counts['prepare'] += 1
        return {'step': 'prepare'}

    async def review(arguments: dict[str, str], context: RunContext) -> dict[str, str]:
        counts['review'] += 1
        if counts['review'] == 1:
            raise RuntimeError('review failed once')
        return {'step': 'review', 'from': context.shared_state['prepare']['step']}

    registry.register(ToolSpec(name='prepare', description='prepare', input_schema={'type': 'object'}), prepare)
    registry.register(ToolSpec(name='review', description='review', input_schema={'type': 'object'}), review)
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = StubModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())
    monkeypatch.setattr('agent_graph.scheduler.uuid.uuid4', lambda: SimpleNamespace(hex='graph-fork-run'))

    with pytest.raises(RuntimeError, match='Run graph-fork-run failed'):
        await scheduler.run('graph-input')

    checkpoints = scheduler.list_checkpoints('graph-fork-run')
    replay = await scheduler.replay('graph-fork-run', checkpoints[-1]['checkpoint_id'])
    monkeypatch.undo()
    forked = await scheduler.resume('graph-fork-run', checkpoint_id=checkpoints[-1]['checkpoint_id'], fork=True)
    original_trace = store.load_trace('graph-fork-run')
    fork_trace = store.load_trace(forked['run_id'])

    assert replay['checkpoint_kind'] == 'graph'
    assert replay['state']['results']['prepare']['step'] == 'prepare'
    assert forked['run_id'] != 'graph-fork-run'
    assert forked['result']['review']['step'] == 'review'
    assert counts['prepare'] == 1
    assert counts['review'] == 2
    assert original_trace['lineage']['child_runs'][0]['run_id'] == forked['run_id']
    assert fork_trace['lineage']['parent_run_id'] == 'graph-fork-run'
    assert fork_trace['lineage']['resume_strategy'] == 'fork'


class DummyFederationManager:
    async def run_remote(
        self,
        remote_name: str,
        target: str,
        input_text: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del session_id, metadata
        return {'remote': remote_name, 'target': target, 'input': input_text}


@pytest.mark.asyncio
async def test_graph_scheduler_runs_federated_node(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'remote_step',
                'agents': [],
                'nodes': [
                    {
                        'id': 'remote_step',
                        'type': 'federated',
                        'target': 'loopback/local_echo',
                    }
                ],
            },
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()
    store = SQLiteRunStore(tmp_path, 'state.db')
    model_client = StubModelClient()
    orchestrator = AgentOrchestrator(config, model_client, registry, store, GuardrailEngine())
    scheduler = GraphScheduler(
        config,
        registry,
        orchestrator,
        store,
        DummyMcpManager(),
        GuardrailEngine(),
        federation_manager=cast(Any, DummyFederationManager()),
    )

    result = await scheduler.run('federated input')

    assert result['result']['remote'] == 'loopback'
    assert result['result']['target'] == 'local_echo'


