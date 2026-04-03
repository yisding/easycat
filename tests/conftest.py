"""Global pytest configuration for integration test capability gating."""

from __future__ import annotations

import socket

import pytest


def _can_bind_localhost() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return True
    except OSError:
        return False


_HAS_LOCALHOST_SOCKET_ACCESS = _can_bind_localhost()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _HAS_LOCALHOST_SOCKET_ACCESS:
        return

    skip_socket = pytest.mark.skip(
        reason="integration_socket tests require localhost socket access in this environment"
    )
    for item in items:
        if item.get_closest_marker("integration_socket") is not None:
            item.add_marker(skip_socket)
