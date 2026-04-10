from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

from agent_config.app import load_config
from agent_runtime import build_runtime_from_config

pytestmark = [pytest.mark.real]


@pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'), reason='requires DEEPSEEK_API_KEY')
def test_real_harness_run_completes_and_writes_artifacts(tmp_path: Path) -> None:
    output_root = Path(tempfile.gettempdir()) / 'easy-agent-harness-real' / tmp_path.name
    if output_root.exists():
        shutil.rmtree(output_root, ignore_errors=True)
    last_error: Exception | None = None
    result: dict[str, Any] | None = None

    for _ in range(2):
        config = load_config('configs/harness.example.yml')
        config.storage.path = str(output_root / 'state')
        config.harnesses[0].artifacts_dir = str(output_root / 'artifacts')
        runtime = build_runtime_from_config(config)

        async def _run(current_runtime: Any = runtime) -> dict[str, Any]:
            try:
                return cast(
                    dict[str, Any],
                    await current_runtime.run_harness(
                        'delivery_loop',
                        'Create a concise completion note for this repository.',
                    ),
                )
            finally:
                await current_runtime.aclose()

        try:
            result = asyncio.run(_run())
            break
        except RuntimeError as exc:
            last_error = exc
            if output_root.exists():
                shutil.rmtree(output_root, ignore_errors=True)
    if result is None:
        assert last_error is not None
        raise last_error

    assert result['result']['status'] == 'succeeded'
    assert result['result']['cycles_completed'] >= 1
    assert Path(result['result']['features_path']).is_file()
    assert Path(result['result']['progress_path']).is_file()
