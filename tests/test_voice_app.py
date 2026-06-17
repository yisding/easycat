"""Construction, allow-list, mutual-exclusion, aliases, and local delegation.

These tests exercise ``VoiceApp`` without importing provider SDKs: local
delegation is verified by monkeypatching ``create_session`` and ``run_session``
rather than by wiring real providers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import easycat.config as config_module
import easycat.helpers as helpers_module
from easycat.config import EasyConfig
from easycat.voice_app import VoiceApp


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let bare ``EasyConfig`` presets validate without a real key check.

    The default OpenAI realtime chain only needs *a* key present to pass
    ``EasyConfig`` validation; these tests never reach a real API.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


class _FakeSession:
    """Stand-in for a :class:`Session` returned by ``create_session``."""

    def __init__(self, config: Any) -> None:
        self.config = config


class _LifecycleSession:
    """Fake session that counts ``start``/``stop`` calls for lifecycle tests.

    ``stop`` mirrors the real :meth:`easycat.session.Session.stop`: it is
    idempotent, returning early once the session is closed. ``stop_calls``
    counts every invocation; ``effective_stops`` counts only the calls that
    actually tore the session down.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.start_calls = 0
        self.stop_calls = 0
        self.effective_stops = 0
        self._closed = False

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self, *, force: bool = False) -> None:
        self.stop_calls += 1
        if self._closed:
            return
        self._closed = True
        self.effective_stops += 1

    async def __aenter__(self) -> _LifecycleSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop(force=True)


@pytest.fixture
def fake_create_session(monkeypatch: pytest.MonkeyPatch) -> list[EasyConfig]:
    """Record configs passed to ``create_session`` and return fake sessions."""
    seen: list[EasyConfig] = []

    def _create(config: EasyConfig) -> _FakeSession:
        seen.append(config)
        return _FakeSession(config)

    monkeypatch.setattr(config_module, "create_session", _create)
    return seen


# ── Construction: allow-list ─────────────────────────────────────────


def test_allow_list_accepts_known_high_level_fields() -> None:
    app = VoiceApp(agent="agent-obj", stt="openai/realtime", tts="openai")
    assert app._config_kwargs == {
        "agent": "agent-obj",
        "stt": "openai/realtime",
        "tts": "openai",
    }


def test_allow_list_rejects_unknown_kwarg() -> None:
    with pytest.raises(ValueError) as exc:
        VoiceApp(agent="a", typo_field=123)
    assert "typo_field" in str(exc.value)


def test_dev_is_owned_not_forwarded() -> None:
    app = VoiceApp(agent="a", dev=True)
    assert app.dev is True
    assert "dev" not in app._config_kwargs


def test_dev_defaults_false() -> None:
    assert VoiceApp(agent="a").dev is False


def test_positional_and_keyword_agent_conflict() -> None:
    # ``agent`` is a named parameter, so the language rejects double-binding it.
    with pytest.raises(TypeError):
        VoiceApp("a", agent="b")


# ── Construction: mutual exclusion ───────────────────────────────────


def test_config_and_high_level_field_are_mutually_exclusive() -> None:
    # The worked example from the spec: agent silently disagrees otherwise.
    with pytest.raises(ValueError) as exc:
        VoiceApp(agent="a", config=EasyConfig.browser(agent="b"))
    message = str(exc.value)
    assert "mutually exclusive" in message
    assert "agent" in message


def test_config_and_config_factory_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError) as exc:
        VoiceApp(config=EasyConfig.mic(), config_factory=lambda t: EasyConfig.mic())
    message = str(exc.value)
    assert "config" in message
    assert "config_factory" in message


def test_config_factory_and_high_level_field_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        VoiceApp(agent="a", config_factory=lambda t: EasyConfig.mic())


def test_single_input_style_is_accepted() -> None:
    # Each individual style is fine on its own.
    VoiceApp(agent="a", stt="openai")
    VoiceApp(config=EasyConfig.mic())
    VoiceApp(config_factory=lambda t: EasyConfig.mic())
    VoiceApp()  # no inputs at all is also fine


# ── Mode aliases ─────────────────────────────────────────────────────


def test_unknown_mode_raises() -> None:
    app = VoiceApp(agent="a")
    with pytest.raises(ValueError) as exc:
        app.run("nonsense")
    assert "nonsense" in str(exc.value)


