"""Public EventBus attachment contracts for injected and configured providers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from easycat import EventBusBindable
from easycat.events import EventBus
from easycat.session._session import Session
from easycat.session._types import SessionConfig


class _PublicHookProvider:
    def __init__(self) -> None:
        self.received_bus: EventBus | None = None
        self._event_bus: EventBus | None = None
        self._config = SimpleNamespace(event_bus=None)

    def set_event_bus(self, event_bus: EventBus) -> None:
        self.received_bus = event_bus


def test_session_attaches_public_event_bus_hook_to_every_audio_stage() -> None:
    bus = EventBus()
    providers = [_PublicHookProvider() for _ in range(6)]

    Session(
        SessionConfig(
            runtime_mode="text_session",
            event_bus=bus,
            stt=providers[0],
            tts=providers[1],
            vad=providers[2],
            noise_reducer=providers[3],
            echo_canceller=providers[4],
            transport=providers[5],
        )
    )

    assert all(isinstance(provider, EventBusBindable) for provider in providers)
    assert all(provider.received_bus is bus for provider in providers)
    # The public hook wins; private-name fallbacks are not touched.
    assert all(provider._event_bus is None for provider in providers)
    assert all(provider._config.event_bus is None for provider in providers)


def test_failed_public_hook_uses_legacy_event_bus_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenPublicHook:
        def __init__(self) -> None:
            self._event_bus: EventBus | None = None

        def set_event_bus(self, event_bus: EventBus) -> None:
            del event_bus
            raise RuntimeError("not ready")

    bus = EventBus()
    provider = BrokenPublicHook()
    session = object.__new__(Session)
    session.event_bus = bus

    with caplog.at_level("WARNING", logger="easycat.session"):
        session._maybe_attach_event_bus(provider)

    assert provider._event_bus is bus
    assert "rejected set_event_bus" in caplog.text


def test_async_public_hook_is_rejected_and_uses_legacy_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class AsyncPublicHook:
        def __init__(self) -> None:
            self._event_bus: EventBus | None = None

        async def set_event_bus(self, event_bus: EventBus) -> None:
            self._event_bus = event_bus

    bus = EventBus()
    provider = AsyncPublicHook()
    session = object.__new__(Session)
    session.event_bus = bus

    with caplog.at_level("WARNING", logger="easycat.session"):
        session._maybe_attach_event_bus(provider)

    assert provider._event_bus is bus
    assert "set_event_bus() must be synchronous" in caplog.text


def test_legacy_config_fallback_remains_compatible_and_preserves_explicit_bus() -> None:
    session_bus = EventBus()
    explicit_bus = EventBus()
    unset = SimpleNamespace(_config=SimpleNamespace(event_bus=None))
    configured = SimpleNamespace(_config=SimpleNamespace(event_bus=explicit_bus))
    session = object.__new__(Session)
    session.event_bus = session_bus

    session._maybe_attach_event_bus(unset)
    session._maybe_attach_event_bus(configured)

    assert unset._config.event_bus is session_bus
    assert configured._config.event_bus is explicit_bus


@dataclass(frozen=True)
class _ConfiguredStage:
    event_bus: EventBus | None = None


@dataclass
class _DataclassLiveStage:
    event_bus: EventBus | None = None

    async def process(self, chunk):
        if False:
            yield chunk

    def configure(self, **kwargs) -> None:
        del kwargs

    def feed_reference(self, chunk) -> None:
        del chunk

    def set_event_bus(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus


def test_audio_pipeline_injects_bus_into_registered_stage_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.config import _factory as factory

    bus = EventBus()
    captured: dict[str, _ConfiguredStage] = {}
    config = SimpleNamespace(
        stt=object(),
        tts=object(),
        vad=_ConfiguredStage(),
        noise_reduction=_ConfiguredStage(),
        enable_noise_reduction=True,
        echo_cancellation=_ConfiguredStage(),
        transport=object(),
    )

    monkeypatch.setattr(factory, "_create_stt", lambda _config, _bus: object())
    monkeypatch.setattr(factory, "_create_tts", lambda _config, _bus: object())
    monkeypatch.setattr(factory, "_should_auto_turn_from_stt_final", lambda _config: False)
    monkeypatch.setattr(factory, "_create_transport", lambda _config, _bus: object())

    def create_vad(stage: _ConfiguredStage):
        captured["vad"] = stage
        return object()

    def create_noise(stage: _ConfiguredStage):
        captured["noise"] = stage
        return object()

    def create_echo(stage: _ConfiguredStage):
        captured["echo"] = stage
        return object()

    monkeypatch.setattr(factory, "_create_vad", create_vad)
    monkeypatch.setattr(factory, "_resolve_noise_reducer", create_noise)
    monkeypatch.setattr(factory, "_resolve_echo_canceller", create_echo)

    factory._resolve_audio_pipeline(config, bus)

    assert captured == {
        "vad": _ConfiguredStage(event_bus=bus),
        "noise": _ConfiguredStage(event_bus=bus),
        "echo": _ConfiguredStage(event_bus=bus),
    }

    live_stage = _DataclassLiveStage()
    config.vad = live_stage
    config.noise_reduction = live_stage
    config.echo_cancellation = live_stage

    factory._resolve_audio_pipeline(config, bus)

    assert captured["vad"] is live_stage
    assert captured["noise"] is live_stage
    assert captured["echo"] is live_stage
    assert live_stage.event_bus is None  # Session attaches it through set_event_bus.
