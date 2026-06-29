"""Session lifecycle, teardown, and cancellation behavior tests."""

from __future__ import annotations

import asyncio

import pytest

from easycat._bounded_queue import BoundedAudioQueue
from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    Interruption,
    TTSAudio,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.records import JournalRecordKind
from easycat.session._session import Session
from easycat.session._types import TurnState
from easycat.turn_manager import TurnManagerState
from tests.session._session_core_helpers import (
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
    TrackingJournal,
    WarmupSTT,
    WarmupTransport,
    WarmupTTS,
    _full_config,
    _make_chunk,
)


@pytest.mark.asyncio
async def test_start_runs_provider_warmup_before_audio_ingress():
    calls: list[str] = []
    journal = InMemoryRingBuffer()
    session = Session(
        _full_config(
            stt=WarmupSTT(calls),
            tts=WarmupTTS(calls),
            transport=WarmupTransport(calls),
            journal=journal,
        )
    )

    await session.start()
    await asyncio.sleep(0)
    await session.stop(force=True)

    assert "transport.receive" in calls
    # Provider/model warmup runs BEFORE the transport is connected so the slow
    # model-load / handshake cost does not execute while a live capture device
    # is already buffering inbound frames into an undrained queue.  The
    # transport's own warmup runs AFTER connect (it may prime connect-created
    # resources), and both precede the receive (ingress) loop.
    assert calls.index("stt.warmup") < calls.index("transport.connect")
    assert calls.index("tts.warmup") < calls.index("transport.connect")
    assert calls.index("transport.connect") < calls.index("transport.warmup")
    assert calls.index("transport.warmup") < calls.index("transport.receive")
    # Two-phase warmup emits one ``warmup_completed`` per phase: providers
    # before connect, the transport after.  ``AgentRunner`` is warmupable (it
    # delegates ``warmup()`` to the wrapped agent), so the default agent
    # wrapper is recorded as warmed even though ``FakeAgent`` itself has no
    # ``warmup`` hook to forward to.
    records = [record for record in journal.read() if record.name == "warmup_completed"]
    warmed = [c["component"] for record in records for c in record.data["components"]]
    assert warmed == ["stt", "tts", "agent", "transport"]


@pytest.mark.asyncio
async def test_session_default_construction():
    session = Session(_full_config())
    assert session.turn_state == TurnState.IDLE
    assert not session.is_running
    assert session.cancel_token is None


@pytest.mark.asyncio
async def test_session_start_and_stop():
    transport = FakeTransport()
    config = _full_config(transport=transport)
    session = Session(config)

    await session.start()
    assert session.is_running
    assert transport.connected

    await session.stop()
    assert not session.is_running
    assert transport.disconnected
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_session_shutdown():
    transport = FakeTransport()
    config = _full_config(transport=transport)
    session = Session(config)

    await session.start()
    await session.shutdown()

    assert not session.is_running
    assert transport.disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["stop", "shutdown"])
async def test_session_teardown_finalizes_and_closes_journal(method_name: str):
    transport = FakeTransport()
    journal = TrackingJournal()
    session = Session(_full_config(transport=transport, journal=journal, session_id="sess"))

    await session.start()
    await getattr(session, method_name)()

    assert journal.finalize_calls == 1
    assert journal.close_calls == 1


def test_session_close_compatibility_alias_finalizes_journal_only():
    journal = TrackingJournal()
    session = Session(_full_config(journal=journal, session_id="sess"))

    session.close()
    session.close()

    assert journal.finalize_calls == 1
    assert journal.close_calls == 0


def test_session_destroy_compatibility_alias_closes_debug_backends():
    journal = TrackingJournal()
    session = Session(_full_config(journal=journal, session_id="sess"))

    session.destroy()
    session.destroy()

    assert journal.finalize_calls == 1
    assert journal.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["close", "destroy"])
async def test_session_low_level_compatibility_aliases_reject_running_session(
    method_name: str,
):
    transport = FakeTransport()
    session = Session(_full_config(transport=transport))

    await session.start()
    try:
        with pytest.raises(RuntimeError, match="await session.stop"):
            getattr(session, method_name)()
        assert transport.connected
    finally:
        await session.stop(force=True)


def test_session_close_compatibility_alias_retries_after_finalize_failure():
    class FailingOnceJournal(TrackingJournal):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_finalize = True

        def finalize(self) -> None:
            self.finalize_calls += 1
            if self.fail_next_finalize:
                self.fail_next_finalize = False
                raise RuntimeError("finalize failed")

    journal = FailingOnceJournal()
    session = Session(_full_config(journal=journal, session_id="sess"))

    with pytest.raises(RuntimeError, match="finalize failed"):
        session.close()

    session.close()

    assert journal.finalize_calls == 2
    assert journal.close_calls == 0


