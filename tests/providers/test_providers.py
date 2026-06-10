"""Tests for provider Protocol definitions — verify structural subtyping works."""

from collections.abc import AsyncIterator

import pytest

from easycat._provider_catalog import ProviderCatalog
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
)
from easycat.providers import (
    NoiseReducer,
    STTProvider,
    Transport,
    TransportLike,
    TTSProvider,
    VADProvider,
)
from easycat.tts.input import TTSInput

# ── Stub implementations ──────────────────────────────────────────


_STUB_VERSION = {
    "provider": "stub",
    "model": "unknown",
    "api_version": "unknown",
    "sdk_version": "unknown",
}


class StubSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return True

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:
        yield STTEvent(type=STTEventType.FINAL, text="stub")

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubTTS:
    @property
    def supports_ssml(self) -> bool:
        return False

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K),
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        yield VADStartSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


# ── Protocol conformance tests ────────────────────────────────────


def test_stub_stt_is_stt_provider():
    assert isinstance(StubSTT(), STTProvider)


def test_stub_tts_is_tts_provider():
    assert isinstance(StubTTS(), TTSProvider)


def test_stub_vad_is_vad_provider():
    assert isinstance(StubVAD(), VADProvider)


def test_stub_noise_reducer_is_noise_reducer():
    assert isinstance(StubNoiseReducer(), NoiseReducer)


def test_stub_transport_is_transport():
    assert isinstance(StubTransport(), Transport)


class LegacyTransport:
    """A custom transport satisfying the audio contract but lacking version_info()."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def clear_audio(self) -> None:
        pass


def test_legacy_transport_lacks_version_info_fails_full_protocol():
    # The full Transport protocol now requires version_info(); a transport that
    # predates that contract no longer satisfies isinstance(..., Transport).
    assert not isinstance(LegacyTransport(), Transport)


def test_legacy_transport_satisfies_transport_like():
    # ...but it still matches the narrow audio contract used to discriminate a
    # pre-built transport instance from a transport config in _create_transport.
    assert isinstance(LegacyTransport(), TransportLike)
    assert isinstance(StubTransport(), TransportLike)


class _CatalogProvider:
    pass


class _CatalogConfig:
    pass


def _catalog_kwargs() -> dict:
    return {
        "providers": {"known": (_CatalogProvider, _CatalogConfig)},
        "env_vars": {"known": "KNOWN_API_KEY"},
        "extras": {"known": "known"},
        "api_domains": {"known": ("known.example",)},
        "kind": "Test",
    }


def test_provider_catalog_rejects_mismatched_provider_and_env_var_keys():
    with pytest.raises(
        ValueError,
        match=(
            "Test provider catalog keys must match env var keys; "
            "missing env_vars for: known; env_vars without providers: extra"
        ),
    ):
        ProviderCatalog(**{**_catalog_kwargs(), "env_vars": {"extra": "EXTRA_API_KEY"}})


def test_provider_catalog_rejects_mismatched_extras_keys():
    with pytest.raises(
        ValueError,
        match="Test provider catalog keys must match extra keys; missing extras for: known",
    ):
        ProviderCatalog(**{**_catalog_kwargs(), "extras": {}})


def test_provider_catalog_rejects_mismatched_api_domain_keys():
    with pytest.raises(
        ValueError,
        match=(
            "Test provider catalog keys must match api domain keys; "
            "api_domains without providers: extra"
        ),
    ):
        ProviderCatalog(
            **{
                **_catalog_kwargs(),
                "api_domains": {"known": ("known.example",), "extra": ("extra.example",)},
            }
        )
