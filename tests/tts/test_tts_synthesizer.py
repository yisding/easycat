"""Tests for TTSSynthesizer — shared TTS synthesis logic."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from easycat._bounded_queue import BoundedAudioQueue, DropPolicy
from easycat._tts_synthesizer import TTSSynthesizer
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import EventBus, TTSAudio, TTSEvent, TTSEventType, TTSMarkers
from easycat.tts.input import TTSInput

# ── Test helpers ───────────────────────────────────────────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class FakeTTS:
    """TTS that yields one audio chunk per synthesize call."""

    def __init__(self, chunks: int = 1) -> None:
        self._chunks = chunks
        self.synthesized: list[str] = []
        self.cancelled = False

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.synthesized.append(payload.text)
        for _ in range(self._chunks):
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def cancel(self) -> None:
        self.cancelled = True


class MarkerTTS:
    """TTS that yields audio then markers."""

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": "hello", "time": 0.1}])

    async def cancel(self) -> None:
        pass


class ControlledTTS:
    """TTS that pauses after its first audio chunk until the test releases it."""

    def __init__(self) -> None:
        self.first_chunk_ready = asyncio.Event()
        self.release_next = asyncio.Event()

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.first_chunk_ready.set()
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        await self.release_next.wait()
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def cancel(self) -> None:
        pass


class FirstEventTTS:
    """Expose when the provider reaches its first event and is finalized."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finalized = asyncio.Event()

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        try:
            self.started.set()
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        finally:
            self.finalized.set()

    async def cancel(self) -> None:
        pass


