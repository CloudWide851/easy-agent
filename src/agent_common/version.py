from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = 'easy-agent'
DEFAULT_VERSION = '0.3.5'


def runtime_version() -> str:
    try:
        discovered = version(PACKAGE_NAME)
    except PackageNotFoundError:
        return DEFAULT_VERSION
    return DEFAULT_VERSION if discovered != DEFAULT_VERSION else discovered
