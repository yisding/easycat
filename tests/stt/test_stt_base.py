"""Tests for the STT base class and test harness."""

from __future__ import annotations

import struct

import pytest

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType
from easycat.stt.base import STTBase, pcm_to_wav
from easycat.stt.websocket_base import WebSocketSTTBase
from tests.stt.helpers import (
    collect_stt_events,
    generate_pcm_noise,
    generate_pcm_silence,
    generate_pcm_sine,
    make_audio_chunks,
)

# ── pcm_to_wav tests ─────────────────────────────────────────────


def test_pcm_to_wav_header():
    pcm = b"\x00\x00" * 100  # 100 samples of silence
    wav = pcm_to_wav(pcm, PCM16_MONO_16K)

    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert wav[12:16] == b"fmt "
    assert wav[36:40] == b"data"

    # Data size field should match PCM data length
    data_size = struct.unpack("<I", wav[40:44])[0]
    assert data_size == len(pcm)

    # RIFF size = 36 + data_size
    riff_size = struct.unpack("<I", wav[4:8])[0]
    assert riff_size == 36 + len(pcm)


def test_pcm_to_wav_format_fields():
    pcm = b"\x00\x00" * 50
    wav = pcm_to_wav(pcm, PCM16_MONO_16K)

    channels = struct.unpack("<H", wav[22:24])[0]
    sample_rate = struct.unpack("<I", wav[24:28])[0]
    bits_per_sample = struct.unpack("<H", wav[34:36])[0]

    assert channels == 1
    assert sample_rate == 16000
    assert bits_per_sample == 16


def test_pcm_to_wav_different_sample_rate():
    pcm = b"\x00\x00" * 50
    wav = pcm_to_wav(pcm, PCM16_MONO_8K)

    sample_rate = struct.unpack("<I", wav[24:28])[0]
    assert sample_rate == 8000


def test_pcm_to_wav_contains_audio_data():
    pcm = generate_pcm_sine(duration_ms=100, sample_rate=16000)
    wav = pcm_to_wav(pcm, PCM16_MONO_16K)

    # Audio data starts at byte 44
    assert wav[44:] == pcm


# ── STTBase lifecycle tests ───────────────────────────────────────


class EchoSTT(STTBase):
    """Test STT provider that emits a fixed transcript on end_stream."""

    def __init__(self, transcript: str = "test transcript") -> None:
        super().__init__()
        self.transcript = transcript
        self.audio_received: list[bytes] = []

    async def _on_audio(self, chunk: AudioChunk) -> None:
        self.audio_received.append(chunk.data)

    async def _on_end(self) -> None:
        if self.audio_received:
            self._emit_event(STTEvent(type=STTEventType.FINAL, text=self.transcript))


class MockWebSocket:
    def __init__(self, messages: list[str | bytes]) -> None:
        self.messages = messages
        self.sent: list[str | bytes] = []
        self.closed = False
        self._iter_index = 0

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        if self._iter_index >= len(self.messages):
            raise StopAsyncIteration
        message = self.messages[self._iter_index]
        self._iter_index += 1
        return message


class JsonWebSocketSTT(WebSocketSTTBase):
    def __init__(self, ws: MockWebSocket) -> None:
        super().__init__(provider_name="test_stt", provider_error_name="test")
        self._mock_ws = ws

    async def _on_start(self) -> None:
        async def connect(_url: str, **_kwargs: object) -> MockWebSocket:
            return self._mock_ws

        await self._connect_websocket(url="wss://example.test", headers={}, connect_fn=connect)

    async def _on_audio(self, chunk: AudioChunk) -> None:
        await self._send_ws(chunk.data)

    async def _on_end(self) -> None:
        await self._close_active_websocket(close_before_drain=False)

    def _handle_json_message(self, msg: dict[str, object]) -> None:
        text = msg.get("text")
        if isinstance(text, str):
            self._emit_event(STTEvent(type=STTEventType.FINAL, text=text))


@pytest.mark.asyncio
async def test_base_start_stop_lifecycle():
    stt = EchoSTT()
    await stt.start_stream()
    assert stt._running is True
    await stt.end_stream()
    assert stt._running is False


@pytest.mark.asyncio
async def test_base_send_audio_before_start_raises():
    stt = EchoSTT()
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    with pytest.raises(RuntimeError, match="Stream not started"):
        await stt.send_audio(chunk)


