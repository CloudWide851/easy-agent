from __future__ import annotations

from agent_common.models import HumanRequest, HumanRequestStatus
from agent_integrations.human_loop import normalize_human_request_resolution


def test_normalize_human_request_resolution_sets_url_completion_pending() -> None:
    request = HumanRequest(
        request_id='req-1',
        run_id='run-1',
        request_key='mcp_elicitation:remote:key',
        kind='mcp_elicitation',
        status=HumanRequestStatus.PENDING,
        title='Approve login',
        payload={'server': 'remote', 'mode': 'url', 'elicitation_id': 'eli-1'},
        response_payload=None,
        created_at='2026-04-10T00:00:00+00:00',
        resolved_at=None,
    )

    payload = normalize_human_request_resolution(
        request,
        status=HumanRequestStatus.APPROVED,
        response_payload={'action': 'accept'},
    )

    assert payload == {'action': 'accept', 'completion': {'status': 'pending', 'elicitation_id': 'eli-1'}}


def test_normalize_human_request_resolution_maps_cancel_to_cancel_action() -> None:
    request = HumanRequest(
        request_id='req-2',
        run_id='run-2',
        request_key='mcp_elicitation:remote:key',
        kind='mcp_elicitation',
        status=HumanRequestStatus.PENDING,
        title='Approve login',
        payload={'server': 'remote', 'mode': 'url', 'elicitation_id': 'eli-2'},
        response_payload=None,
        created_at='2026-04-10T00:00:00+00:00',
        resolved_at=None,
    )

    payload = normalize_human_request_resolution(
        request,
        status=HumanRequestStatus.CANCELLED,
        response_payload={'ignored': True},
    )

    assert payload == {'action': 'cancel'}
