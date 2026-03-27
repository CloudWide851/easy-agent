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
from agent_graph import AgentOrchestrator, GraphScheduler
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.storage import SQLiteRunStore


class TeamModelClient:
    def __init__(self, fail_closer_once: bool = False) -> None:
        self.fail_closer_once = fail_closer_once
        self.closer_failures = 0

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantResponse:
        system = messages[0].content if messages else ''
        tool_names = [tool.name for tool in tools]
        has_tool_result = bool(messages) and messages[-1].role == 'tool'
        selector_call = not tools and 'Members:' in (messages[-1].content if messages else '')
        if selector_call:
            transcript = messages[-1].content
            if 'research ready' in transcript:
                return AssistantResponse(text='closer', protocol=Protocol.OPENAI)
            return AssistantResponse(text='researcher', protocol=Protocol.OPENAI)
        if any(name.startswith('handoff__') for name in tool_names) and 'Immediately hand off' in system and not has_tool_result:
            return AssistantResponse(
                text='Routing to finisher.',
                tool_calls=[
                    ToolCall(
                        id='handoff-1',
                        name='handoff__finisher',
                        arguments={'message': 'Finish the swarm task and terminate.'},
                    )
                ],
                protocol=Protocol.OPENAI,
            )
        if 'Use python_echo exactly once on the handoff task' in system and not has_tool_result:
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='swarm-1', name='python_echo', arguments={'prompt': 'swarm-finish'})],
                protocol=Protocol.OPENAI,
            )
        if 'Use python_echo exactly once on the handoff task' in system and has_tool_result:
            return AssistantResponse(text='swarm done TERMINATE', protocol=Protocol.OPENAI)
        if 'research ready' in system and not has_tool_result:
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='sel-1', name='python_echo', arguments={'prompt': 'selector-research'})],
                protocol=Protocol.OPENAI,
            )
        if 'research ready' in system and has_tool_result:
            return AssistantResponse(text='research ready', protocol=Protocol.OPENAI)
        if 'finish with TERMINATE' in system and not has_tool_result:
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='sel-2', name='python_echo', arguments={'prompt': 'selector-close'})],
                protocol=Protocol.OPENAI,
            )
        if 'finish with TERMINATE' in system and has_tool_result:
            return AssistantResponse(text='selector done TERMINATE', protocol=Protocol.OPENAI)
        if 'do not terminate' in system and not has_tool_result:
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='rr-1', name='python_echo', arguments={'prompt': 'round-robin-plan'})],
                protocol=Protocol.OPENAI,
            )
        if 'do not terminate' in system and has_tool_result:
            return AssistantResponse(text='planner ready', protocol=Protocol.OPENAI)
        if 'end with TERMINATE' in system and not has_tool_result:
            if self.fail_closer_once and self.closer_failures == 0:
                self.closer_failures += 1
                raise RuntimeError('closer failed once')
            return AssistantResponse(
                text='',
                tool_calls=[ToolCall(id='close-1', name='python_echo', arguments={'prompt': 'team-close'})],
                protocol=Protocol.OPENAI,
            )
        if 'end with TERMINATE' in system and has_tool_result:
            return AssistantResponse(text='closer done TERMINATE', protocol=Protocol.OPENAI)
        raise RuntimeError(f'Unhandled stub request: system={system!r}, tools={tool_names!r}')

    async def aclose(self) -> None:
        return None


class DummyMcpManager:
    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, str]) -> dict[str, str]:
        return {'server': server_name, 'tool': tool_name, **arguments}


def build_scheduler(
    tmp_path: Path,
    graph_payload: dict[str, object],
    model_client: TeamModelClient | None = None,
) -> GraphScheduler:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': graph_payload,
            'skills': [],
            'mcp': [],
            'storage': {'path': str(tmp_path), 'database': 'state.db'},
            'security': {'allowed_commands': []},
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name='python_echo', description='Echo', input_schema={'type': 'object'}),
        lambda arguments, context: {'echo': arguments['prompt'], 'run_id': context.run_id},
    )
    store = SQLiteRunStore(tmp_path, 'state.db')
    client = model_client or TeamModelClient()
    orchestrator = AgentOrchestrator(config, client, registry, store, GuardrailEngine())
    orchestrator.register_subagent_tools()
    return GraphScheduler(config, registry, orchestrator, store, DummyMcpManager(), GuardrailEngine())


@pytest.mark.asyncio
async def test_round_robin_team_runs_and_terminates(tmp_path: Path) -> None:
    scheduler = build_scheduler(
        tmp_path,
        {
            'entrypoint': 'round_robin_team',
            'agents': [
                {
                    'name': 'planner',
                    'description': 'Plans the work.',
                    'system_prompt': 'Use python_echo exactly once, summarize the task, and do not terminate.',
                    'tools': ['python_echo'],
                },
                {
                    'name': 'closer',
                    'description': 'Closes the work.',
                    'system_prompt': 'Use python_echo exactly once, summarize the closure, and end with TERMINATE.',
                    'tools': ['python_echo'],
                },
            ],
            'teams': [
                {'name': 'round_robin_team', 'mode': 'round_robin', 'members': ['planner', 'closer'], 'max_turns': 4}
            ],
            'nodes': [],
        },
    )

    result = await scheduler.run('team task')
    trace = scheduler.store.load_trace(result['run_id'])

    assert result['result']['terminated_by'] == 'closer'
    assert [turn['speaker'] for turn in result['result']['turns']] == ['planner', 'closer']
    assert any(event['kind'] == 'team_finish' for event in trace['events'])


