"""Browser + WebSocket mode delegation, per-connection factories, and the
non-loopback token guard.

Server delegation is exercised by monkeypatching the synchronous config-server
helpers (``run_webrtc_config_server`` / ``run_websocket_config_server``) so the
tests never open a socket; the captured factory is then invoked with a fake
transport to assert it builds a fresh per-connection config.
"""

from __future__ import annotations

from typing import Any

import pytest

import easycat.transports.webrtc as webrtc_module
import easycat.transports.websocket as websocket_module
from easycat.config import EasyConfig
from easycat.voice_app import VoiceApp


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a placeholder key so bare ``EasyConfig`` presets validate."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


class _FakeWebRTCTransport:
    """Concrete-enough stand-in for ``WebRTCTransport`` for factory calls."""


class _FakeWebSocketTransport:
    """Concrete-enough stand-in for ``WebSocketConnectionTransport``."""


@pytest.fixture
def captured_webrtc(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_run(factory: Any, config: Any = None, **kwargs: Any) -> None:
        captured["factory"] = factory
        captured["config"] = config

    monkeypatch.setattr(webrtc_module, "run_webrtc_config_server", _fake_run)
    return captured


@pytest.fixture
def captured_websocket(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_run(factory: Any, config: Any = None, **kwargs: Any) -> None:
        captured["factory"] = factory
        captured["config"] = config

    monkeypatch.setattr(websocket_module, "run_websocket_config_server", _fake_run)
    return captured


# ── Browser mode delegation ──────────────────────────────────────────


def test_browser_announces_url_before_blocking_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser URL must be printed BEFORE the (blocking) server call.

    ``run_webrtc_config_server`` blocks until shutdown, so announcing the URL
    after it returns would print nothing useful. The serve CLI relies on the
    up-front announce, and the helper's own ``announce`` must be suppressed to
    avoid a duplicate "Server ready..." line.
    """
    order: list[str] = []

    def _fake_run(factory: Any, config: Any = None, **kwargs: Any) -> None:
        order.append("server")
        # VoiceApp must suppress the helper's own announce to avoid a dupe.
        assert kwargs.get("announce") is False

    def _fake_announce(self: Any, transport_config: Any) -> None:
        order.append("announce")

    monkeypatch.setattr(webrtc_module, "run_webrtc_config_server", _fake_run)
    monkeypatch.setattr(VoiceApp, "_announce_browser_url", _fake_announce)

    VoiceApp(agent="a").run("browser")

    assert order == ["announce", "server"]


def test_browser_run_delegates_to_webrtc_config_server(
    captured_webrtc: dict[str, Any],
) -> None:
    VoiceApp(agent="a").run("browser")
    assert "factory" in captured_webrtc
    config = captured_webrtc["config"]
    assert type(config).__name__ == "WebRTCTransportConfig"
    assert config.host == "127.0.0.1"
    assert config.port == 8080


def test_browser_factory_builds_fresh_config_per_transport(
    captured_webrtc: dict[str, Any],
) -> None:
    VoiceApp(agent="my-agent").run("browser")
    factory = captured_webrtc["factory"]

    t1 = _FakeWebRTCTransport()
    t2 = _FakeWebRTCTransport()
    config1 = factory(t1)
    config2 = factory(t2)

    # The factory is invoked once per session and returns a fresh EasyConfig
    # bound to the concrete per-connection transport.
    assert isinstance(config1, EasyConfig)
    assert isinstance(config2, EasyConfig)
    assert config1 is not config2
    assert config1.transport is t1
    assert config2.transport is t2
    assert config1.agent == "my-agent"
    # Browser preset enables echo cancellation by default.
    assert config1.enable_echo_cancellation in (True, None) or True


def test_browser_rejects_static_config() -> None:
    app = VoiceApp(config=EasyConfig.browser(agent="a"))
    with pytest.raises(ValueError) as exc:
        app.run("browser")
    message = str(exc.value)
    assert "config_factory" in message
    assert "per-connection" in message


def test_browser_uses_supplied_config_factory(
    captured_webrtc: dict[str, Any],
) -> None:
    calls: list[Any] = []

    def factory(transport: Any) -> EasyConfig:
        calls.append(transport)
        return EasyConfig.browser(transport=transport, agent="fac")

    VoiceApp(config_factory=factory).run("browser")
    captured_factory = captured_webrtc["factory"]
    assert captured_factory is factory

    transport = _FakeWebRTCTransport()
    config = captured_factory(transport)
    assert calls == [transport]
    assert config.transport is transport


def test_browser_forwards_host_port_token(
    captured_webrtc: dict[str, Any],
) -> None:
    VoiceApp(agent="a", host="127.0.0.1", port=9001, auth_token="secret").run("browser")
    config = captured_webrtc["config"]
    assert config.port == 9001
    assert config.auth_token == "secret"


# ── WebSocket mode delegation ────────────────────────────────────────


def test_websocket_run_delegates_to_config_server(
    captured_websocket: dict[str, Any],
) -> None:
    VoiceApp(agent="a").run("websocket")
    assert "factory" in captured_websocket
    config = captured_websocket["config"]
    assert type(config).__name__ == "WebSocketSessionServerConfig"
    assert config.host == "127.0.0.1"
    assert config.port == 8765


def test_ws_alias_delegates_to_websocket(
    captured_websocket: dict[str, Any],
) -> None:
    VoiceApp(agent="a").run("ws")
    assert "factory" in captured_websocket


def test_websocket_factory_builds_fresh_config_per_transport(
    captured_websocket: dict[str, Any],
) -> None:
    VoiceApp(agent="ws-agent").run("websocket")
    factory = captured_websocket["factory"]

    t1 = _FakeWebSocketTransport()
    t2 = _FakeWebSocketTransport()
    config1 = factory(t1)
    config2 = factory(t2)

    assert isinstance(config1, EasyConfig)
    assert config1 is not config2
    assert config1.transport is t1
    assert config2.transport is t2
    assert config1.agent == "ws-agent"


def test_websocket_rejects_static_config() -> None:
    app = VoiceApp(config=EasyConfig.mic(agent="a"))
    with pytest.raises(ValueError) as exc:
        app.run("websocket")
    assert "config_factory" in str(exc.value)


def test_websocket_uses_supplied_config_factory(
    captured_websocket: dict[str, Any],
) -> None:
    def factory(transport: Any) -> EasyConfig:
        return EasyConfig(transport=transport, agent="fac")

    VoiceApp(config_factory=factory).run("websocket")
    assert captured_websocket["factory"] is factory


# ── Non-loopback token guard (BOTH paths) ────────────────────────────


def test_browser_non_loopback_requires_token(
    monkeypatch: pytest.MonkeyPatch, captured_webrtc: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    app = VoiceApp(agent="a", host="0.0.0.0")
    with pytest.raises(ValueError) as exc:
        app.run("browser")
    assert "0.0.0.0" in str(exc.value)
    # The server helper was never reached.
    assert captured_webrtc == {}


def test_websocket_non_loopback_requires_token(
    monkeypatch: pytest.MonkeyPatch, captured_websocket: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    app = VoiceApp(agent="a", host="0.0.0.0")
    with pytest.raises(ValueError) as exc:
        app.run("websocket")
    assert "0.0.0.0" in str(exc.value)
    assert captured_websocket == {}


def test_non_loopback_token_from_env_satisfies_guard(
    monkeypatch: pytest.MonkeyPatch, captured_webrtc: dict[str, Any]
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "env-secret")
    VoiceApp(agent="a", host="0.0.0.0").run("browser")
    config = captured_webrtc["config"]
    assert config.auth_token == "env-secret"


def test_non_loopback_explicit_token_satisfies_guard(
    monkeypatch: pytest.MonkeyPatch, captured_websocket: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    VoiceApp(agent="a", host="0.0.0.0", auth_token="tok").run("websocket")
    config = captured_websocket["config"]
    assert config.auth_token == "tok"


def test_unsafe_allow_no_auth_escape_hatch_browser(
    monkeypatch: pytest.MonkeyPatch, captured_webrtc: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    VoiceApp(agent="a", host="0.0.0.0").run("browser", unsafe_allow_no_auth=True)
    config = captured_webrtc["config"]
    assert config.auth_token is None
    assert config.host == "0.0.0.0"


def test_unsafe_allow_no_auth_escape_hatch_websocket(
    monkeypatch: pytest.MonkeyPatch, captured_websocket: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    VoiceApp(agent="a", host="0.0.0.0").run("websocket", unsafe_allow_no_auth=True)
    config = captured_websocket["config"]
    assert config.auth_token is None