def test_mic_alias_resolves_to_local(
    monkeypatch: pytest.MonkeyPatch, fake_create_session: list[EasyConfig]
) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(helpers_module, "run_session", lambda s: ran.append(s))

    VoiceApp(agent="a").run("mic")
    assert len(ran) == 1
    assert isinstance(ran[0], _FakeSession)


# ── session() semantics ──────────────────────────────────────────────


def test_session_local_returns_unstarted_session(
    fake_create_session: list[EasyConfig],
) -> None:
    app = VoiceApp(agent="a")
    session = app.session("local")
    assert isinstance(session, _FakeSession)
    # session() must use the local mic preset.
    assert len(fake_create_session) == 1
    assert type(fake_create_session[0].transport).__name__ == "LocalTransportConfig"


def test_session_defaults_to_local(fake_create_session: list[EasyConfig]) -> None:
    session = VoiceApp(agent="a").session()
    assert isinstance(session, _FakeSession)


def test_session_local_with_server_policy_field_succeeds(
    fake_create_session: list[EasyConfig],
) -> None:
    """A server-policy field (e.g. ``port``) is accepted but never forwarded
    into ``EasyConfig.mic`` — which has no such field — so local construction
    must not raise ``TypeError``.
    """
    app = VoiceApp(agent="a", port=9001)
    session = app.session("local")
    assert isinstance(session, _FakeSession)
    assert len(fake_create_session) == 1
    config = fake_create_session[0]
    assert config.agent == "a"
    # The server-policy field stayed out of the preset.
    assert not hasattr(config, "port")


@pytest.mark.parametrize("mode", ["browser", "websocket", "twilio", "ws", "phone"])
def test_session_rejects_multi_session_modes(mode: str) -> None:
    app = VoiceApp(agent="a")
    with pytest.raises(ValueError) as exc:
        app.session(mode)
    assert "serve()" in str(exc.value) or "single-session" in str(exc.value)


# ── Local delegation via monkeypatch ─────────────────────────────────


def test_run_local_delegates_to_create_and_run_session(
    monkeypatch: pytest.MonkeyPatch, fake_create_session: list[EasyConfig]
) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(helpers_module, "run_session", lambda s: ran.append(s))

    VoiceApp(agent="my-agent").run("local")

    # create_session was called with EasyConfig.mic(agent=...).
    assert len(fake_create_session) == 1
    config = fake_create_session[0]
    assert type(config.transport).__name__ == "LocalTransportConfig"
    assert config.agent == "my-agent"
    # run_session received the created session.
    assert len(ran) == 1
    assert isinstance(ran[0], _FakeSession)


def test_run_local_with_static_config_passes_it_through(
    monkeypatch: pytest.MonkeyPatch, fake_create_session: list[EasyConfig]
) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(helpers_module, "run_session", lambda s: ran.append(s))

    static = EasyConfig.mic(agent="static-agent")
    VoiceApp(config=static).run("local")

    assert len(fake_create_session) == 1
    # The exact static config object is forwarded unchanged.
    assert fake_create_session[0] is static
    assert len(ran) == 1


def test_twilio_run_delegates_to_server_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run('twilio')`` delegates to the extracted helper (covered in depth in
    ``tests/telephony/test_voice_app_twilio.py``); this is the smoke check."""
    import easycat.telephony.server as server_module

    calls: list[Any] = []

    async def _fake_serve(factory: Any, config: Any) -> None:
        calls.append((factory, config))

    monkeypatch.setattr(server_module, "serve_twilio_voice_app", _fake_serve)
    VoiceApp(agent="a").run("twilio", stream_url="wss://example/media")

    assert len(calls) == 1
    factory, config = calls[0]
    assert callable(factory)
    assert config.stream_url == "wss://example/media"


# ── Local serve(): single clean stop ─────────────────────────────────


async def test_serve_local_stops_session_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve('local')`` starts the session and tears it down exactly once.

    ``wait_for_shutdown_signal`` owns teardown on the signal path (it calls
    ``session.stop()``); ``_serve_local`` adds a ``finally`` that calls
    ``stop()`` again to cover cancellation. Because ``stop()`` is idempotent,
    the session is torn down exactly once on the normal path.
    """
    session = _LifecycleSession(config=object())

    monkeypatch.setattr(config_module, "create_session", lambda _config: session)

    async def _fake_wait(passed_session: Any) -> None:
        # Mirror the real helper: the shutdown path calls stop() once.
        assert passed_session is session
        await passed_session.stop()

    monkeypatch.setattr(helpers_module, "wait_for_shutdown_signal", _fake_wait)

    await VoiceApp(agent="a").serve("local")

    assert session.start_calls == 1
    assert session.effective_stops == 1


