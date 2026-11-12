"""Construction-time validation for :class:`VoiceServerConfig`."""

from __future__ import annotations

import pytest

from easycat.server import VoiceServerConfig


@pytest.mark.parametrize("port", [-1, 65536, 70000, True, "8080", 8080.0])
def test_rejects_invalid_port(port: object) -> None:
    with pytest.raises(ValueError, match="port"):
        VoiceServerConfig(port=port)  # type: ignore[arg-type]


def test_rejects_top_port_when_websocket_listener_needs_port_plus_one() -> None:
    with pytest.raises(ValueError, match=r"port \+ 1"):
        VoiceServerConfig(port=65535)


@pytest.mark.parametrize(
    "kwargs",
    [{"port": 0}, {"port": 65534}, {"port": 65535, "enable_websocket": False}],
)
def test_accepts_valid_port(kwargs: dict[str, object]) -> None:
    config = VoiceServerConfig(**kwargs)  # type: ignore[arg-type]
    assert config.port == kwargs["port"]