@pytest.mark.asyncio
async def test_round_robin_team_resume_skips_completed_turns(tmp_path: Path) -> None:
    scheduler = build_scheduler(
        tmp_path,
        {
            'entrypoint': 'round_robin_team',
            'agents': [
                {
                    'name': 'planner',
                    'description': 'Plans the work.',
                    'system_prompt': 'Use python_echo exactly once, summarize the task, and do not terminate.',
                    'tools': ['python_echo'],
                },
                {
                    'name': 'closer',
                    'description': 'Closes the work.',
                    'system_prompt': 'Use python_echo exactly once, summarize the closure, and end with TERMINATE.',
                    'tools': ['python_echo'],
                },
            ],
            'teams': [
                {'name': 'round_robin_team', 'mode': 'round_robin', 'members': ['planner', 'closer'], 'max_turns': 4}
            ],
            'nodes': [],
        },
        model_client=TeamModelClient(fail_closer_once=True),
    )

    with pytest.raises(RuntimeError, match='failed') as exc_info:
        await scheduler.run('team task')

    run_id = str(exc_info.value).split()[1]
    resumed = await scheduler.resume(run_id)
    trace = scheduler.store.load_trace(run_id)
    team_turn_events = [event for event in trace['events'] if event['kind'] == 'team_turn']

    assert resumed['result']['terminated_by'] == 'closer'
    assert [event['payload']['speaker'] for event in team_turn_events].count('planner') == 1
    assert [event['payload']['speaker'] for event in team_turn_events].count('closer') == 2
    assert any(event['kind'] == 'run_resumed' for event in trace['events'])


@pytest.mark.asyncio
async def test_selector_team_chooses_members_from_transcript(tmp_path: Path) -> None:
    scheduler = build_scheduler(
        tmp_path,
        {
            'entrypoint': 'selector_team',
            'agents': [
                {
                    'name': 'researcher',
                    'description': 'Use this member first to create a short research note when no research note exists yet.',
                    'system_prompt': 'Use python_echo exactly once and say research ready without TERMINATE.',
                    'tools': ['python_echo'],
                },
                {
                    'name': 'closer',
                    'description': 'Use this member after research is ready to close the run with TERMINATE.',
                    'system_prompt': 'Use python_echo exactly once and finish with TERMINATE.',
                    'tools': ['python_echo'],
                },
            ],
            'teams': [
                {
                    'name': 'selector_team',
                    'mode': 'selector',
                    'members': ['researcher', 'closer'],
                    'max_turns': 4,
                    'selector_prompt': 'Choose researcher before research ready appears, otherwise choose closer.',
                }
            ],
            'nodes': [],
        },
    )

    result = await scheduler.run('selector task')

    assert result['result']['terminated_by'] == 'closer'
    assert [turn['speaker'] for turn in result['result']['turns']] == ['researcher', 'closer']


@pytest.mark.asyncio
async def test_swarm_team_handoff_switches_speaker(tmp_path: Path) -> None:
    scheduler = build_scheduler(
        tmp_path,
        {
            'entrypoint': 'swarm_team',
            'agents': [
                {
                    'name': 'dispatcher',
                    'description': 'Routes work to the finisher using handoff tools.',
                    'system_prompt': 'Immediately hand off to the finisher with a brief message about the task.',
                    'tools': [],
                },
                {
                    'name': 'finisher',
                    'description': 'Completes the task after a handoff and finishes with TERMINATE.',
                    'system_prompt': 'Use python_echo exactly once on the handoff task and end with TERMINATE.',
                    'tools': ['python_echo'],
                },
            ],
            'teams': [
                {'name': 'swarm_team', 'mode': 'swarm', 'members': ['dispatcher', 'finisher'], 'max_turns': 4}
            ],
            'nodes': [],
        },
    )

    waiting = await scheduler.run('swarm task')
    request = scheduler.store.load_human_request(waiting['request_id'])

    assert waiting['status'] == RunStatus.WAITING_APPROVAL.value
    assert request.status is HumanRequestStatus.PENDING

    scheduler.store.resolve_human_request(request.request_id, status=HumanRequestStatus.APPROVED)
    result = await scheduler.resume(waiting['run_id'])
    trace = scheduler.store.load_trace(waiting['run_id'])

    assert result['result']['terminated_by'] == 'finisher'
    assert result['result']['turns'][0]['handoff_target'] == 'finisher'
    assert any(event['kind'] == 'team_handoff' for event in trace['events'])


@pytest.mark.asyncio
async def test_team_node_dispatches_inside_graph(tmp_path: Path) -> None:
    scheduler = build_scheduler(
        tmp_path,
        {
            'entrypoint': 'collaboration',
            'agents': [
                {
                    'name': 'planner',
                    'description': 'Plans the work.',
                    'system_prompt': 'Use python_echo exactly once, summarize the task, and do not terminate.',
                    'tools': ['python_echo'],
                },
                {
                    'name': 'closer',
                    'description': 'Closes the work.',
                    'system_prompt': 'Use python_echo exactly once, summarize the closure, and end with TERMINATE.',
                    'tools': ['python_echo'],
                },
            ],
            'teams': [
                {'name': 'round_robin_team', 'mode': 'round_robin', 'members': ['planner', 'closer'], 'max_turns': 4}
            ],
            'nodes': [
                {
                    'id': 'collaboration',
                    'type': 'team',
                    'target': 'round_robin_team',
                    'input_template': '{input}',
                }
            ],
        },
    )

    result = await scheduler.run('graph team task')

    assert result['result']['terminated_by'] == 'closer'

