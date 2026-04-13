from pathlib import Path

from agent_common.models import ChatMessage, HumanRequestStatus
from agent_integrations.storage import SQLiteRunStore


def test_sqlite_run_store_persists_trace(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_run('run_1', 'baseline', {'input': 'hello'})
    store.record_node('run_1', 'node_1', 'succeeded', 1, {'value': 1}, None)
    store.record_event('run_1', 'custom', {'value': 2}, scope='agent', node_id='node_1', span_id='span-1')
    store.finish_run('run_1', 'succeeded', {'result': 'ok'})

    trace = store.load_trace('run_1')

    assert trace['status'] == 'succeeded'
    assert trace['run_kind'] == 'graph'
    assert trace['nodes'][0]['node_id'] == 'node_1'
    assert trace['events'][0]['kind'] == 'custom'
    assert trace['events'][0]['scope'] == 'agent'
    assert trace['events'][0]['node_id'] == 'node_1'
    assert trace['events'][0]['sequence'] == 1



def test_sqlite_run_store_persists_session_memory_and_checkpoints(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    messages = [
        ChatMessage(role='user', content='hello'),
        ChatMessage(role='assistant', content='world'),
    ]

    store.create_run('run_2', 'baseline', {'input': 'hello'}, session_id='session-a')
    store.save_session_messages('session-a', 'baseline', messages)
    store.save_session_state('session-a', 'baseline', {'input': 'hello', 'node_a': {'value': 1}})
    store.save_harness_state('session-a', 'delivery_loop', {'status': 'running', 'cycle_index': 2})
    store.create_checkpoint('run_2', 'graph', {'results': {'node_a': {'value': 1}}, 'remaining': ['node_b']})

    run_payload = store.load_run('run_2')
    restored_messages = store.load_session_messages('session-a')
    restored_state = store.load_session_state('session-a')
    restored_harness_state = store.load_harness_state('session-a', 'delivery_loop')
    checkpoint = store.load_latest_checkpoint('run_2')
    trace = store.load_trace('run_2')

    assert run_payload['session_id'] == 'session-a'
    assert run_payload['run_kind'] == 'graph'
    assert [message.content for message in restored_messages] == ['hello', 'world']
    assert restored_state['node_a']['value'] == 1
    assert restored_harness_state['cycle_index'] == 2
    assert checkpoint is not None
    assert checkpoint['kind'] == 'graph'
    assert checkpoint['payload']['remaining'] == ['node_b']
    assert trace['session_id'] == 'session-a'
    assert trace['checkpoints'][0]['kind'] == 'graph'


def test_sqlite_run_store_tracks_human_requests_interrupts_and_oauth_state(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_run('run_3', 'baseline', {'input': 'approve'})

    request = store.create_human_request('run_3', 'tool:echo', 'tool', 'Approve echo', {'tool_name': 'python_echo'})
    pending = store.load_human_request_by_key('run_3', 'tool:echo')
    requests = store.list_human_requests(run_id='run_3')

    assert pending is not None
    assert pending.request_id == request.request_id
    assert requests[0].status is HumanRequestStatus.PENDING

    resolved = store.resolve_human_request(request.request_id, status=HumanRequestStatus.APPROVED, response_payload={'approved_by': 'tester'})
    store.request_interrupt('run_3', {'reason': 'pause'})
    first_interrupt = store.consume_interrupt('run_3')
    second_interrupt = store.consume_interrupt('run_3')
    store.save_oauth_client_info('remote', {'client_id': 'abc'})
    store.save_oauth_tokens('remote', {'access_token': 'secret-token'})
    store.save_federation_auth_state(
        'federation-remote',
        tokens={'access_token': 'fed-token'},
        metadata={'token_endpoint': 'https://issuer.example/token'},
        jwks={'keys': []},
    )

    trace = store.load_trace('run_3')

    assert resolved.status is HumanRequestStatus.APPROVED
    assert resolved.response_payload == {'approved_by': 'tester'}
    assert first_interrupt == {'reason': 'pause'}
    assert second_interrupt is None
    assert store.load_oauth_client_info('remote') == {'client_id': 'abc'}
    assert store.load_oauth_tokens('remote') == {'access_token': 'secret-token'}
    assert store.load_federation_auth_state('federation-remote') is not None
    assert trace['human_requests'][0]['status'] == HumanRequestStatus.APPROVED


def test_sqlite_run_store_updates_mcp_requests_and_root_snapshots(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_run('run-mcp', 'baseline', {'input': 'hello'})
    request = store.create_human_request(
        'run-mcp',
        'mcp_elicitation:remote:stable-key',
        'mcp_elicitation',
        'Approve remote login',
        {
            'server': 'remote',
            'mode': 'url',
            'elicitation_id': 'eli-1',
            'url': 'https://example.com/oauth/start',
        },
    )
    store.resolve_human_request(
        request.request_id,
        status=HumanRequestStatus.APPROVED,
        response_payload={'action': 'accept', 'completion': {'status': 'pending', 'elicitation_id': 'eli-1'}},
    )
    store.update_human_request_response(
        request.request_id,
        {'action': 'accept', 'completion': {'status': 'completed', 'elicitation_id': 'eli-1'}},
    )
    store.save_mcp_root_snapshot(
        'filesystem',
        [{'path': 'C:/work', 'name': 'work', 'uri': 'file:///C:/work'}],
        last_notified_at='2026-04-10T00:00:00+00:00',
    )
    store.save_mcp_catalog_snapshot(
        'remote',
        'resources',
        [{'uri': 'file:///notes.txt', 'name': 'notes'}],
        last_notified_at='2026-04-10T00:05:00+00:00',
    )
    store.save_mcp_resource_subscription(
        'remote',
        'file:///notes.txt',
        status='active',
        subscription={'uri': 'file:///notes.txt', 'status': 'active'},
    )

    updated = store.load_human_request(request.request_id)
    located = store.find_mcp_elicitation_request('remote', 'eli-1')
    snapshot = store.load_mcp_root_snapshot('filesystem')
    catalog_snapshot = store.load_mcp_catalog_snapshot('remote', 'resources')
    subscription = store.load_mcp_resource_subscription('remote', 'file:///notes.txt')

    assert updated.response_payload == {'action': 'accept', 'completion': {'status': 'completed', 'elicitation_id': 'eli-1'}}
    assert located is not None
    assert located.request_id == request.request_id
    assert snapshot is not None
    assert snapshot['roots'][0]['uri'] == 'file:///C:/work'
    assert snapshot['last_notified_at'] == '2026-04-10T00:00:00+00:00'
    assert catalog_snapshot is not None
    assert catalog_snapshot['entries'][0]['uri'] == 'file:///notes.txt'
    assert catalog_snapshot['last_notified_at'] == '2026-04-10T00:05:00+00:00'
    assert subscription is not None
    assert subscription['status'] == 'active'


def test_sqlite_run_store_tracks_workbench_and_federated_tasks(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_workbench_session(
        session_id='wb-1',
        owner_run_id='run-4',
        name='skill-echo',
        root_path=str(tmp_path / 'workbench' / 'wb-1'),
        executor_name='process',
        metadata={'kind': 'skill'},
        runtime_state={'status': 'running'},
        expires_at='2099-01-01T00:00:00+00:00',
    )
    store.record_workbench_execution(
        session_id='wb-1',
        command=['python', '-c', "print('ok')"],
        returncode=0,
        stdout='ok',
        stderr='',
    )
    store.create_federated_task(
        'task-1',
        'agent_export',
        'agent',
        'queued',
        {'input': 'hello'},
        tenant_id='tenant-a',
        subject_id='user-a',
        task_scope=['task-1'],
    )
    store.update_federated_task('task-1', status='succeeded', response_payload={'result': 'done'}, local_run_id='run-4')

    workbench = store.load_workbench_session('wb-1')
    federated = store.load_federated_task('task-1')

    assert workbench['name'] == 'skill-echo'
    assert workbench['runtime_state']['status'] == 'running'
    assert workbench['runtime_state']['status'] == 'running'
    assert store.list_workbench_sessions(owner_run_id='run-4')[0]['session_id'] == 'wb-1'
    assert federated['status'] == 'succeeded'
    assert federated['response_payload'] == {'result': 'done'}
    assert federated['local_run_id'] == 'run-4'
    assert federated['tenant_id'] == 'tenant-a'
    assert federated['subject_id'] == 'user-a'
    assert federated['task_scope'] == ['task-1']


def test_sqlite_run_store_tracks_federated_events_and_subscriptions(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_federated_task('task-2', 'agent_export', 'agent', 'queued', {'input': 'hello'}, tenant_id='tenant-b', subject_id='user-b')
    event = store.create_federated_task_event('task-2', 'task_queued', {'task': {'task_id': 'task-2', 'status': 'queued'}})
    store.create_federated_subscription(
        subscription_id='sub-1',
        task_id='task-2',
        mode='webhook',
        callback_url='http://127.0.0.1:9999/callback',
        status='active',
        tenant_id='tenant-b',
        subject_id='user-b',
        lease_expires_at='2099-01-01T00:00:00+00:00',
        from_sequence=event['sequence'],
    )
    store.update_federated_subscription(
        'sub-1',
        last_delivered_sequence=event['sequence'],
        delivery_attempts=1,
        last_error='temporary failure',
        next_retry_at='2099-01-01T00:01:00+00:00',
    )

    events = store.list_federated_task_events('task-2')
    subscription = store.load_federated_subscription('sub-1')

    assert events[0]['event_kind'] == 'task_queued'
    assert events[0]['payload']['task']['status'] == 'queued'
    assert subscription['last_delivered_sequence'] == event['sequence']
    assert subscription['delivery_attempts'] == 1
    assert subscription['last_error'] == 'temporary failure'
    assert subscription['tenant_id'] == 'tenant-b'
    assert subscription['subject_id'] == 'user-b'