@pytest.mark.asyncio
async def test_base_validates_pcm_encoding():
    stt = EchoSTT()
    await stt.start_stream()
    bad_chunk = AudioChunk(
        data=b"\x00\x00",
        format=AudioFormat(sample_rate=16000, channels=1, sample_width=2, encoding="mulaw"),
    )
    with pytest.raises(ValueError, match="PCM encoding"):
        await stt.send_audio(bad_chunk)
    await stt.end_stream()


@pytest.mark.asyncio
async def test_base_validates_sample_rate():
    stt = STTBase(expected_sample_rate=16000)
    await stt.start_stream()
    bad_chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_8K)
    with pytest.raises(ValueError, match="sample rate"):
        await stt.send_audio(bad_chunk)
    await stt.end_stream()


@pytest.mark.asyncio
async def test_base_end_stream_idempotent():
    stt = EchoSTT()
    await stt.start_stream()
    await stt.end_stream()
    # Second call should be a no-op
    await stt.end_stream()


@pytest.mark.asyncio
async def test_base_emits_events():
    stt = EchoSTT(transcript="hello world")
    pcm = generate_pcm_sine(duration_ms=200)
    chunks = make_audio_chunks(pcm)
    events = await collect_stt_events(stt, chunks)

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "hello world"


@pytest.mark.asyncio
async def test_base_no_events_on_empty_audio():
    stt = EchoSTT()
    events = await collect_stt_events(stt, [])
    assert len(events) == 0


@pytest.mark.asyncio
async def test_base_receives_all_audio():
    stt = EchoSTT()
    pcm = generate_pcm_sine(duration_ms=500)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)
    await stt.end_stream()

    total = b"".join(stt.audio_received)
    assert total == pcm


@pytest.mark.asyncio
async def test_base_fresh_queue_per_stream():
    stt = EchoSTT(transcript="first")
    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    assert len(events1) == 1
    assert events1[0].text == "first"

    stt.transcript = "second"
    events2 = await collect_stt_events(stt, chunks)
    assert len(events2) == 1
    assert events2[0].text == "second"


@pytest.mark.asyncio
async def test_websocket_base_ignores_binary_and_invalid_json_messages():
    ws = MockWebSocket([b"\x00\x01", "{not json", '{"text": "hello"}'])
    stt = JsonWebSocketSTT(ws)

    events = await collect_stt_events(stt, make_audio_chunks(generate_pcm_sine(duration_ms=100)))

    assert [event.text for event in events] == ["hello"]
    assert ws.closed is True
    assert ws.sent


# ── Test harness tests (verify helper functions) ──────────────────


def test_generate_pcm_sine_length():
    pcm = generate_pcm_sine(duration_ms=1000, sample_rate=16000)
    expected_samples = 16000
    expected_bytes = expected_samples * 2  # 16-bit = 2 bytes per sample
    assert len(pcm) == expected_bytes


def test_generate_pcm_silence_length():
    pcm = generate_pcm_silence(duration_ms=500, sample_rate=8000)
    expected_samples = 4000
    assert len(pcm) == expected_samples * 2


def test_generate_pcm_noise_deterministic():
    a = generate_pcm_noise(duration_ms=100, seed=42)
    b = generate_pcm_noise(duration_ms=100, seed=42)
    assert a == b

    c = generate_pcm_noise(duration_ms=100, seed=99)
    assert a != c


def test_make_audio_chunks_count():
    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)
    assert len(chunks) == 5


def test_make_audio_chunks_total_data():
    pcm = generate_pcm_sine(duration_ms=300, sample_rate=16000)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)
    total = b"".join(c.data for c in chunks)
    assert total == pcm


@pytest.mark.asyncio
async def test_collect_stt_events_harness():
    """The test harness itself should work correctly with a simple provider."""
    stt = EchoSTT(transcript="harness test")
    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)
    events = await collect_stt_events(stt, chunks)
    assert len(events) == 1
    assert events[0].text == "harness test"


# ── STTProvider protocol conformance ─────────────────────────────


def test_stt_base_conforms_to_protocol():
    from easycat.providers import STTProvider

    assert isinstance(STTBase(), STTProvider)


def test_echo_stt_conforms_to_protocol():
    from easycat.providers import STTProvider

    assert isinstance(EchoSTT(), STTProvider)
