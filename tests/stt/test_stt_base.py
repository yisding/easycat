"""Tests for the STT base class and test harness."""

from __future__ import annotations

import asyncio

import pytest

from easycat._concurrency import RuntimeSupervisor
from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType
from easycat.runtime.scope import RuntimeScope, RuntimeScopeState
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
async def test_cancelled_start_rolls_back_running_state_and_allows_retry() -> None:
    class BlockingStartupSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.start_calls = 0
            self.start_entered = asyncio.Event()

        async def _on_start(self) -> None:
            self.start_calls += 1
            if self.start_calls == 1:
                self.start_entered.set()
                await asyncio.Event().wait()

    stt = BlockingStartupSTT()
    first_start = asyncio.create_task(stt.start_stream())
    await stt.start_entered.wait()
    first_start.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first_start

    assert stt._running is False
    await stt.start_stream()
    assert stt._running is True
    assert stt.start_calls == 2
    await stt.end_stream()


@pytest.mark.asyncio
async def test_cancelled_partial_start_closes_resources_before_retry() -> None:
    class ResourceStartupSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.resources: list[dict[str, bool]] = []
            self.start_entered = asyncio.Event()

        async def _on_start(self) -> None:
            resource = {"closed": False}
            self.resources.append(resource)
            if len(self.resources) == 1:
                self.start_entered.set()
                await asyncio.Event().wait()

        async def _on_end(self) -> None:
            if self.resources:
                self.resources[-1]["closed"] = True

    stt = ResourceStartupSTT()
    first_start = asyncio.create_task(stt.start_stream())
    await stt.start_entered.wait()
    first_start.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first_start

    assert stt.resources == [{"closed": True}]
    await stt.start_stream()
    await stt.end_stream()
    assert stt.resources == [{"closed": True}, {"closed": True}]


@pytest.mark.asyncio
async def test_failed_start_cleanup_preserves_caller_cancellation_after_cleanup() -> None:
    primary = RuntimeError("startup failed")

    class SlowCleanupSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_entered = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.cleanup_finished = False

        async def _on_start(self) -> None:
            raise primary

        async def _on_start_failed(self) -> None:
            self.cleanup_entered.set()
            await self.release_cleanup.wait()
            self.cleanup_finished = True

    stt = SlowCleanupSTT()
    start = asyncio.create_task(stt.start_stream())
    await stt.cleanup_entered.wait()
    start.cancel()
    await asyncio.sleep(0)
    start.cancel()
    stt.release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await start

    assert exc_info.value.__cause__ is primary
    assert stt.cleanup_finished is True
    assert stt._running is False


@pytest.mark.asyncio
async def test_failed_start_cleanup_is_retained_and_retried_before_reuse() -> None:
    startup_error = RuntimeError("startup failed")
    cleanup_errors = [RuntimeError("cleanup one"), RuntimeError("cleanup two")]

    class RetryCleanupSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.start_calls = 0
            self.cleanup_calls = 0

        async def _on_start(self) -> None:
            self.start_calls += 1
            if self.start_calls == 1:
                raise startup_error

        async def _on_start_failed(self) -> None:
            self.cleanup_calls += 1
            if cleanup_errors:
                raise cleanup_errors.pop(0)

    stt = RetryCleanupSTT()

    with pytest.raises(RuntimeError, match="startup failed") as first:
        await stt.start_stream()

    assert first.value is startup_error
    assert isinstance(first.value.__cause__, RuntimeError)
    assert str(first.value.__cause__) == "cleanup one"
    assert stt._failed_start_cleanup_pending is True

    with pytest.raises(RuntimeError, match="cleanup is incomplete") as second:
        await stt.start_stream()

    assert str(second.value.__cause__) == "cleanup two"
    assert stt.start_calls == 1
    assert stt._failed_start_cleanup_pending is True

    await stt.start_stream()
    assert stt.start_calls == 2
    assert stt.cleanup_calls == 3
    assert stt._failed_start_cleanup_pending is False
    await stt.end_stream()


@pytest.mark.asyncio
async def test_failed_end_cleanup_finishes_under_repeated_cancellation() -> None:
    class SlowEndCleanupSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_entered = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.cleanup_finished = False

        async def _on_end(self) -> None:
            raise RuntimeError("provider end failed")

        async def _on_end_cleanup(self) -> None:
            self.cleanup_entered.set()
            await self.release_cleanup.wait()
            self.cleanup_finished = True

    stt = SlowEndCleanupSTT()
    await stt.start_stream()
    with pytest.raises(RuntimeError, match="provider end failed"):
        await stt.end_stream()

    closing = asyncio.create_task(stt.close())
    await stt.cleanup_entered.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    stt.release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert stt.cleanup_finished is True
    assert stt._failed_end_cleanup_pending is False
    assert stt._failed_end_cleanup_error is None


