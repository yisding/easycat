"""Tests for provider Protocol definitions — verify structural subtyping works."""

from collections.abc import AsyncIterator

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
    TTSProvider,
    VADProvider,
)
from easycat.tts.input import TTSInput

# ── Stub implementations ──────────────────────────────────────────


class StubSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:
        yield STTEvent(type=STTEventType.FINAL, text="stub")


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


class StubVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        yield VADStartSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 300,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        pass


class StubNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


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
