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

import easycat.server.webrtc_routes as webrtc_module
import easycat.server.websocket as websocket_module
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
        captured["kwargs"] = kwargs

    monkeypatch.setattr(webrtc_module, "run_webrtc_config_server", _fake_run)
    return captured


@pytest.fixture
def captured_websocket(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_run(factory: Any, config: Any = None, **kwargs: Any) -> None:
        captured["factory"] = factory
        captured["config"] = config
        captured["kwargs"] = kwargs

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
    assert config1.enable_echo_cancellation is True


@pytest.mark.parametrize(
    ("mode", "transport_type"),
    [
        ("local", "LocalTransportConfig"),
        ("browser", "WebRTCTransportConfig"),
        ("websocket", "WebSocketTransportConfig"),
        ("twilio", "TwilioTransportConfig"),
        ("telnyx", "TelnyxTransportConfig"),
    ],
)
def test_resolve_config_previews_defaults_without_starting_runtime(
    mode: str,
    transport_type: str,
) -> None:
    app = VoiceApp(agent="my-agent")

    config = app.resolve_config(mode)  # type: ignore[arg-type]

    assert isinstance(config, EasyConfig)
    assert config.agent == "my-agent"
    assert type(config.transport).__name__ == transport_type
    assert type(config.stt).__name__ == "OpenAIRealtimeSTTConfig"
    assert type(config.tts).__name__ == "OpenAITTSConfig"


def test_resolve_config_requires_transport_for_application_factory() -> None:
    app = VoiceApp(config_factory=lambda transport: EasyConfig.browser(transport=transport))

    with pytest.raises(ValueError, match="concrete transport"):
        app.resolve_config("browser")


def test_browser_rejects_static_config() -> None:
    app = VoiceApp(config=EasyConfig.browser(agent="a"))
    with pytest.raises(ValueError) as exc:
        app.run("browser")
    message = str(exc.value)
    assert "config_factory" in message
    assert "per-connection" in message


def test_browser_rejects_live_high_level_collaborator() -> None:
    """A built provider/bridge passed as a high-level field is shared across
    every per-connection ``EasyConfig``; reject it like a static ``config``."""

    class _LiveBridge:
        """Stand-in for a stateful agent bridge (e.g. RemoteResponsesAPIBridge)."""

    app = VoiceApp(agent=_LiveBridge())
    with pytest.raises(ValueError) as exc:
        app.run("browser")
    message = str(exc.value)
    assert "config_factory" in message
    assert "per-connection" in message
    assert "agent" in message


def test_browser_allows_string_and_config_high_level_fields(
    captured_webrtc: dict[str, Any],
) -> None:
    """Provider-name strings and provider-config specs are safe to reuse across
    per-connection sessions, so they pass the live-collaborator guard that runs
    when the per-connection factory is built (a fresh provider is built from
    them each connection)."""
    from easycat.stt.openai_provider import OpenAISTTConfig

    # Building the per-connection factory (where the guard runs) must not raise.
    VoiceApp(agent="a", stt=OpenAISTTConfig()).run("browser")
    assert "factory" in captured_webrtc


@pytest.mark.parametrize("mode", ["browser", "websocket"])
def test_per_connection_modes_allow_named_provider_config_wrappers(
    mode: str,
    captured_webrtc: dict[str, Any],
    captured_websocket: dict[str, Any],
) -> None:
    from easycat.stt.factory import STTProviderConfig
    from easycat.tts.factory import TTSProviderConfig

    app = VoiceApp(
        agent="a",
        stt=STTProviderConfig(provider="openai", api_key="test-key"),
        tts=TTSProviderConfig(provider="openai", api_key="test-key"),
    )

    app.run(mode)  # type: ignore[arg-type]

    captured = captured_webrtc if mode == "browser" else captured_websocket
    assert "factory" in captured


def test_browser_allows_framework_agent_spec(
    captured_webrtc: dict[str, Any],
) -> None:
    """The documented quickstart shape ``VoiceApp(agent=Agent(...)).run("browser")``
    must work: a framework agent *spec* (here the OpenAI Agents SDK ``Agent``) is
    rebuilt into a fresh bridge per session — the bridge, not the wrapped spec,
    owns per-session state — so it is safe to reuse across per-connection
    sessions and the live-collaborator guard must not reject it."""
    agents = pytest.importorskip("agents")

    VoiceApp(agent=agents.Agent(name="assistant", instructions="help")).run("browser")
    assert "factory" in captured_webrtc


def test_browser_rejects_built_agent_bridge() -> None:
    """A built ``ExternalAgentBridge`` carries per-session conversation state and
    is passed through (not rebuilt) per session, so sharing one across
    per-connection sessions must be rejected with the ``config_factory`` remedy."""
    from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

    bridge = RemoteResponsesAPIBridge(base_url="https://api.openai.com", model="gpt-4o")
    app = VoiceApp(agent=bridge)
    with pytest.raises(ValueError) as exc:
        app.run("browser")
    message = str(exc.value)
    assert "config_factory" in message
    assert "per-connection" in message
    assert "agent" in message


def test_browser_allows_registered_extension_stt_config(
    monkeypatch: pytest.MonkeyPatch,
    captured_webrtc: dict[str, Any],
) -> None:
    """A registered third-party STT *config* (outside the built-in
    ``STTConfig`` union) is a spec from which a fresh provider is built per
    session, so it must pass the live-collaborator guard like the built-ins."""
    from dataclasses import dataclass

    from easycat.stt.factory import _CATALOG, register_stt_provider

    # Snapshot/restore the global catalog so the fake provider does not leak.
    snapshot = (
        dict(_CATALOG.providers),
        dict(_CATALOG.env_vars),
        dict(_CATALOG.extras),
        dict(_CATALOG.api_domains),
        dict(_CATALOG.probe_modules),
        dict(_CATALOG.capabilities),
        dict(_CATALOG.capability_resolvers),
        dict(_CATALOG.config_to_provider),
        _CATALOG._discovered,
    )

    @dataclass
    class _ExtensionSTTConfig:
        api_key: str | None = None

    class _ExtensionSTT:
        def __init__(self, config: Any) -> None:
            self.config = config

    try:
        register_stt_provider(
            "extstt", _ExtensionSTT, _ExtensionSTTConfig, env_var="EXTSTT_API_KEY"
        )
        # Building the per-connection factory must not reject the extension config.
        VoiceApp(agent="a", stt=_ExtensionSTTConfig()).run("browser")
        assert "factory" in captured_webrtc
    finally:
        (
            providers,
            env_vars,
            extras,
            api_domains,
            probe_modules,
            capabilities,
            capability_resolvers,
            reverse,
            discovered,
        ) = snapshot
        for attr, restored in (
            ("providers", providers),
            ("env_vars", env_vars),
            ("extras", extras),
            ("api_domains", api_domains),
            ("probe_modules", probe_modules),
            ("capabilities", capabilities),
            ("capability_resolvers", capability_resolvers),
            ("config_to_provider", reverse),
        ):
            current = getattr(_CATALOG, attr)
            current.clear()
            current.update(restored)
        object.__setattr__(_CATALOG, "_discovered", discovered)


def test_announce_browser_url_omits_token_but_keeps_usable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token-protected browser serve must NOT print the token value, but MUST
    tell the operator to append it as ``#token=``. The bundled client reads its
    bearer token from that fragment, so an origin-only hint would open the
    page unauthenticated (→ 401 from ``/config`` and ``/offer``)."""
    from easycat.cli import _output
    from easycat.transports.webrtc import WebRTCTransportConfig

    printed: list[str] = []
    monkeypatch.setattr(
        _output.stdout_console, "print", lambda msg, *a, **k: printed.append(str(msg))
    )

    config = WebRTCTransportConfig(host="127.0.0.1", port=8080, auth_token="a+b&c#d e")
    VoiceApp(agent="a")._announce_browser_url(config)

    assert printed, "expected an announced URL"
    output = "\n".join(printed)
    # (i) The token value must never leak — neither raw nor URL-encoded.
    assert "a+b&c#d e" not in output
    assert "a%2Bb%26c%23d+e" not in output
    # (ii) The append-token instruction must be present and usable.
    assert "#token=" in output
    assert "serve token" in output


def test_announce_browser_url_without_token_prints_plain_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(iii) With no token configured, announce the plain origin URL as before."""
    from easycat.cli import _output
    from easycat.transports.webrtc import WebRTCTransportConfig

    printed: list[str] = []
    monkeypatch.setattr(
        _output.stdout_console, "print", lambda msg, *a, **k: printed.append(str(msg))
    )

    config = WebRTCTransportConfig(host="127.0.0.1", port=8080)
    VoiceApp(agent="a")._announce_browser_url(config)

    assert printed == ["Open http://localhost:8080"]


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
    VoiceApp(agent="a", host="127.0.0.1", port=9001, serve_token="secret").run("browser")
    config = captured_webrtc["config"]
    assert config.port == 9001
    assert config.auth_token == "secret"


def test_browser_default_max_sessions_uses_webrtc_default(
    captured_webrtc: dict[str, Any],
) -> None:
    """Without an explicit limit, the WebRTCTransportConfig default applies."""
    VoiceApp(agent="a").run("browser")
    assert captured_webrtc["config"].max_sessions == 64


def test_browser_forwards_max_sessions_from_construction(
    captured_webrtc: dict[str, Any],
) -> None:
    VoiceApp(agent="a", max_sessions=3).run("browser")
    assert captured_webrtc["config"].max_sessions == 3


def test_browser_forwards_max_sessions_from_run(
    captured_webrtc: dict[str, Any],
) -> None:
    VoiceApp(agent="a").run("browser", max_sessions=5)
    assert captured_webrtc["config"].max_sessions == 5


def test_browser_factory_does_not_leak_server_policy_fields(
    captured_webrtc: dict[str, Any],
) -> None:
    """Server-policy fields land on the transport config, NOT the EasyConfig.

    Invoking the captured per-connection factory is what catches the leak:
    ``EasyConfig.browser`` has no ``host`` / ``port`` / ``serve_token`` field, so
    forwarding any of them would raise ``TypeError`` here (which previously only
    surfaced on the first real connection, after a clean startup).
    """
    VoiceApp(agent="a", host="127.0.0.1", port=9001, serve_token="secret").run("browser")

    # Server-policy fields reached the transport config.
    transport_config = captured_webrtc["config"]
    assert transport_config.port == 9001
    assert transport_config.auth_token == "secret"

    # Invoking the factory must succeed and produce a clean EasyConfig.
    factory = captured_webrtc["factory"]
    config = factory(_FakeWebRTCTransport())
    assert isinstance(config, EasyConfig)
    assert config.agent == "a"
    # None of the server-policy fields leaked into the EasyConfig.
    for leaked in ("host", "port", "serve_token", "max_sessions"):
        assert not hasattr(config, leaked)


def test_websocket_factory_does_not_leak_server_policy_fields(
    captured_websocket: dict[str, Any],
) -> None:
    """The WebSocket per-connection factory must also stay leak-free."""
    VoiceApp(agent="a", host="127.0.0.1", port=9001, serve_token="secret", max_sessions=3).run(
        "websocket"
    )

    server_config = captured_websocket["config"]
    assert server_config.port == 9001
    assert server_config.auth_token == "secret"
    assert server_config.max_sessions == 3

    factory = captured_websocket["factory"]
    config = factory(_FakeWebSocketTransport())
    assert isinstance(config, EasyConfig)
    assert config.agent == "a"
    for leaked in ("host", "port", "auth_token", "max_sessions"):
        assert not hasattr(config, leaked)


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
    VoiceApp(agent="a", host="0.0.0.0", serve_token="tok").run("websocket")
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
    # The flag must reach the WebRTC serve helper too; otherwise its own
    # non-loopback guard would re-reject the unauthenticated bind.
    assert captured_webrtc["kwargs"]["unsafe_allow_no_auth"] is True


def test_unsafe_allow_no_auth_escape_hatch_websocket(
    monkeypatch: pytest.MonkeyPatch, captured_websocket: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    VoiceApp(agent="a", host="0.0.0.0").run("websocket", unsafe_allow_no_auth=True)
    config = captured_websocket["config"]
    assert config.auth_token is None


# ── Blank/whitespace token must not satisfy the guard ────────────────


def test_browser_blank_serve_token_does_not_satisfy_guard(
    monkeypatch: pytest.MonkeyPatch, captured_webrtc: dict[str, Any]
) -> None:
    """A whitespace-only token normalizes to "no token", so the non-loopback
    bind guard must still fire — otherwise ``"   "`` would arm a truthy bind
    while the downstream WS authorizer treats it as unauthenticated."""
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    app = VoiceApp(agent="a", host="0.0.0.0", serve_token="   ")
    with pytest.raises(ValueError) as exc:
        app.run("browser")
    assert "0.0.0.0" in str(exc.value)
    assert captured_webrtc == {}


def test_websocket_blank_serve_token_does_not_satisfy_guard(
    monkeypatch: pytest.MonkeyPatch, captured_websocket: dict[str, Any]
) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    app = VoiceApp(agent="a", host="0.0.0.0", serve_token="   ")
    with pytest.raises(ValueError) as exc:
        app.run("websocket")
    assert "0.0.0.0" in str(exc.value)
    assert captured_websocket == {}


def test_whitespace_env_token_does_not_satisfy_guard(
    monkeypatch: pytest.MonkeyPatch, captured_webrtc: dict[str, Any]
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "   ")
    with pytest.raises(ValueError):
        VoiceApp(agent="a", host="0.0.0.0").run("browser")
    assert captured_webrtc == {}


def test_wrong_server_token_env_var_does_not_satisfy_guard(
    monkeypatch: pytest.MonkeyPatch, captured_webrtc: dict[str, Any]
) -> None:
    """Only ``EASYCAT_SERVE_TOKEN`` arms the guard; the look-alike
    ``EASYCAT_SERVER_TOKEN`` must be ignored (guards against a partial rename)."""
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    monkeypatch.setenv("EASYCAT_SERVER_TOKEN", "wrong-var")
    with pytest.raises(ValueError) as exc:
        VoiceApp(agent="a", host="0.0.0.0").run("browser")
    assert "0.0.0.0" in str(exc.value)
    assert captured_webrtc == {}


# ── serve()/run() announce symmetry ──────────────────────────────────


async def test_serve_browser_accepts_announce_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve('browser', announce=...)`` must be accepted symmetrically with
    ``run('browser', announce=...)`` (it previously raised "unknown kwarg") and,
    like the sync path, VoiceApp must own the announcement line itself instead of
    delegating a misleading origin-only message to the helper."""
    calls: dict[str, Any] = {}
    announced: list[bool] = []

    async def _fake_serve(factory: Any, config: Any = None, **kwargs: Any) -> None:
        calls["kwargs"] = kwargs

    monkeypatch.setattr(webrtc_module, "serve_webrtc_config_sessions", _fake_serve)
    monkeypatch.setattr(
        VoiceApp, "_announce_browser_url", lambda self, cfg: announced.append(True)
    )

    await VoiceApp(agent="a").serve("browser", announce=False)
    assert announced == []
    # The helper's own announce is always suppressed; VoiceApp owns the line.
    assert calls["kwargs"]["announce"] is False

    await VoiceApp(agent="a").serve("browser", announce=True)
    assert announced == [True]
    assert calls["kwargs"]["announce"] is False


async def test_serve_browser_announces_token_safe_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async browser ``serve()`` with a serve token must announce a usable hint:
    the token value never appears, but the operator is told to append it as
    ``#token=`` so following the hint does not open the page unauthenticated."""
    from easycat.cli import _output

    printed: list[str] = []
    monkeypatch.setattr(
        _output.stdout_console, "print", lambda msg, *a, **k: printed.append(str(msg))
    )

    calls: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any = None, **kwargs: Any) -> None:
        calls["config"] = config
        calls["kwargs"] = kwargs

    monkeypatch.setattr(webrtc_module, "serve_webrtc_config_sessions", _fake_serve)

    await VoiceApp(agent="a", serve_token="s3+cr et&x").serve("browser")

    output = "\n".join(printed)
    assert calls["config"].auth_token == "s3+cr et&x"
    # VoiceApp owns the announcement; the helper's origin-only line stays off.
    assert calls["kwargs"]["announce"] is False
    # (i) the token value is never printed; (ii) the append hint is present.
    assert "s3+cr et&x" not in output
    assert "#token=" in output
    assert "serve token" in output


# ── Fail-loud on a misspelled server kwarg ───────────────────────────


def test_browser_run_rejects_unknown_kwarg(captured_webrtc: dict[str, Any]) -> None:
    """A typo'd ``port``/``serve_token`` must raise, not silently bind the
    default or run unauthenticated."""
    with pytest.raises(ValueError, match="Unknown keyword argument"):
        VoiceApp(agent="a").run("browser", prot=9000)
    # The guard fires before the serve helper, so it is never reached.
    assert "factory" not in captured_webrtc


def test_websocket_run_rejects_unknown_kwarg(captured_websocket: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Unknown keyword argument"):
        VoiceApp(agent="a").run("websocket", serve_tokn="secret")
    assert "factory" not in captured_websocket
