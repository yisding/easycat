from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import AudioChunk
from easycat.events import Event, STTEvent, TTSEvent
from easycat.runtime.capabilities import (
    aclose_if_supported,
    bind_identity_sink_if_supported,
    clear_audio_if_supported,
    health_checkable,
    playback_acknowledgements,
    transport_reports_audio_delivery,
)
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.tts.input import TTSInput


class _ActiveSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:
        return
        yield


class _ActiveTTS:
    @property
    def supports_ssml(self) -> bool:
        return True

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        return
        yield

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _ActiveVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        return
        yield

    def configure(self, **kwargs: object) -> None:
        pass


class _ActiveTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        return
        yield

    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    async def clear_audio(self) -> None:
        pass


class _ActiveAgent:
    async def run(self, text: str) -> str:
        return text


class _PassthroughSTT(_ActiveSTT):
    is_passthrough_provider = True


class _PassthroughTTS(_ActiveTTS):
    is_passthrough_provider = True


class _PassthroughVAD(_ActiveVAD):
    is_passthrough_provider = True


class _PassthroughTransport(_ActiveTransport):
    is_passthrough_provider = True


class _ActiveNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


class _PassthroughNoiseReducer(_ActiveNoiseReducer):
    is_passthrough_provider = True


class _ActiveEchoCanceller:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        pass


class _PassthroughEchoCanceller(_ActiveEchoCanceller):
    is_passthrough_provider = True


class _CapabilityTransport(_ActiveTransport):
    reports_audio_delivery = True

    async def send_playback_mark(self, name: str | None = None) -> str:
        return name or "custom_mark"


class _DelegatedCapabilities(_CapabilityTransport):
    def __init__(self) -> None:
        self.cleared = False
        self.closed = False
        self.identity_sink = None

    async def clear_audio(self) -> None:
        self.cleared = True

    def bind_identity_sink(self, sink: object) -> None:
        self.identity_sink = sink

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class _CapabilityProxy:
    def __init__(self, target: object) -> None:
        self._target = target

    def __getattr__(self, name: str) -> object:
        return getattr(self._target, name)


def _config(**overrides: object) -> SessionConfig:
    values = {
        "stt": _ActiveSTT(),
        "tts": _ActiveTTS(),
        "vad": _ActiveVAD(),
        "transport": _ActiveTransport(),
        "agent": _ActiveAgent(),
    }
    values.update(overrides)
    return SessionConfig(**values)


@pytest.mark.parametrize(
    ("field", "provider", "expected_name"),
    [
        ("stt", _PassthroughSTT(), "stt"),
        ("tts", _PassthroughTTS(), "tts"),
        ("vad", _PassthroughVAD(), "vad"),
        ("transport", _PassthroughTransport(), "transport"),
    ],
)
def test_session_validation_rejects_custom_passthrough_provider_markers(
    field: str,
    provider: object,
    expected_name: str,
) -> None:
    with pytest.raises(ValueError, match=expected_name):
        Session(_config(**{field: provider}))


def test_session_validation_rejects_custom_passthrough_noise_when_enabled() -> None:
    with pytest.raises(ValueError, match="noise_reducer"):
        Session(
            _config(
                noise_reducer=_PassthroughNoiseReducer(),
                enable_noise_reduction=True,
            )
        )


def test_custom_passthrough_processors_do_not_auto_enable_features() -> None:
    session = Session(
        _config(
            noise_reducer=_PassthroughNoiseReducer(),
            echo_canceller=_PassthroughEchoCanceller(),
        )
    )

    assert session._enable_noise_reduction is False
    assert session._enable_aec is False


def test_custom_active_processors_still_auto_enable_features() -> None:
    session = Session(
        _config(
            noise_reducer=_ActiveNoiseReducer(),
            echo_canceller=_ActiveEchoCanceller(),
        )
    )

    assert session._enable_noise_reduction is True
    assert session._enable_aec is True


def test_transport_capabilities_are_detected_structurally() -> None:
    transport = _CapabilityTransport()
    session = Session(_config(transport=transport))

    assert session._audio_router._playback_ack_transport is transport
    assert session._audio_router._transport_reports_audio_delivery is True


@pytest.mark.asyncio
async def test_capability_helpers_support_getattr_delegation() -> None:
    target = _DelegatedCapabilities()
    proxy = _CapabilityProxy(target)
    sink = object()

    playback = playback_acknowledgements(proxy)
    assert playback is not None
    assert await playback.send_playback_mark() == "custom_mark"

    await clear_audio_if_supported(proxy)
    assert target.cleared is True

    assert bind_identity_sink_if_supported(proxy, sink) is True
    assert target.identity_sink is sink

    health = health_checkable(proxy)
    assert health is not None
    assert await health.health_check() is True

    await aclose_if_supported(proxy)
    assert target.closed is True
    assert transport_reports_audio_delivery(proxy) is True
