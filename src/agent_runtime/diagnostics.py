from __future__ import annotations

import json
from typing import Any, Protocol

from agent_common.models import RunStatus
from agent_runtime.tasks import render_task_prompt


class DiagnosticStore(Protocol):
    def load_run_summary(self, run_id: str) -> dict[str, Any]: ...
    def load_trace(self, run_id: str) -> dict[str, Any]: ...


def explain_run(store: DiagnosticStore, run_id: str) -> dict[str, Any]:
    summary = store.load_run_summary(run_id)
    trace = store.load_trace(run_id)
    events = [dict(item) for item in trace.get('events', []) if isinstance(item, dict)]
    event_kinds = [str(item.get('kind') or '') for item in events]
    text = json.dumps({'summary': summary, 'events': events}, ensure_ascii=False, default=str)
    status = str(summary.get('status') or trace.get('status') or '')

    layer, headline, actions = _classify(status, event_kinds, text)
    evidence = _evidence(summary, events, text)
    return {
        'run_id': run_id,
        'status': status,
        'likely_layer': layer,
        'headline': headline,
        'evidence': evidence,
        'recommended_actions': actions,
        'counts': {
            'events': int(summary.get('event_count') or len(events)),
            'nodes': int(summary.get('node_count') or len(trace.get('nodes', []))),
            'checkpoints': int(summary.get('checkpoint_count') or len(trace.get('checkpoints', []))),
            'human_requests': int(summary.get('human_request_count') or len(trace.get('human_requests', []))),
        },
    }


def build_fix_package(store: DiagnosticStore, run_id: str, *, task_pack: str = 'auto') -> dict[str, Any]:
    explanation = explain_run(store, run_id)
    selected_pack = _select_task_pack(str(task_pack), explanation)
    context = _fix_context(explanation)
    return {
        'run_id': run_id,
        'mode': 'advice_only',
        'selected_task_pack': selected_pack,
        'explanation': explanation,
        'probable_cause': _probable_cause(explanation),
        'recommended_commands': _fix_commands(run_id, explanation),
        'safety_notes': _safety_notes(explanation),
        'task_prompt': render_task_prompt(selected_pack, context),
    }


def fix_package_markdown(payload: dict[str, Any]) -> str:
    raw_explanation = payload.get('explanation')
    explanation: dict[str, Any] = raw_explanation if isinstance(raw_explanation, dict) else {}
    raw_commands = payload.get('recommended_commands')
    commands: list[Any] = raw_commands if isinstance(raw_commands, list) else []
    raw_notes = payload.get('safety_notes')
    notes: list[Any] = raw_notes if isinstance(raw_notes, list) else []
    return '\n'.join(
        [
            f"# easy-agent run fix: {payload.get('run_id')}",
            '',
            f"- Mode: `{payload.get('mode')}`",
            f"- Layer: `{explanation.get('likely_layer', 'unknown')}`",
            f"- Status: `{explanation.get('status', 'unknown')}`",
            f"- Task pack: `{payload.get('selected_task_pack')}`",
            f"- Headline: {explanation.get('headline', '-')}",
            f"- Probable cause: {payload.get('probable_cause', '-')}",
            '',
            '## Recommended Commands',
            *[f"- `{item}`" for item in commands],
            '',
            '## Safety Notes',
            *[f"- {item}" for item in notes],
            '',
            '## Task Prompt',
            '',
            '```text',
            str(payload.get('task_prompt') or ''),
            '```',
        ]
    )


def _classify(status: str, event_kinds: list[str], text: str) -> tuple[str, str, list[str]]:
    if status == RunStatus.SUCCEEDED.value:
        if any('retry' in kind or 'repair' in kind for kind in event_kinds):
            return (
                'runtime_recovery',
                'Run succeeded after a retry or repair path.',
                ['Inspect trace spans to confirm the retry path is expected before using the run as a golden example.'],
            )
        return ('success', 'Run completed successfully.', ['Use traces export --tree when you need detailed timing or tool-call context.'])
    if status == RunStatus.WAITING_APPROVAL.value:
        return (
            'human_approval',
            'Run is waiting for a human approval request.',
            ['Use approvals list/show, then approve or reject the pending request before resuming the run.'],
        )
    if status == RunStatus.INTERRUPTED.value:
        return (
            'human_interrupt',
            'Run was interrupted at a safe point.',
            ['Inspect checkpoints and resume or fork from the latest safe checkpoint when ready.'],
        )
    if 'Missing API key environment variable' in text:
        return (
            'model_provider',
            'The configured model provider is missing its API key environment variable.',
            ['Set the configured api_key_env value, or rerun the workflow with the mock provider for offline validation.'],
        )
    if any(kind == 'tool_validation_failed' for kind in event_kinds):
        return (
            'tool_validation',
            'A tool call did not satisfy the registered input schema.',
            ['Inspect the tool_validation_failed event and tighten the tool prompt or schema before rerunning.'],
        )
    if 'guardrail' in text and '"outcome": "block"' in text:
        return (
            'guardrail',
            'A guardrail blocked tool input or final output.',
            ['Inspect guardrail events and decide whether the input, tool arguments, or policy should change.'],
        )
    if any(kind == 'tool_call_failed' for kind in event_kinds):
        return (
            'tool_runtime',
            'A tool raised an exception during execution.',
            ['Inspect the failed tool event and reproduce the tool with the recorded normalized arguments.'],
        )
    if 'MCP' in text or 'mcp' in text:
        return (
            'mcp',
            'The failure appears related to MCP transport, catalog, or tool execution.',
            ['Run mcp list/resources/prompts checks for the configured server and verify roots, auth, and transport startup.'],
        )
    if 'max_iterations' in text or 'exceeded max_iterations' in text:
        return (
            'agent_loop',
            'The agent exceeded its iteration budget.',
            ['Tighten the agent prompt/tool contract, block duplicate tool loops, or raise max_iterations only after reviewing trace output.'],
        )
    if 'Event loop is closed' in text or 'I/O operation on closed pipe' in text:
        return (
            'cleanup_warning',
            'The run encountered a known Windows asyncio subprocess cleanup warning.',
            ['Treat this as cleanup debt when the test result is otherwise green; inspect subprocess teardown if it starts failing the suite.'],
        )
    return (
        'runtime',
        'Run failed, but no specialized classifier matched the stored trace.',
        ['Export the trace tree and inspect the latest run_failed, node failure, or tool event.'],
    )


