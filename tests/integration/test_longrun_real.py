from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from agent_runtime.longrun import run_longrun_suite


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = [pytest.mark.real]


@pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'), reason='requires DEEPSEEK_API_KEY')
@pytest.mark.skipif(not os.environ.get('PG_PASSWORD'), reason='requires PG_PASSWORD')
@pytest.mark.skipif(not _port_open('127.0.0.1', 6379), reason='requires local Redis on 127.0.0.1:6379')
@pytest.mark.skipif(not _port_open('127.0.0.1', 5432), reason='requires local PostgreSQL on 127.0.0.1:5432')
def test_real_longrun_suite_executes_all_modes(tmp_path: Path) -> None:
    output_root = Path(tempfile.gettempdir()) / 'easy-agent-longrun-real' / tmp_path.name
    if output_root.exists():
        shutil.rmtree(output_root, ignore_errors=True)
    report = run_longrun_suite('configs/longrun.example.yml', cycles=1, output_root=output_root)
    if any(summary['failures'] > 0 for summary in report['summary'].values()):
        shutil.rmtree(output_root, ignore_errors=True)
        report = run_longrun_suite('configs/longrun.example.yml', cycles=1, output_root=output_root)

    assert set(report['summary']) == {'single_agent', 'sub_agent', 'multi_agent_graph'}
    assert all(summary['failures'] == 0 for summary in report['summary'].values())
