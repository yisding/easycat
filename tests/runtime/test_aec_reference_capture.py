"""AEC far-end reference frame capture (WP17 STEP 1).

The bot playback fed to the echo canceller as the far-end reference is the one
AEC leg the pipeline never journals on its own.  ``AudioStage.record_reference``
captures it as an ``aec_reference_frame`` record (mirroring ``tts_frame``); the
router calls it from the outbound delivery side effect, strictly additive and
never raising.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import TTSEvent, TTSEventType
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.context import RunContext
from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.stages.audio import AudioStage
from easycat.turn_manager import TurnManagerConfig

# ── Test doubles ─────────────────────────────────────────────────


class _PassthroughAEC:
    """Echo canceller that returns the mic chunk unchanged and tracks feeds."""

    def __init__(self) -> None:
        self.fed: list[AudioChunk] = []

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        self.fed.append(chunk)

    def configure(self, **_kw) -> None: ...


class _RaisingAEC(_PassthroughAEC):
    """Echo canceller whose ``feed_reference`` always raises."""

    def feed_reference(self, chunk: AudioChunk) -> None:
        self.fed.append(chunk)
        raise ValueError("near/far sample rate mismatch")


class _FakeTransport:
    def __init__(self, chunks_in: list[AudioChunk]) -> None:
        self._chunks_in = chunks_in
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._chunks_in:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return True

    async def clear_audio(self) -> None: ...


class _FakeVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, _chunk: AudioChunk) -> AsyncIterator:
        from easycat.events import VADStartSpeaking, VADStopSpeaking

        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 2:
            yield VADStopSpeaking()

    def configure(self, **_kw) -> None: ...


class _FakeSTT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()

    async def start_stream(self) -> None: ...
    async def send_audio(self, _chunk: AudioChunk) -> None: ...
    async def end_stream(self) -> None:
        from easycat.events import STTEvent, STTEventType

        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="hi"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator:
        while True:
            evt = await self._queue.get()
            if evt is None:
                break
            yield evt


class _FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class _DistinctiveTTS:
    async def synthesize(self, _payload) -> AsyncIterator[TTSEvent]:
        for marker in (b"\x11\x22", b"\x33\x44"):
            chunk = AudioChunk(data=marker * 160, format=PCM16_MONO_16K)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    async def stop(self) -> None: ...
    async def cancel(self) -> None: ...


def _silent_chunk() -> AudioChunk:
    return AudioChunk(data=bytes(320), format=PCM16_MONO_16K)


async def _drive_session(*, enable_aec: bool, echo_canceller) -> InMemoryRingBuffer:
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=2048, artifact_store=artifact_store)
    transport = _FakeTransport(chunks_in=[_silent_chunk(), _silent_chunk()])
    session = Session(
        SessionConfig(
            transport=transport,
            vad=_FakeVAD(),
            stt=_FakeSTT(),
            agent=_FakeAgent(),
            tts=_DistinctiveTTS(),
            noise_reducer=PassthroughNoiseReducer(),
            echo_canceller=echo_canceller,
            enable_noise_reduction=False,
            enable_echo_cancellation=enable_aec,
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
            journal=journal,
            artifact_store=artifact_store,
            session_id="aec-ref-test",
        )
    )
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()
    return journal


def _reference_records(journal: InMemoryRingBuffer) -> list:
    return [r for r in journal.read() if r.name == AEC_REFERENCE_FRAME_NAME]


# ── AudioStage.record_reference (unit) ───────────────────────────


def test_record_reference_journals_frame_with_output_ref():
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=64, artifact_store=artifact_store)
    ctx = RunContext(
        run_id="s",
        session_id="s",
        runtime_mode="chained_pipeline",
        journal=journal,
        artifact_store=artifact_store,
    )
    from easycat._turn_context import TurnContext
    from easycat.cancel import CancelToken

    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    stage = AudioStage(PassthroughNoiseReducer(), echo_canceller=_PassthroughAEC())
    chunk = AudioChunk(data=b"\x05\x06" * 160, format=PCM16_MONO_16K)

    stage.record_reference(chunk, ctx, turn)

    refs = _reference_records(journal)
    assert len(refs) == 1
    rec = refs[0]
    assert rec.output_ref is not None
    assert rec.turn_id == "t1"
    assert rec.data.get("stage") == "audio"
    assert rec.data.get("audio_bytes") == len(chunk.data)
    # The captured bytes round-trip through the artifact store.
    assert artifact_store.get(rec.output_ref) == chunk.data


def test_record_reference_skips_when_no_artifact_store():
    journal = InMemoryRingBuffer(capacity=64)  # no artifact store
    ctx = RunContext(
        run_id="s",
        session_id="s",
        runtime_mode="chained_pipeline",
        journal=journal,
        artifact_store=None,
    )
    from easycat._turn_context import TurnContext
    from easycat.cancel import CancelToken

    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    stage = AudioStage(PassthroughNoiseReducer(), echo_canceller=_PassthroughAEC())
    chunk = AudioChunk(data=b"\x05\x06" * 160, format=PCM16_MONO_16K)

    stage.record_reference(chunk, ctx, turn)

    # No artifact store → put_artifact returns None → no record emitted.
    assert _reference_records(journal) == []


# ── End-to-end through a Session ─────────────────────────────────


async def test_journal_has_reference_frames_when_aec_enabled():
    aec = _PassthroughAEC()
    journal = await _drive_session(enable_aec=True, echo_canceller=aec)
    refs = _reference_records(journal)
    assert refs, "AEC enabled: journal must contain aec_reference_frame records"
    for rec in refs:
        assert rec.output_ref is not None
        assert rec.data.get("stage") == "audio"


async def test_journal_has_no_reference_frames_when_aec_disabled():
    journal = await _drive_session(enable_aec=False, echo_canceller=None)
    assert _reference_records(journal) == []


async def test_feed_reference_exception_emits_no_capture_and_keeps_sending():
    """A ``feed_reference`` exception must not emit a capture, must not disable
    the send path, and must still deliver the bot's audio to the transport."""
    aec = _RaisingAEC()
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=2048, artifact_store=artifact_store)
    transport = _FakeTransport(chunks_in=[_silent_chunk(), _silent_chunk()])
    session = Session(
        SessionConfig(
            transport=transport,
            vad=_FakeVAD(),
            stt=_FakeSTT(),
            agent=_FakeAgent(),
            tts=_DistinctiveTTS(),
            noise_reducer=PassthroughNoiseReducer(),
            echo_canceller=aec,
            enable_noise_reduction=False,
            enable_echo_cancellation=True,
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
            journal=journal,
            artifact_store=artifact_store,
            session_id="aec-raise-test",
        )
    )
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    # feed_reference raised → no reference frame was journaled.
    assert _reference_records(journal) == []
    # The bot's TTS audio still reached the transport.
    assert transport.sent, "feed_reference failure must not stop sending audio"