@pytest.mark.asyncio
async def test_end_stream_ignores_preexisting_cancel_count_for_owned_send_cancel() -> None:
    class BlockingSendSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_entered = asyncio.Event()
            self.end_calls = 0

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_entered.set()
            await asyncio.Future()

        async def _on_end(self) -> None:
            self.end_calls += 1

    stt = BlockingSendSTT()
    await stt.start_stream()
    sending = asyncio.create_task(
        stt.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
    )
    await stt.send_entered.wait()

    async def end_after_caught_cancel() -> int:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        await stt.end_stream()
        return caller.cancelling()

    cancellation_requests = await asyncio.create_task(end_after_caught_cancel())
    await sending

    assert cancellation_requests == 1
    assert stt.end_calls == 1
    assert stt._active_audio_send_task is None
    assert stt._failed_end_cleanup_pending is False
    assert stt._failed_end_cleanup_error is None


@pytest.mark.asyncio
async def test_failed_end_close_waits_for_retained_cancellation_resistant_send() -> None:
    class RetainedSendSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_entered = asyncio.Event()
            self.first_cancel = asyncio.Event()
            self.second_cancel = asyncio.Event()
            self.release_send = asyncio.Event()
            self.cancel_count = 0
            self.cleanup_calls = 0

        async def _on_audio(self, chunk: AudioChunk) -> None:
            self.send_entered.set()
            while not self.release_send.is_set():
                try:
                    await self.release_send.wait()
                except asyncio.CancelledError:
                    self.cancel_count += 1
                    self.first_cancel.set()
                    if self.cancel_count >= 2:
                        self.second_cancel.set()

        async def _on_end_cleanup(self) -> None:
            self.cleanup_calls += 1

    stt = RetainedSendSTT()
    await stt.start_stream()
    sending = asyncio.create_task(
        stt.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
    )
    await stt.send_entered.wait()

    async def end_after_caught_cancel() -> None:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        await stt.end_stream()

    ending = asyncio.create_task(end_after_caught_cancel())
    await stt.first_cancel.wait()
    ending.cancel()
    ending.cancel()
    assert ending.cancelling() == 3
    with pytest.raises(asyncio.CancelledError):
        await ending

    retained_send = stt._active_audio_send_task
    assert retained_send is not None
    assert not retained_send.done()
    assert stt._failed_end_cleanup_pending is True

    closing = asyncio.create_task(stt.close())
    await stt.second_cancel.wait()
    await asyncio.sleep(0)
    assert not closing.done()

    stt.release_send.set()
    await asyncio.gather(closing, sending)

    assert retained_send.done()
    assert stt._active_audio_send_task is None
    assert stt.cleanup_calls == 1
    assert stt._failed_end_cleanup_pending is False


@pytest.mark.asyncio
@pytest.mark.parametrize("new_caller_cancellation", [False, True])
async def test_reap_retained_send_distinguishes_new_cancel_from_preexisting_count(
    new_caller_cancellation: bool,
) -> None:
    stt = STTBase(allow_end_during_audio_send=True)
    send_entered = asyncio.Event()
    send_cancelled = asyncio.Event()
    release_send = asyncio.Event()

    async def retained_send() -> None:
        send_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if not new_caller_cancellation:
                raise
            send_cancelled.set()
            await release_send.wait()

    owned_send = asyncio.create_task(retained_send())
    stt._active_audio_send_task = owned_send
    await send_entered.wait()

    async def reap_after_caught_cancel() -> int:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        await stt.end_stream()
        return caller.cancelling()

    reaping = asyncio.create_task(reap_after_caught_cancel())
    if not new_caller_cancellation:
        assert await reaping == 1
        await asyncio.gather(owned_send, return_exceptions=True)
        assert stt._active_audio_send_task is None
        return

    await send_cancelled.wait()
    reaping.cancel()
    assert reaping.cancelling() == 2
    with pytest.raises(asyncio.CancelledError):
        await reaping

    assert stt._active_audio_send_task is owned_send
    release_send.set()
    await owned_send
    await stt.end_stream()
    assert stt._active_audio_send_task is None


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
async def test_base_close_ends_an_active_stream():
    stt = EchoSTT()
    await stt.start_stream()
    await stt.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))

    await stt.close()

    assert stt._running is False
    assert [event.text async for event in stt.events()] == ["test transcript"]


