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

    trace = store.load_trace('run_3')

    assert resolved.status is HumanRequestStatus.APPROVED
    assert resolved.response_payload == {'approved_by': 'tester'}
    assert first_interrupt == {'reason': 'pause'}
    assert second_interrupt is None
    assert store.load_oauth_client_info('remote') == {'client_id': 'abc'}
    assert store.load_oauth_tokens('remote') == {'access_token': 'secret-token'}
    assert trace['human_requests'][0]['status'] == HumanRequestStatus.APPROVED


def test_sqlite_run_store_tracks_workbench_and_federated_tasks(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_workbench_session(
        session_id='wb-1',
        owner_run_id='run-4',
        name='skill-echo',
        root_path=str(tmp_path / 'workbench' / 'wb-1'),
        executor_name='process',
        metadata={'kind': 'skill'},
        expires_at='2099-01-01T00:00:00+00:00',
    )
    store.record_workbench_execution(
        session_id='wb-1',
        command=['python', '-c', "print('ok')"],
        returncode=0,
        stdout='ok',
        stderr='',
    )
    store.create_federated_task('task-1', 'agent_export', 'agent', 'queued', {'input': 'hello'})
    store.update_federated_task('task-1', status='succeeded', response_payload={'result': 'done'}, local_run_id='run-4')

    workbench = store.load_workbench_session('wb-1')
    federated = store.load_federated_task('task-1')

    assert workbench['name'] == 'skill-echo'
    assert store.list_workbench_sessions(owner_run_id='run-4')[0]['session_id'] == 'wb-1'
    assert federated['status'] == 'succeeded'
    assert federated['response_payload'] == {'result': 'done'}
    assert federated['local_run_id'] == 'run-4'