def _evidence(summary: dict[str, Any], events: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    output_payload = summary.get('output_payload')
    if output_payload:
        evidence.append({'source': 'run.output_payload', 'value': output_payload})
    for event in events[-8:]:
        kind = str(event.get('kind') or '')
        if kind.endswith('failed') or kind in {'tool_validation_failed', 'run_waiting_approval', 'run_interrupted'}:
            evidence.append({'source': f"event:{kind}", 'value': event.get('payload') or {}})
    if 'Event loop is closed' in text:
        evidence.append({'source': 'known_warning', 'value': 'Event loop is closed'})
    if 'I/O operation on closed pipe' in text:
        evidence.append({'source': 'known_warning', 'value': 'I/O operation on closed pipe'})
    return evidence[:6]


def _select_task_pack(requested: str, explanation: dict[str, Any]) -> str:
    if requested != 'auto':
        return requested
    layer = str(explanation.get('likely_layer') or '')
    if layer in {'tool_validation', 'tool_runtime', 'mcp', 'model_provider', 'runtime', 'agent_loop'}:
        return 'bug-fix'
    if layer in {'cleanup_warning', 'success', 'runtime_recovery'}:
        return 'repo-review'
    if layer in {'human_approval', 'human_interrupt', 'guardrail'}:
        return 'release-check'
    return 'bug-fix'


def _fix_context(explanation: dict[str, Any]) -> str:
    return json.dumps(
        {
            'run_id': explanation.get('run_id'),
            'status': explanation.get('status'),
            'likely_layer': explanation.get('likely_layer'),
            'headline': explanation.get('headline'),
            'evidence': explanation.get('evidence', []),
            'recommended_actions': explanation.get('recommended_actions', []),
            'counts': explanation.get('counts', {}),
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _probable_cause(explanation: dict[str, Any]) -> str:
    layer = str(explanation.get('likely_layer') or 'runtime')
    mapping = {
        'success': 'The run succeeded; no fix is required unless the trace shows unexpected retries.',
        'runtime_recovery': 'The run recovered through retry or repair; review whether that fallback should become the normal path.',
        'human_approval': 'The run is blocked on a durable human approval request.',
        'human_interrupt': 'The run was intentionally interrupted at a safe point.',
        'model_provider': 'The configured provider is unavailable or missing required credentials.',
        'tool_validation': 'The model emitted tool arguments that did not match the registered schema.',
        'guardrail': 'A configured guardrail blocked tool input or final output.',
        'tool_runtime': 'A local tool raised an exception during execution.',
        'mcp': 'An MCP transport, catalog, auth, roots, or tool-call path failed.',
        'agent_loop': 'The agent repeated work until it hit the iteration budget.',
        'cleanup_warning': 'Windows asyncio subprocess teardown emitted a known cleanup warning after execution.',
        'runtime': 'The stored trace does not match a specialized classifier; inspect the latest failed event.',
    }
    return mapping.get(layer, mapping['runtime'])


def _fix_commands(run_id: str, explanation: dict[str, Any]) -> list[str]:
    commands = [
        f'easy-agent runs explain {run_id} -c easy-agent.yml',
        f'easy-agent traces open {run_id} -c easy-agent.yml --no-browser',
        'easy-agent connectors doctor -c easy-agent.yml',
    ]
    layer = str(explanation.get('likely_layer') or '')
    if layer == 'human_approval':
        commands.insert(1, 'easy-agent approvals list -c easy-agent.yml')
    if layer == 'mcp':
        commands.append('easy-agent mcp list -c easy-agent.yml')
    if layer in {'model_provider', 'tool_validation', 'agent_loop'}:
        commands.append('easy-agent task show bug-fix --format json')
    return commands


def _safety_notes(explanation: dict[str, Any]) -> list[str]:
    layer = str(explanation.get('likely_layer') or '')
    notes = ['This command is advice-only and does not modify files or rerun the agent.']
    if layer in {'guardrail', 'human_approval'}:
        notes.append('Do not bypass approvals or guardrails without reviewing the recorded payload.')
    if layer == 'mcp':
        notes.append('Check MCP roots and auth before widening filesystem or browser access.')
    if layer == 'model_provider':
        notes.append('Keep provider credentials in environment variables or local env files only.')
    if layer == 'agent_loop':
        notes.append('Prefer tightening prompts or duplicate-call controls before raising max_iterations.')
    return notes
