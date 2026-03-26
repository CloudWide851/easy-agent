from pathlib import Path

from agent_common.models import ChatMessage
from agent_integrations.storage import SQLiteRunStore


def test_sqlite_run_store_persists_trace(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path, 'state.db')
    store.create_run('run_1', 'baseline', {'input': 'hello'})
    store.record_node('run_1', 'node_1', 'succeeded', 1, {'value': 1}, None)
    store.record_event('run_1', 'custom', {'value': 2}, scope='agent', node_id='node_1', span_id='span-1')
    store.finish_run('run_1', 'succeeded', {'result': 'ok'})

    trace = store.load_trace('run_1')

    assert trace['status'] == 'succeeded'
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
    store.create_checkpoint('run_2', 'graph', {'results': {'node_a': {'value': 1}}, 'remaining': ['node_b']})

    run_payload = store.load_run('run_2')
    restored_messages = store.load_session_messages('session-a')
    restored_state = store.load_session_state('session-a')
    checkpoint = store.load_latest_checkpoint('run_2')
    trace = store.load_trace('run_2')

    assert run_payload['session_id'] == 'session-a'
    assert [message.content for message in restored_messages] == ['hello', 'world']
    assert restored_state['node_a']['value'] == 1
    assert checkpoint is not None
    assert checkpoint['kind'] == 'graph'
    assert checkpoint['payload']['remaining'] == ['node_b']
    assert trace['session_id'] == 'session-a'
    assert trace['checkpoints'][0]['kind'] == 'graph'
