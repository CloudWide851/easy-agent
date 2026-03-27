from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_common.models import (
    AssistantResponse,
    ChatMessage,
    HumanRequestStatus,
    Protocol,
    RunStatus,
    ToolCall,
    ToolSpec,
)
from agent_common.tools import ToolRegistry
from agent_config.app import AppConfig, ModelConfig
from agent_graph import AgentOrchestrator
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.storage import SQLiteRunStore
from agent_runtime.harness import HarnessRuntime


def _cycle_from_prompt(prompt: str) -> int:
    match = re.search(r'Cycle: (\d+)', prompt)
    assert match is not None
    return int(match.group(1))


class HarnessModelClient:
    def __init__(
        self,
        *,
        complete_on_cycle: int = 2,
        fail_cycle_two_once: bool = False,
        replan_first_cycle: bool = False,
    ) -> None:
        self.complete_on_cycle = complete_on_cycle
        self.fail_cycle_two_once = fail_cycle_two_once
        self.replan_first_cycle = replan_first_cycle
        self.initializer_calls = 0
        self.evaluator_calls = 0
        self.worker_attempts: dict[int, int] = {}
        self._replan_sent = False

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantResponse:
        system = messages[0].content if messages else ''
        prompt = next((message.content for message in reversed(messages) if message.role == 'user'), '')
        tool_result = bool(messages) and messages[-1].role == 'tool'

        if 'initializer agent' in system:
            self.initializer_calls += 1
            return AssistantResponse(text=f'bootstrap summary v{self.initializer_calls}', protocol=Protocol.OPENAI)

        if 'worker agent' in system:
            cycle = _cycle_from_prompt(prompt)
            self.worker_attempts[cycle] = self.worker_attempts.get(cycle, 0) + 1
            if self.fail_cycle_two_once and cycle == 2 and self.worker_attempts[cycle] == 1 and not tool_result:
                raise RuntimeError('worker failed once on cycle two')
            if not tool_result:
                return AssistantResponse(
                    text='',
                    tool_calls=[ToolCall(id=f'worker-{cycle}', name='python_echo', arguments={'prompt': f'cycle-{cycle}'})],
                    protocol=Protocol.OPENAI,
                )
            return AssistantResponse(text=f'worker cycle {cycle} done', protocol=Protocol.OPENAI)

        if 'team planner' in system:
            cycle = _cycle_from_prompt(prompt)
            if not tool_result:
                return AssistantResponse(
                    text='',
                    tool_calls=[ToolCall(id=f'team-plan-{cycle}', name='python_echo', arguments={'prompt': f'team-cycle-{cycle}'})],
                    protocol=Protocol.OPENAI,
                )
            return AssistantResponse(text=f'team planner cycle {cycle}', protocol=Protocol.OPENAI)

        if 'team closer' in system:
            cycle = _cycle_from_prompt(prompt)
            return AssistantResponse(text=f'team closer cycle {cycle} TERMINATE', protocol=Protocol.OPENAI)

        if 'evaluator agent' in system:
            self.evaluator_calls += 1
            cycle = _cycle_from_prompt(prompt)
            if self.replan_first_cycle and cycle == 1 and not self._replan_sent:
                self._replan_sent = True
                return AssistantResponse(
                    text='DECISION: REPLAN\nSUMMARY: bootstrap needs refresh\nNEXT: update the plan',
                    protocol=Protocol.OPENAI,
                )
            decision = 'COMPLETE' if cycle >= self.complete_on_cycle else 'CONTINUE'
            return AssistantResponse(
                text=f'DECISION: {decision}\nSUMMARY: cycle {cycle} checked\nNEXT: move forward',
                protocol=Protocol.OPENAI,
            )

        raise RuntimeError(f'Unhandled harness stub request: {system!r}')

    async def aclose(self) -> None:
        return None