def test_session_destroy_compatibility_alias_retries_after_close_failure():
    class FailingOnceJournal(TrackingJournal):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_close = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_next_close:
                self.fail_next_close = False
                raise RuntimeError("close failed")

    journal = FailingOnceJournal()
    session = Session(_full_config(journal=journal, session_id="sess"))

    with pytest.raises(RuntimeError, match="close failed"):
        session.destroy()

    session.destroy()

    assert journal.finalize_calls == 1
    assert journal.close_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["stop", "shutdown"])
async def test_session_teardown_closes_audio_providers(method_name: str):
    calls: list[str] = []

    class CloseSTT(FakeSTT):
        def close(self) -> None:
            calls.append("stt.close")

    class AsyncCloseTTS(FakeTTS):
        async def aclose(self) -> None:
            calls.append("tts.aclose")

    class AsyncCloseVAD(FakeVAD):
        async def close(self) -> None:
            calls.append("vad.close")

    class CloseNoiseReducer:
        async def process(self, chunk: AudioChunk) -> AudioChunk:
            return chunk

        def close(self) -> None:
            calls.append("noise.close")

    class AsyncCloseEchoCanceller:
        async def process(self, chunk: AudioChunk) -> AudioChunk:
            return chunk

        def feed_reference(self, chunk: AudioChunk) -> None:
            pass

        async def aclose(self) -> None:
            calls.append("echo.aclose")

    session = Session(
        _full_config(
            stt=CloseSTT(),
            tts=AsyncCloseTTS(),
            vad=AsyncCloseVAD(),
            noise_reducer=CloseNoiseReducer(),
            echo_canceller=AsyncCloseEchoCanceller(),
        )
    )

    await getattr(session, method_name)()

    assert calls == [
        "stt.close",
        "tts.aclose",
        "vad.close",
        "noise.close",
        "echo.aclose",
    ]


@pytest.mark.asyncio
async def test_shutdown_ends_active_stt_stream_without_close_hook():
    class EndOnlySTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__()
            self.end_calls = 0

        async def end_stream(self) -> None:
            self.end_calls += 1
            await self._queue.put(None)

    stt = EndOnlySTT()
    session = Session(_full_config(stt=stt))
    session._stt_active = True

    await session.shutdown()

    assert stt.end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["stop", "shutdown"])
async def test_external_outbound_queue_survives_session_teardown(method_name: str):
    transport = FakeTransport()
    queue = BoundedAudioQueue()
    session = Session(_full_config(transport=transport, outbound_queue=queue))

    await session.start()
    await getattr(session, method_name)()

    assert await queue.put(_make_chunk())
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_replay_gated_audio_stays_bot_speaking_until_outbound_drain():
    transport = FakeTransport()
    session = Session(_full_config(transport=transport))
    events = [
        TTSAudio(chunk=_make_chunk()),
        TTSAudio(chunk=_make_chunk()),
    ]

    await session.replay_gated_audio(events)

    assert session._turn_manager.state == TurnManagerState.BOT_SPEAKING
    assert session._outbound_queue.qsize() == 2

    await session._audio_router._drain_outbound_audio()

    assert session._turn_manager.state == TurnManagerState.IDLE
    assert len(transport.sent) == 2


@pytest.mark.asyncio
async def test_session_start_idempotent():
    session = Session(_full_config())
    await session.start()
    await session.start()
    assert session.is_running
    await session.stop()


@pytest.mark.asyncio
async def test_session_concurrent_start_calls_share_single_startup():
    class BlockingConnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0
            self.connect_entered = asyncio.Event()
            self.allow_connect = asyncio.Event()
            self.receive_started = asyncio.Event()
            self.allow_receive_exit = asyncio.Event()

        async def connect(self) -> None:
            self.connect_calls += 1
            self.connect_entered.set()
            await self.allow_connect.wait()
            await super().connect()

        async def receive_audio(self):
            self.receive_started.set()
            await self.allow_receive_exit.wait()
            if False:
                yield _make_chunk()

    transport = BlockingConnectTransport()
    session = Session(_full_config(transport=transport))

    first_start = asyncio.create_task(session.start())
    second_start = asyncio.create_task(session.start())

    await transport.connect_entered.wait()
    await asyncio.sleep(0)
    assert transport.connect_calls == 1

    transport.allow_connect.set()
    await asyncio.gather(first_start, second_start)

    assert session.is_running
    assert transport.connect_calls == 1
    assert session._audio_router.pipeline_task is not None
    assert session._audio_router.outbound_task is not None

    transport.allow_receive_exit.set()
    await session.stop(force=True)


@pytest.mark.asyncio
async def test_session_start_rolls_back_after_connect_failure():
    class FlakyTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_calls == 1:
                raise RuntimeError("boom")
            await super().connect()

    transport = FlakyTransport()
    session = Session(_full_config(transport=transport))

    with pytest.raises(RuntimeError, match="boom"):
        await session.start()

    assert not session.is_running
    assert session._audio_router.pipeline_task is None
    assert session._audio_router.outbound_task is None

    await session.start()

    assert session.is_running
    assert transport.connect_calls == 2
    assert transport.connected

    await session.stop()


