from __future__ import annotations

import io

import pytest
from rich.console import Console

from agent_cli.shared import build_cli_inline_resolver
from agent_common.models import HumanRequest, HumanRequestStatus


@pytest.mark.asyncio
async def test_cli_inline_resolver_maps_url_cancel_to_cancelled_status() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    answers = iter(['cancel'])

    def _input(
        prompt: str = '',
        *,
        markup: bool = True,
        emoji: bool = True,
        password: bool = False,
        stream: io.TextIOBase | None = None,
    ) -> str:
        del prompt, markup, emoji, password, stream
        return next(answers)

    console.input = _input  # type: ignore[assignment]
    resolver = build_cli_inline_resolver(console)
    request = HumanRequest(
        request_id='req-1',
        run_id='run-1',
        request_key='mcp_elicitation:remote:key',
        kind='mcp_elicitation',
        status=HumanRequestStatus.PENDING,
        title='Approve login',
        payload={
            'server': 'remote',
            'mode': 'url',
            'url': 'https://example.com/oauth/start',
            'elicitation_id': 'eli-1',
        },
        response_payload=None,
        created_at='2026-04-10T00:00:00+00:00',
        resolved_at=None,
    )

    status, payload = await resolver(request)

    assert status is HumanRequestStatus.CANCELLED
    assert payload == {'action': 'cancel'}