def build_harness_runtime(
    tmp_path: Path,
    model_client: HarnessModelClient | None = None,
    *,
    worker_target: str = 'worker',
) -> tuple[HarnessRuntime, SQLiteRunStore, HarnessModelClient]:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'worker',
                'agents': [
                    {
                        'name': 'initializer',
                        'description': 'Initializes harness state.',
                        'system_prompt': 'initializer agent',
                        'tools': [],
                    },
                    {
                        'name': 'worker',
                        'description': 'Executes one increment at a time.',
                        'system_prompt': 'worker agent',
                        'tools': ['python_echo'],
                    },
                    {
                        'name': 'evaluator',
                        'description': 'Evaluates completion.',
                        'system_prompt': 'evaluator agent',
                        'tools': [],
                    },
                    {
                        'name': 'planner',
                        'description': 'Plans for a team worker.',
                        'system_prompt': 'team planner',
                        'tools': ['python_echo'],
                    },
                    {
                        'name': 'closer',
                        'description': 'Closes for a team worker.',
                        'system_prompt': 'team closer',
                        'tools': [],
                    },
                ],
                'teams': [
                    {
                        'name': 'worker_team',
                        'mode': 'round_robin',
                        'members': ['planner', 'closer'],
                        'max_turns': 2,
                    }
                ],
                'nodes': [],
            },
            'harnesses': [
                {
                    'name': 'delivery_loop',
                    'initializer_agent': 'initializer',
                    'worker_target': worker_target,
                    'evaluator_agent': 'evaluator',
                    'completion_contract': 'Finish the requested work and explain the result clearly.',
                    'artifacts_dir': str(tmp_path / 'artifacts'),
                    'max_cycles': 3,
                    'max_replans': 1,
                }
            ],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name='python_echo',
            description='Echo harness input.',
            input_schema={'type': 'object', 'properties': {'prompt': {'type': 'string'}}, 'required': ['prompt']},
        ),
        lambda arguments, context: {'echo': arguments['prompt'], 'run_id': context.run_id},
    )
    store = SQLiteRunStore(tmp_path, 'state.db')
    client = model_client or HarnessModelClient()
    guardrails = GuardrailEngine()
    orchestrator = AgentOrchestrator(config, client, registry, store, guardrails)
    return HarnessRuntime(config, orchestrator, store, guardrails), store, client


@pytest.mark.asyncio
async def test_harness_run_persists_artifacts_and_state(tmp_path: Path) -> None:
    harness_runtime, store, client = build_harness_runtime(tmp_path)

    result = await harness_runtime.run('delivery_loop', 'prepare the release notes', session_id='session-alpha')
    trace = store.load_trace(result['run_id'])
    saved_state = store.load_harness_state('session-alpha', 'delivery_loop')
    features = json.loads(Path(result['result']['features_path']).read_text(encoding='utf-8'))

    assert result['session_id'] == 'session-alpha'
    assert result['result']['status'] == 'succeeded'
    assert result['result']['cycles_completed'] == 2
    assert client.initializer_calls == 1
    assert trace['run_kind'] == 'harness'
    assert any(event['kind'] == 'harness_initialized' for event in trace['events'])
    assert any(event['kind'] == 'harness_evaluated' for event in trace['events'])
    assert saved_state['status'] == 'succeeded'
    assert features['cycles_completed'] == 2
    assert Path(result['result']['bootstrap_path']).is_file()
    assert Path(result['result']['progress_path']).is_file()


@pytest.mark.asyncio
async def test_harness_resume_uses_checkpoint_without_rerunning_initializer(tmp_path: Path) -> None:
    harness_runtime, store, client = build_harness_runtime(
        tmp_path,
        HarnessModelClient(fail_cycle_two_once=True),
    )

    with pytest.raises(RuntimeError, match='failed') as exc_info:
        await harness_runtime.run('delivery_loop', 'finish the checklist', session_id='session-beta')

    run_id = str(exc_info.value).split()[2]
    waiting = await harness_runtime.resume(run_id)
    request = store.load_human_request(waiting['request_id'])

    assert waiting['status'] == RunStatus.WAITING_APPROVAL.value
    assert request.status is HumanRequestStatus.PENDING

    store.resolve_human_request(request.request_id, status=HumanRequestStatus.APPROVED)
    resumed = await harness_runtime.resume(run_id)
    trace = store.load_trace(run_id)

    assert resumed['result']['status'] == 'succeeded'
    assert client.initializer_calls == 1
    assert client.worker_attempts[1] == 2
    assert client.worker_attempts[2] == 3
    assert any(event['kind'] == 'run_resumed' for event in trace['events'])


@pytest.mark.asyncio
async def test_harness_replan_refreshes_initializer_summary(tmp_path: Path) -> None:
    harness_runtime, store, client = build_harness_runtime(
        tmp_path,
        HarnessModelClient(complete_on_cycle=2, replan_first_cycle=True),
    )

    result = await harness_runtime.run('delivery_loop', 'ship a demo page', session_id='session-gamma')
    trace = store.load_trace(result['run_id'])

    assert result['result']['replan_count'] == 1
    assert client.initializer_calls == 2
    assert any(event['kind'] == 'harness_replanned' for event in trace['events'])


@pytest.mark.asyncio
async def test_harness_allows_team_worker_target(tmp_path: Path) -> None:
    harness_runtime, _, _ = build_harness_runtime(tmp_path, worker_target='worker_team')

    result = await harness_runtime.run('delivery_loop', 'coordinate a small team run')

    assert result['result']['status'] == 'succeeded'
