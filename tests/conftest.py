from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

_TEST_TEMP_ROOT = Path(tempfile.gettempdir()) / 'easy-agent-pytest' / 'repo-tests'


@pytest.fixture(scope='session', autouse=True)
def _session_temp_root() -> Path:
    _TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ['TMP'] = str(_TEST_TEMP_ROOT)
    os.environ['TEMP'] = str(_TEST_TEMP_ROOT)
    os.environ['TMPDIR'] = str(_TEST_TEMP_ROOT)
    tempfile.tempdir = str(_TEST_TEMP_ROOT)
    return _TEST_TEMP_ROOT


@pytest.fixture
def tmp_path(_session_temp_root: Path) -> Path:
    path = _session_temp_root / f'test-{uuid4().hex}'
    path.mkdir(parents=True, exist_ok=False)
    return path
