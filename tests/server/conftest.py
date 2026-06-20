"""Shared collection guard for the ``VoiceServer`` test package.

Every ``integration_socket`` test here drives a real :class:`VoiceServer`, which
binds aiohttp-backed HTTP/WS listeners — so each one needs the optional
``aiohttp`` dependency. Project CI's socket lane installs only ``--group dev``
(no extras), so without this guard those tests ``ModuleNotFoundError`` at
runtime instead of skipping. Mirror the per-file ``skipif`` used in
``test_webrtc_routes.py``, but scope it to the socket tests so the package's
pure-logic unit tests (capacity gate, config, lifecycle state) still run in the
no-extras quick lane.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
_SERVER_TESTS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # ``pytest_collection_modifyitems`` receives the WHOLE session's items even
    # from a subpackage conftest, so scope the skip to this package's tests only
    # (other ``integration_socket`` suites, e.g. the websocket transport, run on
    # base deps and must not be skipped).
    if _HAS_AIOHTTP:
        return
    skip_aiohttp = pytest.mark.skip(reason="aiohttp not installed")
    for item in items:
        if "integration_socket" not in item.keywords:
            continue
        if _SERVER_TESTS_DIR in item.path.parents:
            item.add_marker(skip_aiohttp)
