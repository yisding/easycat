"""Session.play_dtmf_tones: in-band DTMF through the AudioRouter (plan section 4)."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat._audio_utils import resample
from easycat._dtmf_tones import (
    DTMFPlaybackCancelled,
    DTMFToneConfig,
    dtmf_sequence_duration_ms,
    render_dtmf_pcm16,
)
from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import AudioOut, PlaybackMarkAck, TransportAudioDelivered
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.capabilities import PROTECTED_PLAYBACK_CHUNK_ATTR, protected_playback
from easycat.session._media_output import SessionMediaOutput
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from tests._fakes import FakeTransport
from tests.session._session_core_helpers import _full_config, _make_chunk
from tests.telephony._dtmf_decoder import decode_dtmf

_WAIT_S = 5.0


class _RecordingTransport(FakeTransport):
    """Long-lived transport that records the outbound wire order."""

    def __init__(
        self,
        *,
        inbound: list[AudioChunk] | None = None,
        accepted: bool = True,
        tts_send_delay_s: float = 0.0,
        pause_after_tone_frames: int | None = None,
    ) -> None:
        super().__init__(accepted=accepted)
        self.wire: list[tuple[str, Any]] = []
        self._inbound = inbound or []
        self._closed = asyncio.Event()
        self._tts_send_delay_s = tts_send_delay_s
        self._pause_after = pause_after_tone_frames
        self.tone_frames = 0
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()
        self.inbound_consumed = asyncio.Event()
        self.audio_target: int | None = None
        self.audio_target_reached = asyncio.Event()

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._inbound:
            yield chunk
        self.inbound_consumed.set()
        await self._closed.wait()

    async def disconnect(self) -> None:
        self.disconnected = True
        self._closed.set()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        is_tone = bool(getattr(chunk, "_easycat_dtmf_tone", False))
        if not is_tone and self._tts_send_delay_s:
            await asyncio.sleep(self._tts_send_delay_s)
        self.sent.append(chunk)
        self.wire.append(("tone" if is_tone else "audio", chunk))
        if not is_tone and self.kinds().count("audio") == self.audio_target:
            self.audio_target_reached.set()
        if is_tone:
            self.tone_frames += 1
            if self.tone_frames == self._pause_after:
                self.paused.set()
                await self.resume.wait()
        return self.accepted

    async def clear_audio(self) -> None:
        self.clear_calls += 1
        self.wire.append(("clear", None))

    # ── helpers ──
    def tone_pcm(self) -> bytes:
        return b"".join(chunk.data for kind, chunk in self.wire if kind == "tone")

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.wire]


class _Token:
    mark_name: str | None = None


class _GuardTransport(_RecordingTransport):
    """Minimal ProtectedPlayback transport: defers clears while a guard is armed."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.active_token: object | None = None
        self.deferred_clear = False
        self.hold = asyncio.Event()
        self.hold.set()
        self.aborts: list[object | None] = []
        self.max_durations: list[float] = []
        self.guard_future: asyncio.Future[str] | None = None
        self.untagged_tones = 0
        self.ended = asyncio.Event()
        self.fence_result = True

    async def wait_playout_fence(self, *, timeout_s: float) -> bool:
        self.wire.append(("fence", timeout_s))
        return self.fence_result

    def begin_protected_playback(self, *, max_duration_s: float) -> object:
        token = _Token()
        self.active_token = token
        self.max_durations.append(max_duration_s)
        self.wire.append(("begin", None))
        return token

    async def end_protected_playback(self, token: object) -> asyncio.Future[str]:
        assert isinstance(token, _Token)
        assert token is self.active_token
        self.wire.append(("end", "easycat_dtmf_1"))
        token.mark_name = "easycat_dtmf_1"
        self.guard_future = asyncio.get_running_loop().create_future()
        self.ended.set()
        return self.guard_future

    def abort_protected_playback(self, token: object | None) -> None:
        self.aborts.append(token)
        self.active_token = None
        self.hold.set()

    def ack(self, outcome: str = "played") -> None:
        assert self.guard_future is not None
        self.guard_future.set_result(outcome)
        self.active_token = None
        if self.deferred_clear:
            self.deferred_clear = False
            self.wire.append(("clear", None))
        self.hold.set()

    async def clear_audio(self) -> None:
        if self.active_token is not None:
            self.deferred_clear = True
            self.hold.clear()
            self.wire.append(("clear_deferred", None))
            return
        await super().clear_audio()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        is_tone = bool(getattr(chunk, "_easycat_dtmf_tone", False))
        if is_tone:
            if getattr(chunk, PROTECTED_PLAYBACK_CHUNK_ATTR, None) is not self.active_token:
                self.untagged_tones += 1
        else:
            await self.hold.wait()
        return await super().send_audio(chunk)