@pytest.mark.asyncio
async def test_session_stop_idempotent():
    session = Session(_full_config())
    await session.stop()
    assert not session.is_running


@pytest.mark.asyncio
async def test_cancel_turn_resets_state():
    session = Session(_full_config())
    session._turn_state = TurnState.LISTENING
    turn = TurnContext("test-turn", CancelToken())
    session._turn = turn
    await session.cancel_turn()
    assert session.turn_state == TurnState.IDLE
    assert turn.cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_turn_barge_in_emits_interruption():
    session = Session(_full_config())
    session._turn_state = TurnState.BOT_SPEAKING
    session._turn = TurnContext("test-turn", CancelToken())

    received: list = []
    session.event_bus.subscribe(Interruption, lambda e: received.append(e))

    await session.cancel_turn(barge_in=True)
    assert len(received) == 1
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_cancel_turn_barge_in_propagates_signal_through_all_stages():
    """WS3 T3.8: a barge-in must dispatch an InterruptSignal through
    every stage, producing one ControlSignalRecord per stage in the
    journal so replay can see who observed the signal and when.
    """
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(_full_config(journal=journal))
    session._turn_state = TurnState.BOT_SPEAKING
    session._turn = TurnContext("test-turn-signal", CancelToken())

    await session.cancel_turn(barge_in=True)

    signal_records = [r for r in journal.read() if r.kind == JournalRecordKind.CONTROL]
    # One per stage plus the trailing cause record.
    stage_records = [r for r in signal_records if r.name == "control_signal"]
    cause_records = [r for r in signal_records if r.name == "control_signal_cause"]
    observed = {r.data["observed_stage"] for r in stage_records}
    # Telephony doesn't have its own stage; the session only fans the
    # signal through helpers when at least one is registered.
    assert observed == {
        "transport",
        "tts",
        "agent",
        "turn",
        "stt",
        "vad",
        "audio",
    }
    # Every stage record carries the same signal_id so a replay UI can
    # group the upstream walk into one logical event.
    signal_ids = {r.data["signal_id"] for r in stage_records}
    assert len(signal_ids) == 1
    # The cause record links the signal back to "barge_in".
    assert len(cause_records) == 1
    assert cause_records[0].data["cause"] == "barge_in"
    assert cause_records[0].data["signal_id"] == next(iter(signal_ids))


@pytest.mark.asyncio
async def test_cancel_tts_playback_resets_state():
    session = Session(_full_config())
    session._turn_state = TurnState.BOT_SPEAKING
    turn = TurnContext("test-turn", CancelToken())
    session._turn = turn
    await session.cancel_tts_playback()
    assert session.turn_state == TurnState.IDLE
    # cancel_tts_playback should NOT cancel the shared token —
    # only TTS is stopped, agent streams can continue.
    assert not turn.cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_reset_state():
    session = Session(_full_config())
    session._turn_state = TurnState.PROCESSING
    turn = TurnContext("test-turn", CancelToken())
    session._turn = turn
    await session.reset_state()
    assert session.turn_state == TurnState.IDLE
    assert turn.cancel_token.is_cancelled


class BlockingClearTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.clear_started = asyncio.Event()
        self.clear_continue = asyncio.Event()
        self.queue_size_at_clear: int | None = None
        self.observed_queue: BoundedAudioQueue | None = None

    async def clear_audio(self) -> None:
        assert self.observed_queue is not None
        self.queue_size_at_clear = self.observed_queue.qsize()
        self.clear_started.set()
        await self.clear_continue.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["cancel_turn", "cancel_tts_playback", "reset_state"])
async def test_cancellation_flushes_outbound_audio_before_transport_clear(method_name: str):
    transport = BlockingClearTransport()
    session = Session(_full_config(transport=transport))
    transport.observed_queue = session._outbound_queue
    await session._outbound_queue.put(_make_chunk())

    task = asyncio.create_task(getattr(session, method_name)())
    await asyncio.wait_for(transport.clear_started.wait(), timeout=1)

    assert transport.queue_size_at_clear == 0
    assert session._outbound_queue.empty()

    transport.clear_continue.set()
    await task


@pytest.mark.asyncio
async def test_cancel_tts_playback_does_not_let_drain_send_queued_audio_after_clear():
    transport = BlockingClearTransport()
    session = Session(_full_config(transport=transport))
    transport.observed_queue = session._outbound_queue
    await session._outbound_queue.put(_make_chunk())

    cancel_task = asyncio.create_task(session.cancel_tts_playback())
    await asyncio.wait_for(transport.clear_started.wait(), timeout=1)

    await session._audio_router._drain_outbound_audio()

    assert transport.queue_size_at_clear == 0
    assert transport.sent == []

    transport.clear_continue.set()
    await cancel_task
