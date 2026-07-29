"""Tests for the STT base class and test harness."""

from __future__ import annotations

import asyncio

import pytest

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType
from easycat.stt.base import STTBase
from easycat.stt.websocket_base import WebSocketSTTBase
from tests.stt.helpers import (
    collect_stt_events,
    generate_pcm_sine,
    make_audio_chunks,
)

# ── _drain_buffer_to_wav tests ────────────────────────────────────


def test_drain_buffer_to_wav_returns_none_when_empty():
    stt = STTBase()
    stt._buffer = bytearray()
    stt._audio_format = PCM16_MONO_16K
    assert stt._drain_buffer_to_wav() is None


def test_drain_buffer_to_wav_returns_none_when_no_format():
    stt = STTBase()
    stt._buffer = bytearray(b"\x00\x00" * 10)
    stt._audio_format = None
    assert stt._drain_buffer_to_wav() is None


def test_drain_buffer_to_wav_wraps_and_clears_in_place():
    stt = STTBase()
    buf = bytearray(b"\x00\x00" * 10)
    stt._buffer = buf
    stt._audio_format = PCM16_MONO_16K
    wav = stt._drain_buffer_to_wav()
    assert wav is not None and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert wav[44:] == b"\x00\x00" * 10
    assert len(buf) == 0  # cleared
    assert stt._buffer is buf  # same object (in-place clear, not rebind)
    assert stt._audio_format == PCM16_MONO_16K  # latched format preserved


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
        await self._close_active_websocket()

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
async def test_websocket_end_stream_preempts_stalled_ordered_send() -> None:
    class PausingWebSocketSTT(WebSocketSTTBase):
        def __init__(self) -> None:
            super().__init__(provider_name="test", provider_error_name="test")
            self.send_started = asyncio.Event()
            self.send_cancelled = asyncio.Event()
            self.end_called = asyncio.Event()

        async def _on_start(self) -> None:
            pass

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.send_cancelled.set()
                raise

        async def _on_end(self) -> None:
            self.end_called.set()

        def _handle_json_message(self, msg: dict[str, object]) -> None:
            _ = msg

    stt = PausingWebSocketSTT()
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    await stt.start_stream()

    first_send = asyncio.create_task(stt.send_audio(chunk))
    await asyncio.wait_for(stt.send_started.wait(), timeout=1)
    second_send = asyncio.create_task(stt.send_audio(chunk))

    await asyncio.wait_for(stt.end_stream(), timeout=0.1)

    assert stt.end_called.is_set()
    assert stt.send_cancelled.is_set()
    assert not second_send.done()

    await asyncio.wait_for(first_send, timeout=1)
    with pytest.raises(RuntimeError, match="Stream not started"):
        await asyncio.wait_for(second_send, timeout=1)


@pytest.mark.asyncio
async def test_segment_commit_waits_for_in_flight_ordered_send() -> None:
    class OrderedSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.order: list[str] = []

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_started.set()
            await self.release_send.wait()
            self.order.append("audio")

        async def _on_commit_segment(self) -> bool:
            self.order.append("commit")
            return True

    stt = OrderedSTT()
    await stt.start_stream()
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    send = asyncio.create_task(stt.send_audio(chunk))
    await stt.send_started.wait()
    commit = asyncio.create_task(stt.commit_segment())

    await asyncio.sleep(0)
    assert not commit.done()
    stt.release_send.set()
    assert await commit is True
    await send
    assert stt.order == ["audio", "commit"]
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
    ws = MockWebSocket([b"\x00\x01", "{not json", "[]", '{"text": "hello"}'])
    stt = JsonWebSocketSTT(ws)

    events = await collect_stt_events(stt, make_audio_chunks(generate_pcm_sine(duration_ms=100)))

    assert [event.text for event in events] == ["hello"]
    assert ws.closed is True
    assert ws.sent


# ── STTProvider protocol conformance ─────────────────────────────


def test_stt_base_conforms_to_protocol():
    from easycat.providers import STTProvider

    assert isinstance(STTBase(), STTProvider)


def test_echo_stt_conforms_to_protocol():
    from easycat.providers import STTProvider

    assert isinstance(EchoSTT(), STTProvider)