class _MarkOnlyTransport(_RecordingTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.marks: list[str] = []

    async def send_playback_mark(self, name: str | None = None) -> str:
        mark = name or f"mark_{len(self.marks) + 1}"
        self.marks.append(mark)
        self.wire.append(("mark", mark))
        return mark


class _RecordingAEC:
    def __init__(self) -> None:
        self.references: list[AudioChunk] = []

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        self.references.append(chunk)

    def version_info(self) -> dict[str, str]:
        return {"provider": "recording-aec"}


def _session(transport: FakeTransport, **overrides: Any) -> Session:
    return Session(_full_config(transport=transport, enable_vad=False, **overrides))


def _contiguous(kinds: list[str], kind: str) -> bool:
    positions = [index for index, value in enumerate(kinds) if value == kind]
    return bool(positions) and positions == list(range(positions[0], positions[-1] + 1))


# ── Ordering ──────────────────────────────────────────────────


async def test_queued_tts_goes_out_before_fence_and_tones() -> None:
    transport = _GuardTransport(tts_send_delay_s=0.005)
    async with _session(transport) as session:
        for _ in range(5):
            await session._audio_router.queue_outbound(_make_chunk())
        playback = await session.play_dtmf_tones("12#")
        transport.ack()
        assert await playback.wait(_WAIT_S) == "played"

    kinds = transport.kinds()
    fence = kinds.index("fence")
    assert kinds[:fence] == ["audio"] * 5
    assert kinds[fence + 1] == "begin"
    assert kinds[fence + 2] == "tone"
    assert kinds.index("end") == len(kinds) - 1 - kinds[::-1].index("tone") + 1
    assert transport.untagged_tones == 0
    assert decode_dtmf(transport.tone_pcm()) == "12#"
    assert transport.tone_pcm() == render_dtmf_pcm16("12#")
    assert playback.key_count == 3
    assert playback.duration_ms == dtmf_sequence_duration_ms("12#")
    assert playback.mark_name == "easycat_dtmf_1"
    assert playback.delivery == "in_band"
    assert transport.max_durations == [pytest.approx(playback.duration_ms / 1000 + 5.0)]


async def test_concurrent_tts_never_interleaves_with_tone_frames() -> None:
    transport = _GuardTransport(pause_after_tone_frames=4)
    transport.audio_target = 10
    async with _session(transport) as session:
        play = asyncio.create_task(session.play_dtmf_tones("1234"))
        await asyncio.wait_for(transport.paused.wait(), _WAIT_S)
        for _ in range(10):
            await session._audio_router.queue_outbound(_make_chunk())
        await asyncio.sleep(0.02)
        transport.resume.set()
        playback = await asyncio.wait_for(play, _WAIT_S)
        transport.ack()
        await asyncio.wait_for(transport.audio_target_reached.wait(), _WAIT_S)
        assert await playback.wait(_WAIT_S) == "played"

    kinds = transport.kinds()
    assert _contiguous(kinds, "tone")
    assert kinds.index("end") > max(i for i, k in enumerate(kinds) if k == "tone")
    assert min(i for i, k in enumerate(kinds) if k == "audio") > kinds.index("end")
    assert decode_dtmf(transport.tone_pcm()) == "1234"


async def test_unconfirmed_fence_still_sends_tones() -> None:
    transport = _GuardTransport()
    transport.fence_result = False
    async with _session(transport) as session:
        playback = await session.play_dtmf_tones("5")
        transport.ack()
        assert await playback.wait(_WAIT_S) == "played"
    assert decode_dtmf(transport.tone_pcm()) == "5"


# ── Barge-in and cancellation ─────────────────────────────────


async def test_barge_in_and_caller_cancellation_do_not_split_the_sequence() -> None:
    transport = _GuardTransport(pause_after_tone_frames=3)
    async with _session(transport) as session:
        play = asyncio.create_task(session.play_dtmf_tones("123"))
        await asyncio.wait_for(transport.paused.wait(), _WAIT_S)
        play.cancel()
        await session.cancel_tts_playback()
        transport.resume.set()
        with pytest.raises(asyncio.CancelledError):
            await play
        await asyncio.wait_for(transport.ended.wait(), _WAIT_S)
        # The clear was deferred by the guard, never written mid-sequence.
        assert "clear" not in transport.kinds()
        transport.ack()

    kinds = transport.kinds()
    assert "clear_deferred" in kinds
    assert kinds.index("clear") > kinds.index("end")
    assert _contiguous([k for k in kinds if k != "clear_deferred"], "tone")
    assert decode_dtmf(transport.tone_pcm()) == "123"
    # Only the context-manager force stop aborted (all guards); never the burst.
    assert transport.aborts in ([], [None])


async def test_cancel_tts_playback_on_plain_transport_still_submits_every_tone() -> None:
    """The router keeps submitting the burst after a mid-sequence barge-in.

    This covers submission only: a plain transport has no ``ProtectedPlayback``
    guard, so its ``clear`` (recorded mid-sequence here) may still discard
    tone frames it had buffered. A protected-playback transport (see
    ``test_barge_in_and_caller_cancellation_do_not_split_the_sequence``)
    instead defers the ``clear`` until the tones are acknowledged or the
    guard expires; neither test proves what the far end actually heard.
    """
    transport = _RecordingTransport(pause_after_tone_frames=2)
    async with _session(transport) as session:
        play = asyncio.create_task(session.play_dtmf_tones("98"))
        await asyncio.wait_for(transport.paused.wait(), _WAIT_S)
        await session.cancel_tts_playback()
        transport.resume.set()
        playback = await asyncio.wait_for(play, _WAIT_S)
        assert playback.mark_name is None
    kinds = transport.kinds()
    first_tone = kinds.index("tone")
    last_tone = len(kinds) - 1 - kinds[::-1].index("tone")
    assert first_tone < kinds.index("clear") < last_tone
    assert decode_dtmf(transport.tone_pcm()) == "98"


async def test_bursts_are_serialized() -> None:
    transport = _GuardTransport()
    async with _session(transport) as session:
        first, second = await asyncio.gather(
            session.play_dtmf_tones("1"), session.play_dtmf_tones("2")
        )
        transport.ack()
    kinds = transport.kinds()
    assert kinds.count("fence") == 2
    # Each burst is fence, begin, tones, end with nothing in between.
    first_end = kinds.index("end")
    assert kinds[first_end + 1] == "fence"
    assert decode_dtmf(transport.tone_pcm()) == "12"
    assert first.key_count == second.key_count == 1


async def test_force_stop_releases_held_send_promptly() -> None:
    transport = _GuardTransport()
    session = _session(transport)
    await session.start()
    playback = await session.play_dtmf_tones("7")
    # Guard still armed (no ack): a barge-in clear is deferred and later TTS
    # is held behind it, pinning the send lock inside the drain task.
    await session.cancel_tts_playback()
    await session._audio_router.queue_outbound(_make_chunk())
    await asyncio.sleep(0.02)
    assert not transport.hold.is_set()

    await asyncio.wait_for(session.stop(force=True), 2.0)

    assert None in transport.aborts
    assert await playback.wait(0) == "disconnected"


async def test_graceful_stop_settles_unacknowledged_playback() -> None:
    transport = _GuardTransport()
    session = _session(transport)
    await session.start()
    playback = await session.play_dtmf_tones("8")
    await asyncio.wait_for(session.stop(), _WAIT_S)
    assert await playback.wait(0) == "disconnected"


# ── AEC reference, AudioOut, privacy ──────────────────────────


async def test_aec_receives_tone_reference_at_the_near_end_rate() -> None:
    aec = _RecordingAEC()
    inbound = [AudioChunk(data=bytes(640), format=PCM16_MONO_16K)]
    transport = _RecordingTransport(inbound=inbound)
    async with _session(transport, echo_canceller=aec, enable_echo_cancellation=True) as session:
        await asyncio.wait_for(transport.inbound_consumed.wait(), _WAIT_S)
        await asyncio.sleep(0.01)
        playback = await session.play_dtmf_tones("4")

    rendered = render_dtmf_pcm16("4")
    assert aec.references
    assert {ref.format.sample_rate for ref in aec.references} == {16000}
    reference_ms = sum(ref.duration_ms for ref in aec.references)
    assert reference_ms == pytest.approx(playback.duration_ms, abs=len(aec.references))
    assert len(rendered) == playback.duration_ms * 16


class _RateLatchingAEC(_RecordingAEC):
    """Pins its rate to the first stream it sees, like LiveKitAEC."""

    def __init__(self) -> None:
        super().__init__()
        self.rate: int | None = None
        self.processed: list[AudioChunk] = []

    def _check(self, chunk: AudioChunk) -> None:
        if self.rate is None:
            self.rate = chunk.format.sample_rate
        elif chunk.format.sample_rate != self.rate:
            raise ValueError("near/far sample-rate mismatch")

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        self._check(chunk)
        self.processed.append(chunk)
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        self._check(chunk)
        super().feed_reference(chunk)


async def test_tone_before_first_mic_frame_does_not_pin_the_aec_rate() -> None:
    """With no near-end rate yet, the tone reference is skipped, not fed at 8 kHz."""
    aec = _RateLatchingAEC()
    transport = _RecordingTransport()
    async with _session(transport, echo_canceller=aec, enable_echo_cancellation=True) as session:
        await session.play_dtmf_tones("4")
        assert aec.references == []
        assert aec.rate is None
        await session._audio_router._process_chunk(
            AudioChunk(data=bytes(640), format=PCM16_MONO_16K)
        )
        assert aec.rate == 16000
        await session.play_dtmf_tones("4")
    assert aec.processed
    assert aec.references
    assert {ref.format.sample_rate for ref in aec.references} == {16000}


@pytest.mark.parametrize("redaction", ["secrets", "pii"])
async def test_audio_out_has_no_turn_and_is_masked_under_pii(redaction: str) -> None:
    transport = _RecordingTransport()
    store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer()
    received: list[AudioOut] = []
    session = _session(
        transport,
        journal=journal,
        artifact_store=store,
        journal_redaction=redaction,
    )
    session.subscribe_event(AudioOut, received.append)
    captured = b""
    async with session:
        await session.start_turn()
        assert session.current_turn is not None
        await session.play_dtmf_tones("0")
        # Read the verbose transport captures before stop() closes the store.
        for record in journal.read():
            data = record.data or {}
            if record.name == "stage_complete" and data.get("stage") == "transport":
                blob = store.get(record.output_ref) if record.output_ref else None
                captured += blob or b""

    tone_pcm = render_dtmf_pcm16("0")
    assert received
    assert all(event.turn_id is None for event in received)
    assert all(event.session_id == session.session_id for event in received)
    out_pcm = b"".join(event.chunk.data for event in received)
    assert len(out_pcm) == len(tone_pcm)
    # The wire always carries the real tones.
    assert transport.tone_pcm() == tone_pcm

    assert len(captured) == len(tone_pcm)
    if redaction == "pii":
        assert out_pcm == bytes(len(tone_pcm))
        assert captured == bytes(len(tone_pcm))
    else:
        assert out_pcm == tone_pcm
        assert captured == tone_pcm


async def test_journal_records_sent_and_completed_without_digits() -> None:
    transport = _GuardTransport()
    journal = InMemoryRingBuffer()
    async with _session(transport, journal=journal) as session:
        playback = await session.play_dtmf_tones("9731#")
        transport.ack()
        assert await playback.wait(_WAIT_S) == "played"
        await asyncio.sleep(0)

    records = {record.name: record for record in journal.read()}
    sent = records["dtmf_tones_sent"]
    completed = records["dtmf_tones_completed"]
    assert sent.turn_id is None
    assert sent.data == {
        "tone_count": 5,
        "duration_ms": playback.duration_ms,
        "mark_name": "easycat_dtmf_1",
        "transport_kind": sent.data["transport_kind"],
        "fence": True,
    }
    assert completed.data == {"mark_name": "easycat_dtmf_1", "outcome": "played"}
    for record in journal.read():
        data = record.data or {}
        assert "digits" not in data
        assert "9731#" not in data.values()
        if record.name.startswith("dtmf_"):
            assert "9731" not in repr(data)


class _ReportingTransport(_RecordingTransport):
    """Delivery-reporting transport that re-chunks like LocalTransport/WebRTC.

    The delivered chunk is a fresh ``AudioChunk`` carrying only the routing
    fields a real reporting transport copies, never ad-hoc attributes.
    """

    reports_audio_delivery = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bus: Any = None

    async def send_audio(self, chunk: AudioChunk) -> bool:
        accepted = await super().send_audio(chunk)
        delivered = AudioChunk(
            data=chunk.data,
            format=chunk.format,
            timestamp=chunk.timestamp,
            _easycat_dtmf_tone=getattr(chunk, "_easycat_dtmf_tone", False) is True,
        )
        await self.bus.emit(
            TransportAudioDelivered(
                chunk=delivered,
                session_id=getattr(chunk, "_easycat_session_id", None),
                turn_id=getattr(chunk, "_easycat_turn_id", None),
                turn_ref=getattr(chunk, "_easycat_turn_ref", None),
            )
        )
        return accepted


async def test_reporting_transport_keeps_tones_out_of_the_turn_and_masks_them() -> None:
    transport = _ReportingTransport()
    received: list[AudioOut] = []
    session = _session(transport, journal_redaction="pii")
    transport.bus = session.event_bus
    session.subscribe_event(AudioOut, received.append)
    async with session:
        await session.start_turn()
        turn = session.current_turn
        assert turn is not None
        bytes_before = turn.audio_bytes_sent
        await session.play_dtmf_tones("0")
        await asyncio.sleep(0)
        assert turn.audio_bytes_sent == bytes_before

    tone_pcm = render_dtmf_pcm16("0")
    assert received
    assert all(event.turn_id is None for event in received)
    out_pcm = b"".join(event.chunk.data for event in received)
    assert out_pcm == bytes(len(tone_pcm))
    assert transport.tone_pcm() == tone_pcm


class _DrainingTransport(_ReportingTransport):
    """Reporting transport with an AEC reference drain queue.

    ``flag_frames=False`` mimics LocalTransport, whose playout callback
    reslices audio into bare bytes, so drained frames lose the tone flag.
    """

    def __init__(self, *, flag_frames: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._flag_frames = flag_frames
        self.reference_queue: list[AudioChunk] = []

    async def send_audio(self, chunk: AudioChunk) -> bool:
        accepted = await super().send_audio(chunk)
        flag = self._flag_frames and getattr(chunk, "_easycat_dtmf_tone", False) is True
        self.reference_queue.append(
            AudioChunk(data=chunk.data, format=chunk.format, _easycat_dtmf_tone=flag)
        )
        return accepted

    def drain_aec_reference_frames(self) -> list[AudioChunk]:
        frames, self.reference_queue = self.reference_queue, []
        return frames


@pytest.mark.parametrize("flag_frames", [True, False], ids=["webrtc-like", "local-like"])
@pytest.mark.parametrize("redaction", ["pii", "secrets"])
async def test_drained_aec_reference_capture_is_masked_under_pii(
    flag_frames: bool, redaction: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Journal every drained frame, not the decimated one-per-second sample.
    monkeypatch.setattr("easycat.session._audio_router._AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES", 1)
    aec = _RecordingAEC()
    transport = _DrainingTransport(flag_frames=flag_frames)
    session = _session(
        transport,
        echo_canceller=aec,
        enable_echo_cancellation=True,
        artifact_store=InMemoryArtifactStore(),
        capture_aec_reference=True,
        journal_redaction=redaction,
    )
    transport.bus = session.event_bus
    captured: list[AudioChunk] = []
    async with session:
        router = session._audio_router

        async def _record(chunk: AudioChunk, *_args: Any) -> None:
            captured.append(chunk)

        router._audio_stage.record_reference = _record  # type: ignore[method-assign]
        await session.play_dtmf_tones("5")
        tone_frames = list(transport.reference_queue)
        mic = AudioChunk(data=bytes(320), format=PCM16_MONO_8K)
        await router._feed_transport_aec_reference(mic, None)

    tone_pcm = render_dtmf_pcm16("5")
    # The canceller always receives the real far-end tones.
    assert b"".join(ref.data for ref in aec.references) == tone_pcm
    assert tone_frames and captured
    for chunk in captured:
        if redaction == "pii":
            assert chunk.data == bytes(len(chunk.data))
        else:
            assert chunk.data in {frame.data for frame in tone_frames}
    if redaction != "pii":
        assert any(chunk.data != bytes(len(chunk.data)) for chunk in captured)


async def test_local_flagged_reference_stays_masked_after_the_mask_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-frame tone flag masks a capture drained after the 1 s time window."""
    np = pytest.importorskip("numpy")
    from easycat.transports.local import LocalTransport, LocalTransportConfig

    # Journal every drained frame, not the decimated one-per-second sample.
    monkeypatch.setattr("easycat.session._audio_router._AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES", 1)
    aec = _RecordingAEC()
    transport = LocalTransport(LocalTransportConfig(output_preroll_frames=0))
    session = _session(
        transport,
        echo_canceller=aec,
        enable_echo_cancellation=True,
        artifact_store=InMemoryArtifactStore(),
        capture_aec_reference=True,
        journal_redaction="pii",
    )
    router = session._audio_router
    captured: list[AudioChunk] = []

    async def _record(chunk: AudioChunk, *_args: Any) -> None:
        captured.append(chunk)

    router._audio_stage.record_reference = _record  # type: ignore[method-assign]
    transport._connected = True
    transport.drain_aec_reference_frames()  # the router's first drain arms capture
    fmt = transport.audio_format
    tone_pcm = resample(render_dtmf_pcm16("5"), PCM16_MONO_8K.sample_rate, fmt.sample_rate)
    frame_samples = transport._frame_samples
    frame_bytes = frame_samples * fmt.frame_size
    tone_pcm = tone_pcm[: len(tone_pcm) - len(tone_pcm) % frame_bytes]
    # Tagged as the router tags tone frames, played out by the speaker callback.
    await transport.send_audio(AudioChunk(data=tone_pcm, format=fmt, _easycat_dtmf_tone=True))
    for _ in range(len(tone_pcm) // frame_bytes):
        outdata = np.zeros((frame_samples, 1), dtype=np.float32)
        transport._output_callback(np, outdata, frame_samples, None, None)

    # The burst and its 1 s masking tail are over: only the flag can mask.
    loop = asyncio.get_running_loop()
    router._tone_capture_mask_until = loop.time() - 1.0
    assert router._dtmf_bursts_sending == 0
    await router._feed_transport_aec_reference(AudioChunk(data=bytes(320), format=fmt), None)

    assert loop.time() >= router._tone_capture_mask_until
    assert captured and all(chunk.data == bytes(len(chunk.data)) for chunk in captured)
    assert b"".join(chunk.data for chunk in captured) == bytes(len(tone_pcm))
    # The canceller always receives the real far-end tones.
    assert b"".join(ref.data for ref in aec.references) == tone_pcm


class _BoundedPlayoutTransport(_RecordingTransport):
    """Reporting transport with a bounded playout queue, like WebRTC's.

    ``send_audio`` rejects a frame when the queue is full; a playout task
    drains one frame per tick and reports it delivered.
    """

    reports_audio_delivery = True

    def __init__(self, *, capacity: int, tick_s: float = 0.001, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bus: Any = None
        self.capacity = capacity
        self.tick_s = tick_s
        self.queue: asyncio.Queue[AudioChunk] = asyncio.Queue()
        self.rejected = 0
        self.max_depth = 0
        self._playout: asyncio.Task[None] | None = None

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if self.queue.qsize() >= self.capacity:
            self.rejected += 1
            return False
        accepted = await super().send_audio(chunk)
        self.queue.put_nowait(chunk)
        self.max_depth = max(self.max_depth, self.queue.qsize())
        if self._playout is None:
            self._playout = asyncio.create_task(self._play())
        return accepted

    async def _play(self) -> None:
        while True:
            chunk = await self.queue.get()
            await asyncio.sleep(self.tick_s)
            await self.bus.emit(
                TransportAudioDelivered(
                    chunk=AudioChunk(
                        data=chunk.data,
                        format=chunk.format,
                        _easycat_dtmf_tone=getattr(chunk, "_easycat_dtmf_tone", False) is True,
                    ),
                    session_id=getattr(chunk, "_easycat_session_id", None),
                )
            )

    async def disconnect(self) -> None:
        if self._playout is not None:
            self._playout.cancel()
        await super().disconnect()


async def test_long_burst_is_paced_to_a_bounded_playout_queue() -> None:
    # 11 keys is about 2.3 s of 20 ms frames: more than a 30-frame queue holds.
    transport = _BoundedPlayoutTransport(capacity=30)
    session = _session(transport)
    transport.bus = session.event_bus
    async with session:
        playback = await session.play_dtmf_tones("12345678901")
        assert await playback.wait(timeout=_WAIT_S) == "estimated"

    assert transport.rejected == 0
    assert transport.max_depth <= 30
    assert decode_dtmf(transport.tone_pcm()) == "12345678901"


class _ManualPlayoutTransport(_RecordingTransport):
    """Reporting transport with a bounded queue that plays only when told to.

    No pending-playout hook (like WebRTC), so a burst does not wait for an
    earlier burst's frames to play before it starts.
    """

    reports_audio_delivery = True

    def __init__(self, *, capacity: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bus: Any = None
        self.capacity = capacity
        self.queue: list[AudioChunk] = []
        self.rejected = 0
        self.max_depth = 0

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if len(self.queue) >= self.capacity:
            self.rejected += 1
            return False
        accepted = await super().send_audio(chunk)
        self.queue.append(chunk)
        self.max_depth = max(self.max_depth, len(self.queue))
        return accepted

    async def play_one(self) -> None:
        if not self.queue:
            return
        chunk = self.queue.pop(0)
        await self.bus.emit(
            TransportAudioDelivered(
                chunk=AudioChunk(
                    data=chunk.data,
                    format=chunk.format,
                    _easycat_dtmf_tone=getattr(chunk, "_easycat_dtmf_tone", False) is True,
                ),
                session_id=getattr(chunk, "_easycat_session_id", None),
            )
        )

    async def clear_audio(self) -> None:
        self.queue.clear()
        await super().clear_audio()


def _play_while_pacing_waits(router: Any, transport: _ManualPlayoutTransport) -> list[int]:
    """Play one queued frame whenever tone pacing waits for playout progress."""
    depths: list[int] = []
    original_wait = router._tone_progress.wait

    async def wait_with_playout() -> bool:
        depths.append(len(transport.queue))
        await transport.play_one()
        return await original_wait()

    router._tone_progress.wait = wait_with_playout
    return depths


async def test_back_to_back_bursts_stay_within_the_pacing_window() -> None:
    """A burst counts the earlier burst's unplayed frames instead of starting at zero."""
    # One key is 13 frames (260 ms); three back-to-back bursts would hold 39
    # frames while the 500 ms window allows 25.
    transport = _ManualPlayoutTransport(capacity=30)
    session = _session(transport)
    transport.bus = session.event_bus
    async with session:
        router = session._audio_router
        waits = _play_while_pacing_waits(router, transport)
        for key in "123":
            await asyncio.wait_for(session.play_dtmf_tones(key), _WAIT_S)
        assert waits
        assert router._tone_ms_in_flight == pytest.approx(20 * len(transport.queue))
    assert transport.rejected == 0
    assert transport.max_depth <= 25
    assert decode_dtmf(transport.tone_pcm()) == "123"


async def test_a_transport_clear_forgets_unplayed_tone_frames() -> None:
    """Cleared frames never report played, so a clear drops their pacing share."""
    transport = _ManualPlayoutTransport(capacity=60)
    session = _session(transport)
    transport.bus = session.event_bus
    async with session:
        router = session._audio_router
        waits = _play_while_pacing_waits(router, transport)
        await asyncio.wait_for(session.play_dtmf_tones("1"), _WAIT_S)
        assert router._tone_ms_in_flight > 0
        await session.cancel_tts_playback()
        assert transport.queue == []
        assert router._tone_ms_in_flight == 0.0
        # The next burst gets the whole window again: 25 frames before a wait.
        await asyncio.wait_for(session.play_dtmf_tones("234"), _WAIT_S)
        assert waits and waits[0] == 25


async def test_a_pacing_stall_drops_the_stale_bookings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio that never reports played (an outside clear) costs one stall, not one per frame."""
    monkeypatch.setattr("easycat.session._audio_router._TONE_PACING_STALL_S", 0)
    transport = _ManualPlayoutTransport(capacity=60)  # never plays
    session = _session(transport)
    transport.bus = session.event_bus
    waits = 0
    async with session:
        router = session._audio_router
        original_wait = router._tone_progress.wait

        def counting_wait() -> Any:
            nonlocal waits
            waits += 1
            return original_wait()

        router._tone_progress.wait = counting_wait
        # Four keys are 43 frames: one stall at frame 26, then a fresh window.
        await asyncio.wait_for(session.play_dtmf_tones("1234"), _WAIT_S)
    assert waits == 1
    assert decode_dtmf(transport.tone_pcm()) == "1234"


class _Fixed16kTransport(_RecordingTransport):
    """Plays every frame at its own 16 kHz rate, ignoring ``chunk.format``."""

    audio_format = PCM16_MONO_16K


class _PreferredStereoTransport(_RecordingTransport):
    preferred_tts_output_format = AudioFormat(sample_rate=16000, channels=2, sample_width=2)


async def test_tones_are_rendered_in_the_transport_output_format() -> None:
    transport = _Fixed16kTransport()
    async with _session(transport) as session:
        playback = await session.play_dtmf_tones("159#")

    frames = [chunk for kind, chunk in transport.wire if kind == "tone"]
    assert {frame.format for frame in frames} == {PCM16_MONO_16K}
    # 20 ms frames at the transport rate (the last one may be shorter).
    assert {len(frame.data) for frame in frames[:-1]} == {640}
    pcm = transport.tone_pcm()
    assert len(pcm) == playback.duration_ms * 32
    assert decode_dtmf(resample(pcm, 16000, 8000)) == "159#"


async def test_tones_follow_a_multichannel_preferred_format() -> None:
    transport = _PreferredStereoTransport()
    async with _session(transport) as session:
        await session.play_dtmf_tones("7")

    frames = [chunk for kind, chunk in transport.wire if kind == "tone"]
    assert {frame.format.channels for frame in frames} == {2}
    stereo = transport.tone_pcm()
    left = b"".join(stereo[i : i + 2] for i in range(0, len(stereo), 4))
    right = b"".join(stereo[i + 2 : i + 4] for i in range(0, len(stereo), 4))
    assert left == right
    assert decode_dtmf(resample(left, 16000, 8000)) == "7"


# ── Stop from inside the outbound path ────────────────────────


@pytest.mark.parametrize("handler_kind", ["audio_out", "error"])
async def test_stop_from_an_outbound_handler_does_not_wait_on_a_pending_burst(
    handler_kind: str,
) -> None:
    """A stop() run on the drain task must not join a burst that needs its lock."""
    transport = _RecordingTransport(tts_send_delay_s=0.01)
    if handler_kind == "error":

        async def failing_send(chunk: AudioChunk) -> bool:
            if not getattr(chunk, "_easycat_dtmf_tone", False):
                await asyncio.sleep(0.01)
                raise RuntimeError("send failed")
            return True

        transport.send_audio = failing_send  # type: ignore[method-assign]
    session = _session(transport)
    state: dict[str, bool] = {}
    stopped = asyncio.Event()

    async def stop_once(event: Any) -> None:
        if state.get("called") or getattr(getattr(event, "chunk", None), "_easycat_dtmf_tone", 0):
            return
        state["called"] = True
        await session.stop()
        state["done"] = True
        stopped.set()

    from easycat.events import Error

    session.subscribe_event(AudioOut if handler_kind == "audio_out" else Error, stop_once)
    await session.start()
    await session._audio_router.queue_outbound(_make_chunk())
    play = asyncio.create_task(session.play_dtmf_tones("1"))

    await asyncio.wait_for(stopped.wait(), _WAIT_S)
    assert state == {"called": True, "done": True}
    results = await asyncio.gather(play, return_exceptions=True)
    assert isinstance(results[0], (asyncio.CancelledError, RuntimeError))


async def test_stop_from_a_tone_handler_does_not_wait_on_a_queued_burst() -> None:
    """A tone's AudioOut handler runs on the burst that holds the DTMF lock."""
    transport = _RecordingTransport()
    session = _session(transport)
    router = session._audio_router
    state: dict[str, bool] = {}
    stopped = asyncio.Event()

    async def stop_on_first_tone(event: AudioOut) -> None:
        if state.get("called") or not getattr(event.chunk, "_easycat_dtmf_tone", False):
            return
        state["called"] = True
        # Let the second burst queue up behind the lock this task holds.
        for _ in range(200):
            if len(router._runtime_scope.tasks(router._DTMF_SEND_TASK_NAME)) >= 2:
                break
            await asyncio.sleep(0)
        assert len(router._runtime_scope.tasks(router._DTMF_SEND_TASK_NAME)) == 2
        await session.stop()
        stopped.set()

    session.subscribe_event(AudioOut, stop_on_first_tone)
    await session.start()
    first = asyncio.create_task(session.play_dtmf_tones("1"))
    second = asyncio.create_task(session.play_dtmf_tones("2"))

    # Without the fix stop() joins the queued burst for its whole budget
    # (drain wait + fence timeout + tones + guard margin, about 20 s).
    await asyncio.wait_for(stopped.wait(), 2.0)
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[1], (asyncio.CancelledError, RuntimeError))
    assert "2" not in decode_dtmf(transport.tone_pcm())


@pytest.mark.parametrize("handler_on", ["tts_frame", "tone_frame"])
async def test_play_dtmf_tones_from_an_outbound_handler_is_rejected(handler_on: str) -> None:
    """A burst awaited on the send-lock holder would deadlock; it raises instead."""
    transport = _RecordingTransport()
    session = _session(transport)
    errors: list[BaseException] = []
    handled = asyncio.Event()

    async def nested_burst(event: AudioOut) -> None:
        is_tone = bool(getattr(event.chunk, "_easycat_dtmf_tone", False))
        if handled.is_set() or is_tone != (handler_on == "tone_frame"):
            return
        try:
            await session.play_dtmf_tones("2")
        except RuntimeError as exc:
            errors.append(exc)
        handled.set()

    session.subscribe_event(AudioOut, nested_burst)
    async with session:
        if handler_on == "tts_frame":
            await session._audio_router.queue_outbound(_make_chunk())
        else:
            await session.play_dtmf_tones("1")
        await asyncio.wait_for(handled.wait(), 2.0)

    assert len(errors) == 1
    message = str(errors[0])
    assert "asyncio.create_task" in message
    assert "2" not in message
    assert "2" not in decode_dtmf(transport.tone_pcm())


async def test_mark_acknowledged_before_send_returns_leaves_no_pending_entry() -> None:
    transport = _MarkOnlyTransport()
    async with _session(transport) as session:
        router = session._audio_router
        send_mark = transport.send_playback_mark

        async def ack_inline(name: str | None = None) -> str:
            mark = await send_mark(name)
            router.on_playback_ack(PlaybackMarkAck(mark_name=mark, session_id=session.session_id))
            return mark

        transport.send_playback_mark = ack_inline  # type: ignore[method-assign]
        playback = await session.play_dtmf_tones("3")
        assert await playback.wait(0) == "played"
        assert router._dtmf_pending == {}
        assert router._dtmf_mark_waiters == {}


# ── Completion paths ──────────────────────────────────────────


async def test_mark_only_transport_resolves_through_router_waiter() -> None:
    transport = _MarkOnlyTransport()
    async with _session(transport) as session:
        playback = await session.play_dtmf_tones("3")
        assert playback.mark_name == "easycat_dtmf_1"
        assert transport.kinds()[-1] == "mark"
        assert not playback.done()
        await session.event_bus.emit(
            PlaybackMarkAck(mark_name="easycat_dtmf_1", session_id=session.session_id)
        )
        assert await playback.wait(_WAIT_S) == "played"


async def test_mark_only_transport_locally_retired_mark_is_not_played() -> None:
    """A mark the transport expired itself (clear/stop/close) confirms nothing."""
    transport = _MarkOnlyTransport()
    async with _session(transport) as session:
        playback = await session.play_dtmf_tones("3")
        assert playback.mark_name is not None
        await session.event_bus.emit(
            PlaybackMarkAck(
                mark_name=playback.mark_name,
                session_id=session.session_id,
                retired=True,
            )
        )
        assert await playback.wait(_WAIT_S) == "disconnected"
        assert session._audio_router._dtmf_mark_waiters == {}


async def test_mark_only_transport_times_out_without_ack() -> None:
    transport = _MarkOnlyTransport()
    config = DTMFToneConfig(tone_ms=40, gap_ms=40, lead_in_ms=0, tail_ms=0)
    async with _session(transport) as session:
        router = session._audio_router
        chunks = [AudioChunk(data=render_dtmf_pcm16("3", config=config), format=PCM16_MONO_8K)]
        playback = await router.send_protected_sequence(
            chunks, key_count=1, duration_ms=40, fence_timeout_s=1.0, guard_margin_s=0.0
        )
        assert await playback.wait(_WAIT_S) == "timeout"
        assert playback.done()


async def test_bare_transport_reports_estimated_completion() -> None:
    transport = _RecordingTransport()
    config = DTMFToneConfig(tone_ms=40, gap_ms=40, lead_in_ms=0, tail_ms=0)
    async with _session(transport) as session:
        playback = await session.play_dtmf_tones("*", tone_config=config)
        assert playback.mark_name is None
        assert protected_playback(transport) is None
        assert await playback.wait(_WAIT_S) == "estimated"


# ── Rejections ────────────────────────────────────────────────


async def test_raises_when_not_running() -> None:
    session = _session(_RecordingTransport())
    with pytest.raises(RuntimeError, match="not running"):
        await session.play_dtmf_tones("1")


async def test_raises_after_stop() -> None:
    transport = _RecordingTransport()
    session = _session(transport)
    await session.start()
    await session.stop()
    with pytest.raises(RuntimeError, match="stopping or has been stopped"):
        await session.play_dtmf_tones("1")


async def test_raises_in_text_session_mode() -> None:
    session = Session(SessionConfig(runtime_mode="text_session"))
    with pytest.raises(RuntimeError, match="text_session"):
        await session.play_dtmf_tones("1")


@pytest.mark.parametrize("digits", ['1"/><Say>', "١٢", "", "a"])
async def test_invalid_digits_are_rejected_without_echo(digits: str) -> None:
    transport = _RecordingTransport()
    async with _session(transport) as session:
        with pytest.raises(ValueError) as excinfo:
            await session.play_dtmf_tones(digits)
    if len(digits) > 1:
        assert digits not in str(excinfo.value)
    assert transport.wire == []


@pytest.mark.parametrize("fence_timeout_s", [0, -1.0, float("inf"), float("nan"), True])
async def test_invalid_fence_timeout_is_rejected(fence_timeout_s: Any) -> None:
    async with _session(_RecordingTransport()) as session:
        with pytest.raises(ValueError, match="fence_timeout_s"):
            await session.play_dtmf_tones("1", fence_timeout_s=fence_timeout_s)


async def test_rejected_frame_aborts_guard_and_raises() -> None:
    transport = _GuardTransport(accepted=False)
    async with _session(transport) as session:
        with pytest.raises(RuntimeError, match="transport rejected DTMF audio"):
            await session.play_dtmf_tones("12")
        assert len(transport.aborts) == 1
        assert transport.aborts[0] is not None
        assert "end" not in transport.kinds()
        assert transport.kinds().count("tone") == 1


# ── SessionMediaOutput ────────────────────────────────────────


async def test_media_output_delegates_to_the_session() -> None:
    transport = _RecordingTransport()
    async with _session(transport) as session:
        output = SessionMediaOutput(session)
        assert output.session is session
        playback = await output.play_dtmf_tones("6", inter_digit_delay_ms=0)
        assert playback.key_count == 1
    assert decode_dtmf(transport.tone_pcm()) == "6"


async def test_media_output_does_not_keep_the_session_alive() -> None:
    session = _session(_RecordingTransport())
    output = SessionMediaOutput(session)
    del session
    gc.collect()
    assert output.session is None
    with pytest.raises(RuntimeError, match="gone"):
        await output.play_dtmf_tones("1")
    with pytest.raises(RuntimeError, match="gone"):
        await output.synthesize_bypass("hello")


# ── Cancellation before the first frame (should_continue) ────


class _BlockingFenceTransport(_GuardTransport):
    """Guard transport whose playout fence waits until the test releases it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fence_started = asyncio.Event()
        self.fence_release = asyncio.Event()

    async def wait_playout_fence(self, *, timeout_s: float) -> bool:
        self.wire.append(("fence", timeout_s))
        self.fence_started.set()
        await self.fence_release.wait()
        return self.fence_result


async def test_in_band_ivr_sender_called_off_during_fence_sends_no_tone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A HUMAN transition while the fence is pending never presses the key."""
    from easycat.telephony.ivr import InBandDTMFDelivery

    transport = _BlockingFenceTransport(tts_send_delay_s=0.005)
    active = True
    async with _session(transport) as session:
        delivery = InBandDTMFDelivery()
        delivery.bind_output(SessionMediaOutput(session))
        for _ in range(3):
            await session._audio_router.queue_outbound(_make_chunk())
        with caplog.at_level("DEBUG"):
            sending = asyncio.create_task(
                delivery.send_dtmf_with_retry("4321", should_continue=lambda: active)
            )
            await asyncio.wait_for(transport.fence_started.wait(), _WAIT_S)
            active = False
            transport.fence_release.set()
            assert await asyncio.wait_for(sending, _WAIT_S) is False

    kinds = transport.kinds()
    assert kinds[:3] == ["audio"] * 3
    assert "tone" not in kinds
    assert "begin" not in kinds
    assert transport.tone_frames == 0
    assert not any("4321" in message for message in caplog.messages)


def _gate_drain(router: Any) -> tuple[asyncio.Event, asyncio.Event]:
    """Make the router's drain wait signal entry and block until released."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original = router.await_drain

    async def gated_drain(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    router.await_drain = gated_drain
    return entered, release


async def test_called_off_during_drain_skips_the_fence() -> None:
    transport = _GuardTransport()
    active = True
    async with _session(transport) as session:
        entered, release = _gate_drain(session._audio_router)
        playing = asyncio.create_task(session.play_dtmf_tones("9", should_continue=lambda: active))
        # The session-level check has passed once the burst waits on the drain.
        await asyncio.wait_for(entered.wait(), _WAIT_S)
        active = False
        release.set()
        with pytest.raises(DTMFPlaybackCancelled) as excinfo:
            await asyncio.wait_for(playing, _WAIT_S)
    assert "9" not in str(excinfo.value)
    kinds = transport.kinds()
    assert "fence" not in kinds
    assert "begin" not in kinds
    assert "tone" not in kinds


class _StreamGuardTransport(_BlockingFenceTransport):
    """Guard transport that names its active stream, like Twilio's streamSid."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stream_id: str | None = "MZ-first"

    def protected_playback_stream_id(self) -> str | None:
        return self.stream_id


async def test_burst_queued_across_a_stream_replacement_is_called_off() -> None:
    transport = _StreamGuardTransport()
    transport.fence_release.set()
    async with _session(transport) as session:
        entered, release = _gate_drain(session._audio_router)
        playing = asyncio.create_task(session.play_dtmf_tones("7"))
        await asyncio.wait_for(entered.wait(), _WAIT_S)
        transport.stream_id = "MZ-replacement"
        release.set()
        with pytest.raises(DTMFPlaybackCancelled) as excinfo:
            await asyncio.wait_for(playing, _WAIT_S)
    assert "7" not in str(excinfo.value)
    kinds = transport.kinds()
    assert "fence" not in kinds
    assert "begin" not in kinds
    assert "tone" not in kinds


@pytest.mark.parametrize("replaced", [True, False], ids=["replaced", "same-stream"])
async def test_stream_replacement_during_the_fence_sends_no_tone(replaced: bool) -> None:
    transport = _StreamGuardTransport()
    async with _session(transport) as session:
        playing = asyncio.create_task(session.play_dtmf_tones("7"))
        await asyncio.wait_for(transport.fence_started.wait(), _WAIT_S)
        if replaced:
            transport.stream_id = "MZ-replacement"
        transport.fence_release.set()
        if replaced:
            with pytest.raises(DTMFPlaybackCancelled):
                await asyncio.wait_for(playing, _WAIT_S)
        else:
            await asyncio.wait_for(playing, _WAIT_S)
    kinds = transport.kinds()
    if replaced:
        assert "begin" not in kinds
        assert "tone" not in kinds
    else:
        assert decode_dtmf(transport.tone_pcm()) == "7"


async def test_stream_replaced_while_rendering_calls_the_burst_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The burst binds to the stream active at the call, not after rendering."""
    from easycat.session import _session as session_module

    transport = _StreamGuardTransport()
    transport.fence_release.set()
    original_render = session_module.render_dtmf_pcm16_async

    async def render_across_a_replacement(*args: Any, **kwargs: Any) -> bytes:
        pcm = await original_render(*args, **kwargs)
        transport.stream_id = "MZ-replacement"
        return pcm

    monkeypatch.setattr(session_module, "render_dtmf_pcm16_async", render_across_a_replacement)
    async with _session(transport) as session:
        with pytest.raises(DTMFPlaybackCancelled) as excinfo:
            await asyncio.wait_for(session.play_dtmf_tones("7"), _WAIT_S)
    assert "7" not in str(excinfo.value)
    kinds = transport.kinds()
    assert "fence" not in kinds
    assert "begin" not in kinds
    assert "tone" not in kinds


class _FlakyStreamIdTransport(_StreamGuardTransport):
    """Stream-identity hook that raises on its first read only."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stream_reads = 0

    def protected_playback_stream_id(self) -> str | None:
        self.stream_reads += 1
        if self.stream_reads == 1:
            raise RuntimeError("stream identity unavailable")
        return super().protected_playback_stream_id()


async def test_raising_stream_id_hook_calls_the_burst_off() -> None:
    """A present hook that raises leaves the stream unknown, so nothing is sent."""
    transport = _FlakyStreamIdTransport()
    transport.fence_release.set()
    async with _session(transport) as session:
        with pytest.raises(DTMFPlaybackCancelled) as excinfo:
            await asyncio.wait_for(session.play_dtmf_tones("7"), _WAIT_S)
        assert transport.stream_reads >= 2
        # The next burst binds afresh and plays.
        playback = await asyncio.wait_for(session.play_dtmf_tones("8"), _WAIT_S)
        transport.ack()
        assert await playback.wait(_WAIT_S) == "played"
    assert "7" not in str(excinfo.value)
    assert transport.kinds().count("fence") == 1
    assert decode_dtmf(transport.tone_pcm()) == "8"


class _ContentionSignallingLock(asyncio.Lock):
    """Lock that signals when an acquirer has to wait for it."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.contended.set()
        return await super().acquire()


async def test_stream_replaced_while_waiting_for_the_send_lock_sends_no_fence() -> None:
    transport = _StreamGuardTransport()
    transport.fence_release.set()
    async with _session(transport) as session:
        router = session._audio_router
        lock = _ContentionSignallingLock()
        router._outbound_send_lock = lock
        await lock.acquire()  # speech still sending after a timed-out drain
        playing = asyncio.create_task(session.play_dtmf_tones("7"))
        await asyncio.wait_for(lock.contended.wait(), _WAIT_S)
        transport.stream_id = "MZ-replacement"
        lock.release()
        with pytest.raises(DTMFPlaybackCancelled):
            await asyncio.wait_for(playing, _WAIT_S)
    kinds = transport.kinds()
    assert "fence" not in kinds
    assert "begin" not in kinds
    assert "tone" not in kinds


async def test_inline_delivery_report_does_not_inflate_tone_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame reported delivered inside ``send_audio`` is not counted as queued."""
    # Keep a regression fast: a stall would otherwise wait 2 s per frame.
    monkeypatch.setattr("easycat.session._audio_router._TONE_PACING_STALL_S", 0.01)
    transport = _ReportingTransport()
    session = _session(transport)
    transport.bus = session.event_bus
    stalls = 0
    async with session:
        router = session._audio_router
        original_wait = router._tone_progress.wait

        async def counting_wait() -> bool:
            nonlocal stalls
            stalls += 1
            return await original_wait()

        router._tone_progress.wait = counting_wait  # type: ignore[method-assign]
        # Four keys at the default timing is well over the 500 ms pacing window.
        await session.play_dtmf_tones("1234")
        assert router._tone_ms_in_flight == 0.0
    assert stalls == 0
    assert decode_dtmf(transport.tone_pcm()) == "1234"


async def test_predicate_is_ignored_once_the_first_frame_is_sent() -> None:
    transport = _GuardTransport(pause_after_tone_frames=2)
    active = True
    async with _session(transport) as session:
        play = asyncio.create_task(session.play_dtmf_tones("12", should_continue=lambda: active))
        await asyncio.wait_for(transport.paused.wait(), _WAIT_S)
        active = False
        transport.resume.set()
        playback = await asyncio.wait_for(play, _WAIT_S)
        transport.ack()
        assert await playback.wait(_WAIT_S) == "played"
    assert decode_dtmf(transport.tone_pcm()) == "12"
    # Only the session stop's release-all abort; the burst's own guard was not aborted.
    assert all(token is None for token in transport.aborts)
    assert "end" in transport.kinds()
