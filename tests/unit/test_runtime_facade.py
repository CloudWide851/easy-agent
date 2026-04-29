from __future__ import annotations

from pathlib import Path

from agent_runtime import AgentApp


def test_agent_app_runs_mock_config(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
graph:
  entrypoint: assistant
  agents:
    - name: assistant
      system_prompt: Reply concisely.
      max_iterations: 2
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )

    app = AgentApp.from_config(config_path)
    try:
        result = app.run('hello from facade')
    finally:
        app.close()

    assert result['status'] == 'succeeded'
    assert 'hello from facade' in str(result['result'])