class MarkerFirstTTS:
    """Yield a marker before audio to verify the barrier covers all events."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.started.set()
        yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": "hello"}])
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def cancel(self) -> None:
        pass


class FailingTTS:
    """TTS that raises mid-stream."""

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        raise RuntimeError("TTS failed")

    async def cancel(self) -> None:
        pass


class CancelledTTS:
    """TTS that raises CancelledError mid-stream."""

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        raise asyncio.CancelledError()

    async def cancel(self) -> None:
        pass


def _make_synth(
    tts=None,
    timeout_config=None,
) -> tuple[TTSSynthesizer, EventBus, BoundedAudioQueue]:
    event_bus = EventBus()
    queue = BoundedAudioQueue(max_size=100, policy=DropPolicy.DROP_OLDEST, name="test")
    synth = TTSSynthesizer(
        tts=tts or FakeTTS(),
        event_bus=event_bus,
        outbound_queue=queue,
        timeout_config=timeout_config,
    )
    return synth, event_bus, queue


# ── Basic synthesis tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_emits_tts_audio_event():
    synth, event_bus, _ = _make_synth()
    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, lambda e: received.append(e))

    result = await synth.synthesize(TTSInput("hello"), None)

    assert result.audio_produced
    assert result.first_audio_time is not None
    assert len(received) == 1


@pytest.mark.asyncio
async def test_synthesize_queues_audio():
    synth, _, queue = _make_synth(tts=FakeTTS(chunks=3))

    result = await synth.synthesize(TTSInput("hello"), None)

    assert result.audio_produced
    assert not queue.empty()


@pytest.mark.asyncio
async def test_synthesize_offers_only_first_audio_to_direct_sender():
    event_bus = EventBus()
    queue = BoundedAudioQueue(max_size=100, policy=DropPolicy.DROP_OLDEST, name="test")
    direct: list[AudioChunk] = []

    async def _send_direct(chunk: AudioChunk) -> bool:
        direct.append(chunk)
        return True

    synth = TTSSynthesizer(
        tts=FakeTTS(chunks=3),
        event_bus=event_bus,
        outbound_queue=queue,
        direct_first_audio=_send_direct,
    )

    result = await synth.synthesize(TTSInput("hello"), None)

    assert result.audio_produced
    assert len(direct) == 1
    assert queue.qsize() == 2


@pytest.mark.asyncio
async def test_synthesize_queues_first_audio_when_direct_sender_declines():
    event_bus = EventBus()
    queue = BoundedAudioQueue(max_size=100, policy=DropPolicy.DROP_OLDEST, name="test")

    async def _decline_direct(_chunk: AudioChunk) -> bool:
        return False

    synth = TTSSynthesizer(
        tts=FakeTTS(chunks=1),
        event_bus=event_bus,
        outbound_queue=queue,
        direct_first_audio=_decline_direct,
    )

    result = await synth.synthesize(TTSInput("hello"), None)

    assert result.audio_produced
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_synthesize_emits_markers():
    synth, event_bus, _ = _make_synth(tts=MarkerTTS())
    markers: list[TTSMarkers] = []
    event_bus.subscribe(TTSMarkers, lambda e: markers.append(e))

    await synth.synthesize(TTSInput("hello"), None)

    assert len(markers) == 1
    assert markers[0].markers[0]["word"] == "hello"


@pytest.mark.asyncio
async def test_synthesize_tracks_audio_bytes():
    synth, _, _ = _make_synth(tts=FakeTTS(chunks=3))
    result = await synth.synthesize(TTSInput("hello"), None)

    # Each chunk is 320 bytes, 3 chunks → 960 bytes
    assert result.audio_bytes == 320 * 3


@pytest.mark.asyncio
async def test_synthesize_no_audio_returns_false():
    class EmptyTTS:
        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            return
            yield  # make it an async generator

        async def cancel(self) -> None:
            pass

    synth, _, _ = _make_synth(tts=EmptyTTS())
    result = await synth.synthesize(TTSInput("hello"), None)
    assert not result.audio_produced
    assert result.first_audio_time is None


@pytest.mark.asyncio
async def test_start_barrier_starts_provider_without_releasing_audio():
    tts = FirstEventTTS()
    synth, event_bus, queue = _make_synth(tts=tts)
    barrier = asyncio.Event()
    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, received.append)

    task = asyncio.create_task(synth.synthesize(TTSInput("hello"), None, start_barrier=barrier))
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert received == []
    assert queue.empty()
    assert not task.done()

    barrier.set()
    result = await task
    assert result.audio_produced is True
    assert len(received) == 1
    assert not queue.empty()


@pytest.mark.asyncio
async def test_start_barrier_holds_marker_events_too():
    tts = MarkerFirstTTS()
    synth, event_bus, _ = _make_synth(tts=tts)
    barrier = asyncio.Event()
    markers: list[TTSMarkers] = []
    audio: list[TTSAudio] = []
    event_bus.subscribe(TTSMarkers, markers.append)
    event_bus.subscribe(TTSAudio, audio.append)

    task = asyncio.create_task(synth.synthesize(TTSInput("hello"), None, start_barrier=barrier))
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert markers == []
    assert audio == []

    barrier.set()
    await task
    assert len(markers) == 1
    assert len(audio) == 1


@pytest.mark.asyncio
async def test_start_barrier_rechecks_cancel_before_releasing_first_event():
    token = CancelToken()
    tts = FirstEventTTS()
    synth, event_bus, queue = _make_synth(tts=tts)
    barrier = asyncio.Event()
    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, received.append)

    task = asyncio.create_task(synth.synthesize(TTSInput("hello"), token, start_barrier=barrier))
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    token.cancel()
    barrier.set()

    result = await task
    assert result.completed is False
    assert result.audio_produced is False
    assert received == []
    assert queue.empty()
    assert tts.finalized.is_set()


# ── Cancellation tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_stops_on_cancel_token():
    token = CancelToken()
    tts = ControlledTTS()
    synth, event_bus, _ = _make_synth(tts=tts)

    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, lambda e: received.append(e))

    task = asyncio.create_task(synth.synthesize(TTSInput("hello"), token))
    await asyncio.wait_for(tts.first_chunk_ready.wait(), timeout=0.5)
    token.cancel()
    tts.release_next.set()
    result = await task

    assert len(received) == 1
    assert result.completed is False


@pytest.mark.asyncio
async def test_synthesize_stops_on_is_active_false():
    active = True
    tts = ControlledTTS()
    synth, event_bus, _ = _make_synth(tts=tts)

    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, lambda e: received.append(e))

    task = asyncio.create_task(synth.synthesize(TTSInput("hello"), None, is_active=lambda: active))
    await asyncio.wait_for(tts.first_chunk_ready.wait(), timeout=0.5)
    active = False
    tts.release_next.set()
    result = await task

    assert len(received) == 1
    assert result.completed is False


@pytest.mark.asyncio
async def test_synthesize_marks_incomplete_on_cancelled_error():
    synth, _, _ = _make_synth(tts=CancelledTTS())

    with pytest.raises(asyncio.CancelledError):
        await synth.synthesize(TTSInput("hello"), None)


# ── Cancel method test ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_delegates_to_tts():
    tts = FakeTTS()
    synth, _, _ = _make_synth(tts=tts)

    await synth.cancel()
    assert tts.cancelled


@pytest.mark.asyncio
async def test_cancel_logs_provider_failure(caplog: pytest.LogCaptureFixture):
    class RaisingCancelTTS(FakeTTS):
        async def cancel(self) -> None:
            raise RuntimeError("provider cancel failed")

    synth, _, _ = _make_synth(tts=RaisingCancelTTS())

    with caplog.at_level(logging.DEBUG, logger="easycat._tts_synthesizer"):
        await synth.cancel()

    assert "TTS provider cancel raised" in caplog.text
    assert "provider cancel failed" in caplog.text