@pytest.mark.asyncio
async def test_send_audio_accepts_lifecycle_cutoff_with_preexisting_cancel_count() -> None:
    class BlockingSendSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_started = asyncio.Event()
            self.end_calls = 0

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_started.set()
            await asyncio.Future()

        async def _on_end(self) -> None:
            self.end_calls += 1

    stt = BlockingSendSTT()
    await stt.start_stream()
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_after_caught_cancel() -> int:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        await stt.send_audio(chunk)
        return caller.cancelling()

    sending = asyncio.create_task(send_after_caught_cancel())
    await stt.send_started.wait()
    await stt.end_stream()

    assert await sending == 1
    assert stt.end_calls == 1
    assert stt._active_audio_send_task is None


@pytest.mark.asyncio
async def test_send_audio_propagates_new_caller_cancel_and_reaps_owned_send() -> None:
    class BlockingSendSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_started = asyncio.Event()
            self.send_cancelled = asyncio.Event()

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_started.set()
            try:
                await asyncio.Future()
            finally:
                self.send_cancelled.set()

    stt = BlockingSendSTT()
    await stt.start_stream()
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_after_caught_cancel() -> None:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert caller.cancelling() == 1
        await stt.send_audio(chunk)

    sending = asyncio.create_task(send_after_caught_cancel())
    await stt.send_started.wait()
    sending.cancel()
    sending.cancel()
    assert sending.cancelling() == 3

    with pytest.raises(asyncio.CancelledError):
        await sending

    assert stt.send_cancelled.is_set()
    assert stt._active_audio_send_task is None
    await stt.end_stream()


@pytest.mark.asyncio
async def test_interruptible_audio_send_attaches_to_the_session_runtime_tree() -> None:
    class BlockingSendSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_started = asyncio.Event()

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_started.set()
            await asyncio.Future()

    root = RuntimeScope.create_root(
        name="session",
        root_id="session:test",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    stt = BlockingSendSTT()
    stt.set_runtime_scope(root, name="stt-provider-runtime")
    await stt.start_stream()

    sending = asyncio.create_task(
        stt.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
    )
    await stt.send_started.wait()

    assert stt._active_audio_send_task in root.tasks("stt_audio_send")
    assert "stt-runtime" in root.cohorts(force=False)

    await stt.end_stream()
    await sending
    assert root.tasks("stt_audio_send") == ()
    await root.close()


@pytest.mark.asyncio
async def test_standalone_close_releases_an_idle_stt_runtime_scope() -> None:
    stt = STTBase(allow_end_during_audio_send=True)
    await stt.start_stream()
    await stt.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
    scope = stt._runtime_scope
    assert scope is not None

    await stt.close()

    assert scope.state is RuntimeScopeState.CLOSED
    assert stt._runtime_scope is None


@pytest.mark.asyncio
async def test_websocket_end_stream_preempts_stalled_ordered_send() -> None:
    class PausingWebSocketSTT(WebSocketSTTBase):
        def __init__(self) -> None:
            super().__init__(provider_name="test", provider_error_name="test")
            self.send_started = asyncio.Event()
            self.send_cancelled = asyncio.Event()
            self.release_cancel_cleanup = asyncio.Event()
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
                await self.release_cancel_cleanup.wait()
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

    end = asyncio.create_task(stt.end_stream())
    await asyncio.wait_for(stt.send_cancelled.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not stt.end_called.is_set()
    stt.release_cancel_cleanup.set()
    await asyncio.wait_for(end, timeout=1)

    assert stt.end_called.is_set()
    assert stt.send_cancelled.is_set()

    # A rapid successor stream must not make the lifecycle cancellation look
    # provider-originated, nor admit audio queued for the old stream.
    await stt.start_stream()
    await asyncio.wait_for(first_send, timeout=1)
    with pytest.raises(RuntimeError, match="Stream not started"):
        await asyncio.wait_for(second_send, timeout=1)
    await stt.end_stream()


@pytest.mark.asyncio
async def test_timed_out_end_does_not_finalize_or_restart_over_live_send() -> None:
    class CancellationResistantSTT(STTBase):
        def __init__(self) -> None:
            super().__init__(allow_end_during_audio_send=True)
            self.send_started = asyncio.Event()
            self.send_cancelled = asyncio.Event()
            self.release_send = asyncio.Event()
            self.end_called = asyncio.Event()

        async def _on_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.send_cancelled.set()
                await self.release_send.wait()

        async def _on_end(self) -> None:
            self.end_called.set()

    stt = CancellationResistantSTT()
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    await stt.start_stream()
    send = asyncio.create_task(stt.send_audio(chunk))
    await stt.send_started.wait()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stt.end_stream(), timeout=0.02)

    assert stt.send_cancelled.is_set()
    assert not stt.end_called.is_set()
    with pytest.raises(RuntimeError, match="still shutting down"):
        await stt.start_stream()

    stt.release_send.set()
    await asyncio.wait_for(send, timeout=1)
    await stt.start_stream()
    await stt.end_stream()


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