async def test_serve_local_stops_session_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled ``serve('local')`` still tears the session down.

    ``wait_for_shutdown_signal``'s signal-handler path awaits the stop event
    without a ``finally``, so when the ``serve('local')`` task is cancelled from
    an outer loop the helper never calls ``stop()``. ``_serve_local``'s own
    ``finally`` must cover that so microphone/provider tasks do not leak.
    """
    session = _LifecycleSession(config=object())

    monkeypatch.setattr(config_module, "create_session", lambda _config: session)

    async def _hang(passed_session: Any) -> None:
        # Mirror the signal-handler path: block until cancelled, never stopping.
        assert passed_session is session
        await asyncio.Event().wait()

    monkeypatch.setattr(helpers_module, "wait_for_shutdown_signal", _hang)

    task = asyncio.ensure_future(VoiceApp(agent="a").serve("local"))
    await asyncio.sleep(0)  # let the task reach the hanging wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.start_calls == 1
    assert session.effective_stops == 1


# ── Local config_factory path (F4) ───────────────────────────────────


def test_local_config_factory_receives_local_transport_instance(
    fake_create_session: list[EasyConfig],
) -> None:
    """``VoiceApp(config_factory=...).session('local')`` invokes the factory with
    a ``LocalTransport`` instance and feeds the resulting config to
    ``create_session``.

    ``EasyConfig`` accepts a transport *instance* for its ``transport`` field
    (``create_session`` discriminates pre-built transports from transport
    configs), so passing ``LocalTransport()`` is the supported local path.
    """
    from easycat.transports import LocalTransport

    seen_transports: list[Any] = []

    def factory(transport: Any) -> EasyConfig:
        seen_transports.append(transport)
        return EasyConfig(transport=transport, agent="fac-agent")

    session = VoiceApp(config_factory=factory).session("local")

    assert isinstance(session, _FakeSession)
    # The factory was invoked once with a real LocalTransport instance.
    assert len(seen_transports) == 1
    assert isinstance(seen_transports[0], LocalTransport)
    # create_session received the factory-built config, transport unchanged.
    assert len(fake_create_session) == 1
    config = fake_create_session[0]
    assert config.transport is seen_transports[0]
    assert config.agent == "fac-agent"


def test_run_local_config_factory_path(
    monkeypatch: pytest.MonkeyPatch, fake_create_session: list[EasyConfig]
) -> None:
    """``run('local')`` with a ``config_factory`` builds the local session via the
    factory and hands it to ``run_session``."""
    from easycat.transports import LocalTransport

    ran: list[Any] = []
    monkeypatch.setattr(helpers_module, "run_session", lambda s: ran.append(s))

    seen_transports: list[Any] = []

    def factory(transport: Any) -> EasyConfig:
        seen_transports.append(transport)
        return EasyConfig(transport=transport, agent="fac-agent")

    VoiceApp(config_factory=factory).run("local")

    assert len(seen_transports) == 1
    assert isinstance(seen_transports[0], LocalTransport)
    assert len(fake_create_session) == 1
    assert fake_create_session[0].transport is seen_transports[0]
    assert len(ran) == 1
    assert isinstance(ran[0], _FakeSession)


# ── Top-level public export + lazy-import guard ──────────────────────


def test_voice_app_is_top_level_public_export() -> None:
    import easycat

    assert "VoiceApp" in easycat.__all__
    resolved = easycat.VoiceApp
    assert resolved is VoiceApp
    assert resolved.__name__ == "VoiceApp"


def test_touching_voice_app_does_not_eager_load_heavy_sdks() -> None:
    """Cold-start guard: ``import easycat; easycat.VoiceApp`` must not drag in
    heavy provider/transport/agent SDKs.

    Runs in a fresh interpreter so the test process's own imports don't pollute
    ``sys.modules`` (mirrors
    ``test_touching_easyconfig_does_not_eager_load_telephony_stack``).
    """
    import subprocess
    import sys

    code = (
        "import sys, easycat\n"
        "app = easycat.VoiceApp\n"
        "heavy = sorted(m for m in sys.modules if m in {'aiortc', 'agents', 'aiohttp'})\n"
        "print('\\n'.join(heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = {line for line in result.stdout.splitlines() if line}
    assert not loaded, f"easycat.VoiceApp eager-loaded heavy SDKs: {sorted(loaded)}"
