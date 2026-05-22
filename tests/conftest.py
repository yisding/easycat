"""Global pytest configuration for integration test capability gating."""

from __future__ import annotations

import socket

import pytest

from tests._marker_lint import validate_flaky_marker, validate_provider_surface_markers


def _can_bind_localhost() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return True
    except OSError:
        return False


_HAS_LOCALHOST_SOCKET_ACCESS = _can_bind_localhost()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marker_errors: list[str] = []
    for item in items:
        marker_names = {marker.name for marker in item.iter_markers()}
        marker_errors.extend(validate_provider_surface_markers(item.nodeid, marker_names))
        flaky_marker = item.get_closest_marker("flaky")
        marker_errors.extend(
            validate_flaky_marker(
                item.nodeid,
                marker_names,
                flaky_marker.kwargs if flaky_marker is not None else {},
            )
        )

    if marker_errors:
        errors = "\n- ".join(marker_errors)
        raise pytest.UsageError(f"validation marker metadata errors:\n- {errors}")

    if _HAS_LOCALHOST_SOCKET_ACCESS:
        return

    skip_socket = pytest.mark.skip(
        reason="integration_socket tests require localhost socket access in this environment"
    )
    for item in items:
        if item.get_closest_marker("integration_socket") is not None:
            item.add_marker(skip_socket)
