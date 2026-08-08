"""Session lifecycle, teardown, and cancellation behavior tests."""

from __future__ import annotations

import asyncio
import sys

import pytest

from easycat._bounded_queue import BoundedAudioQueue
from easycat._provider_helpers import ProviderErrorEmitter
from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentFinal,
    Error,
    ErrorStage,
    EventBus,
    Interruption,
    TTSAudio,
    TurnStarted,
    VADStartSpeaking,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeScope
from easycat.session._session import Session
from easycat.session._types import TurnState
from easycat.stt.base import STTBase
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
    assert warmed == ["stt", "tts", "audio_resampling", "agent", "transport"]


@pytest.mark.asyncio
async def test_session_attaches_runtime_bindables_and_provider_emitters() -> None:
    class ScopedFakeSTT(ProviderErrorEmitter, STTBase):
        _error_stage = ErrorStage.STT
        _provider_error_name = "fake-stt"

        def __init__(self) -> None:
            STTBase.__init__(self)
            self._init_emit_tasks()

    class ScopedFakeTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_scope: RuntimeScope | None = None

        def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
            self.runtime_scope = parent.create_child(name)

    class ScopedFakeTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_scope: RuntimeScope | None = None

        def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
            self.runtime_scope = parent.create_child(name)

    stt = ScopedFakeSTT()
    tts = ScopedFakeTTS()
    transport = ScopedFakeTransport()
    session = Session(_full_config(stt=stt, tts=tts, transport=transport))

    assert stt._emit_scope is not None
    assert stt._emit_scope.parent is session._runtime_scope
    assert stt._emit_scope.name == "stt-provider-events"
    assert stt._runtime_scope is not None
    assert stt._runtime_scope.parent is session._runtime_scope
    assert stt._runtime_scope.name == "stt-provider-runtime"
    assert tts.runtime_scope is not None
    assert tts.runtime_scope.parent is session._runtime_scope
    assert tts.runtime_scope.name == "tts-provider-runtime"
    assert transport.runtime_scope is not None
    assert transport.runtime_scope.parent is session._runtime_scope
    assert transport.runtime_scope.name == "transport-runtime"

    await session.stop(force=True)


@pytest.mark.asyncio
async def test_force_stop_finishes_provider_error_emission_before_close() -> None:
    class ScopedFakeSTT(ProviderErrorEmitter, FakeSTT):
        _error_stage = ErrorStage.STT
        _provider_error_name = "fake-stt"

        def __init__(self) -> None:
            FakeSTT.__init__(self)
            self._init_emit_tasks()
            self._event_bus: EventBus | None = None

        def _resolve_event_bus(self) -> EventBus | None:
            return self._event_bus

        async def close(self) -> None:
            await self._drain_emit_tasks()

    stt = ScopedFakeSTT()
    bus = EventBus()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    received: list[Error] = []

    async def handle_error(event: Error) -> None:
        received.append(event)
        handler_started.set()
        await release_handler.wait()

    bus.subscribe(Error, handle_error)
    session = Session(_full_config(stt=stt, event_bus=bus))
    stt._emit_provider_error(RuntimeError("provider failed"))
    await handler_started.wait()

    stopping = asyncio.create_task(session.stop(force=True))
    await asyncio.sleep(0)

    assert stt._emit_tasks
    assert all(not task.cancelled() for task in stt._emit_tasks)

    release_handler.set()
    await stopping

    assert len(received) == 1
    assert str(received[0].exception) == "provider failed"
    assert not stt._emit_tasks


@pytest.mark.asyncio
async def test_start_preserves_warmup_error_and_blocks_restart_after_failed_rollback() -> None:
    class FailingRollbackTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.warmup_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            self.connected = True

        async def warmup(self) -> None:
            self.warmup_calls += 1
            if self.warmup_calls == 1:
                raise OSError("transport warmup failed")

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.disconnect_calls == 1:
                raise RuntimeError("rollback disconnect failed")
            self.connected = False
            self.disconnected = True

    transport = FailingRollbackTransport()
    bus = EventBus(handler_error_policy="raise")
    observed: list[TurnStarted] = []
    external = bus.subscribe(TurnStarted, observed.append)
    session = Session(_full_config(transport=transport, event_bus=bus))

    with pytest.raises(OSError, match="transport warmup failed") as exc_info:
        await session.start()

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "rollback disconnect failed"
    assert transport.connected is True
    assert transport.connect_calls == 1
    assert session._stopping is True
    assert isinstance(session._lifecycle_cleanup_error, RuntimeError)
    assert session._event_subscriptions == []
    assert external.active is True
    assert bus.subscribers(TurnStarted) == [observed.append]
    assert getattr(bus, "_easycat_was_shared_by_sessions", False)

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await session.start()
    assert transport.connect_calls == 1

    await session.stop(force=True)

    assert session._closed is True
    assert session._stopping is False
    assert session._lifecycle_cleanup_error is None
    assert transport.disconnect_calls == 2
    assert transport.connected is False


@pytest.mark.asyncio
async def test_stop_unregisters_armed_emergency_export():
    """A clean ``stop()`` drops the session's emergency exporter promptly.

    Without the unregister call the exporter closure (which strongly
    references the Session) lingers in the process-wide registry until the
    shared excepthook/atexit hook runs, pinning every stopped session in
    memory for the process lifetime.
    """
    from easycat.config import _factory

    saved_excepthook = sys.excepthook
    saved_registry = dict(_factory._EXPORT_REGISTRY)
    saved_installed = _factory._EXPORT_INSTALLED
    saved_previous = _factory._EXPORT_PREVIOUS_EXCEPTHOOK
    saved_hook = _factory._EXPORT_EXCEPTHOOK

    _factory._EXPORT_REGISTRY.clear()
    _factory._EXPORT_INSTALLED = False
    _factory._EXPORT_PREVIOUS_EXCEPTHOOK = None
    _factory._EXPORT_EXCEPTHOOK = None
    try:
        session = Session(_full_config(transport=FakeTransport()))
        _factory.install_emergency_export(session)
        assert id(session) in _factory._EXPORT_REGISTRY

        await session.start()
        await session.stop()

        # The exporter (and the Session it captures) is dropped on clean stop.
        assert id(session) not in _factory._EXPORT_REGISTRY
        # Draining the last entry uninstalls the shared hook.
        assert _factory._EXPORT_REGISTRY == {}
        assert _factory._EXPORT_INSTALLED is False
    finally:
        _factory._EXPORT_REGISTRY.clear()
        _factory._EXPORT_REGISTRY.update(saved_registry)
        _factory._EXPORT_INSTALLED = saved_installed
        _factory._EXPORT_PREVIOUS_EXCEPTHOOK = saved_previous
        _factory._EXPORT_EXCEPTHOOK = saved_hook
        sys.excepthook = saved_excepthook


@pytest.mark.asyncio
async def test_force_stop_preempts_hung_graceful_stop() -> None:
    class HungOnceDisconnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.disconnect_started = asyncio.Event()
            self.disconnect_cancelled = asyncio.Event()
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.disconnect_calls != 1:
                return
            self.disconnect_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.disconnect_cancelled.set()
                raise

    transport = HungOnceDisconnectTransport()
    session = Session(_full_config(transport=transport))
    await session.start()

    graceful = asyncio.create_task(session.stop())
    await asyncio.wait_for(transport.disconnect_started.wait(), timeout=1)

    await asyncio.wait_for(session.stop(force=True), timeout=1)

    with pytest.raises(asyncio.CancelledError):
        await graceful
    assert transport.disconnect_cancelled.is_set()
    assert transport.disconnect_calls == 2
    assert session._closed is True
    assert session._stopping is False
    assert session._stop_task is None


@pytest.mark.asyncio
async def test_stop_join_ignores_preexisting_cancellation_count() -> None:
    session = Session(_full_config())
    active_stop = asyncio.create_task(asyncio.Event().wait())
    session._stop_task = active_stop
    session._stop_force = False
    joining = asyncio.Event()

    async def join_after_caught_cancellation() -> int:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        joining.set()
        await session.stop()
        return current.cancelling()

    caller = asyncio.create_task(join_after_caught_cancellation())
    await joining.wait()
    await asyncio.sleep(0)
    active_stop.cancel()

    assert await caller == 1
    assert active_stop.cancelled()
    assert session._closed is True
    assert session._stop_task is None


@pytest.mark.asyncio
async def test_stop_join_propagates_cancellation_pending_before_entry() -> None:
    session = Session(_full_config())
    active_stop = asyncio.create_task(asyncio.Event().wait())
    session._stop_task = active_stop
    session._stop_force = False

    async def join_with_pending_cancellation() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await session.stop()

    caller = asyncio.create_task(join_with_pending_cancellation())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert caller.done()
    with pytest.raises(asyncio.CancelledError):
        await caller
    active_stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active_stop


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["voice", "text", "preemptive"])
async def test_force_stop_from_runtime_owned_turn_task_closes_and_cancels_siblings(
    owner_kind: str,
) -> None:
    session = Session(_full_config())
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    stop_returned = asyncio.Event()

    async def sibling_work() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    sibling_task = session._runtime_scope.create_task("sibling_work", sibling_work())
    await sibling_started.wait()

    async def stop_from_agent_final(_event: AgentFinal) -> None:
        await session.stop(force=True)
        stop_returned.set()

    session.event_bus.subscribe(AgentFinal, stop_from_agent_final)

    async def active_turn_work() -> None:
        current = asyncio.current_task()
        assert current is not None
        if owner_kind == "voice":
            session._tts_scheduler.active_turn_task = current
        elif owner_kind == "text":
            session._turn_runner._active_text_turn = current
        else:
            session._turn_runner._preemptive_task = current
        await session.event_bus.emit(AgentFinal(text="done"))

    owner_task = session._runtime_scope.create_task("on_turn_ended", active_turn_work())
    await asyncio.wait_for(owner_task, timeout=1)

    assert stop_returned.is_set()
    assert session._closed
    assert not session._stopping
    assert not owner_task.cancelled()
    assert sibling_task.cancelled()
    assert sibling_cancelled.is_set()
    assert session._runtime_scope.empty


@pytest.mark.asyncio
async def test_session_default_construction():
    session = Session(_full_config())
    assert session.turn_state == TurnState.IDLE
    assert not session.is_running
    assert session.cancel_token is None
    for removed in ("shutdown", "close", "destroy"):
        assert not hasattr(session, removed)


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
async def test_failed_stop_blocks_restart_until_cleanup_retry() -> None:
    class FailingOnceDisconnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.disconnect_calls == 1:
                raise RuntimeError("disconnect failed")
            self.connected = False
            self.disconnected = True

    transport = FailingOnceDisconnectTransport()
    session = Session(_full_config(transport=transport))
    await session.start()

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await session.stop(force=True)

    assert session._closed is False
    assert session._stopping is True
    assert isinstance(session._lifecycle_cleanup_error, RuntimeError)
    assert transport.connected is True

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await session.start()
    assert transport.connect_calls == 1

    await session.stop(force=True)

    assert session._closed is True
    assert session._stopping is False
    assert session._lifecycle_cleanup_error is None
    assert transport.disconnect_calls == 2
    assert transport.connected is False


@pytest.mark.asyncio
async def test_failed_provider_and_agent_close_keep_stop_retryable() -> None:
    class FailingOnceTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("tts close failed")

    class FailingOnceAgent:
        def __init__(self) -> None:
            self.close_calls = 0

        async def run(self, text: str) -> str:
            return text

        async def aclose(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("agent close failed")

    tts = FailingOnceTTS()
    agent = FailingOnceAgent()
    session = Session(_full_config(tts=tts, agent=agent))
    await session.start()

    with pytest.raises(RuntimeError, match="agent close failed") as exc_info:
        await session.stop(force=True)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "tts close failed"
    assert session._closed is False
    assert session._stopping is True
    assert agent.close_calls == 1
    assert tts.close_calls == 1

    await session.stop(force=True)

    assert session._closed is True
    assert session._stopping is False
    assert session._lifecycle_cleanup_error is None
    assert agent.close_calls == 2
    assert tts.close_calls == 2


@pytest.mark.asyncio
async def test_cancelled_stop_blocks_restart_until_cleanup_retry() -> None:
    class BlockingOnceDisconnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.disconnect_started = asyncio.Event()

        async def connect(self) -> None:
            self.connect_calls += 1
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.disconnect_calls == 1:
                self.disconnect_started.set()
                await asyncio.Event().wait()
            self.connected = False
            self.disconnected = True

    transport = BlockingOnceDisconnectTransport()
    session = Session(_full_config(transport=transport))
    await session.start()

    stopping = asyncio.create_task(session.stop(force=True))
    await transport.disconnect_started.wait()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert session._closed is False
    assert session._stopping is True
    assert isinstance(session._lifecycle_cleanup_error, RuntimeError)
    assert transport.connected is True

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await session.start()
    assert transport.connect_calls == 1

    await session.stop(force=True)

    assert session._closed is True
    assert session._stopping is False
    assert session._lifecycle_cleanup_error is None
    assert transport.disconnect_calls == 2
    assert transport.connected is False


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_session_teardown_finalizes_and_closes_journal(force: bool):
    transport = FakeTransport()
    journal = TrackingJournal()
    session = Session(_full_config(transport=transport, journal=journal, session_id="sess"))

    await session.start()
    await session.stop(force=force)

    assert journal.finalize_calls == 1
    assert journal.close_calls == 1


@pytest.mark.asyncio
async def test_session_stop_retries_after_backend_finalize_failure() -> None:
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
    session = Session(_full_config(journal=journal))
    await session.start()
    waiter = asyncio.create_task(session.wait_closed())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="finalize failed"):
        await session.stop()

    assert session._closed is False
    assert waiter.done() is False

    await session.stop()

    await asyncio.wait_for(waiter, timeout=1.0)
    assert session._closed is True
    assert session.journal.read() == []
    assert journal.finalize_calls == 2
    assert journal.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_session_teardown_closes_audio_providers(force: bool):
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

    await session.stop(force=force)

    assert calls == [
        "stt.close",
        "tts.aclose",
        "vad.close",
        "noise.close",
        "echo.aclose",
    ]


@pytest.mark.asyncio
async def test_force_stop_ends_active_stt_stream_without_close_hook():
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

    await session.stop(force=True)

    assert stt.end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_external_outbound_queue_survives_session_teardown(force: bool):
    transport = FakeTransport()
    queue = BoundedAudioQueue()
    session = Session(_full_config(transport=transport, outbound_queue=queue))

    await session.start()
    await session.stop(force=force)

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
async def test_force_stop_cancels_in_progress_start_without_waiting_for_connect():
    class BlockingConnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_entered = asyncio.Event()
            self.allow_connect = asyncio.Event()
            self.allow_receive_exit = asyncio.Event()

        async def connect(self) -> None:
            self.connect_entered.set()
            await self.allow_connect.wait()
            await super().connect()

        async def disconnect(self) -> None:
            self.connected = False
            await super().disconnect()

        async def receive_audio(self):
            await self.allow_receive_exit.wait()
            if False:
                yield _make_chunk()

    transport = BlockingConnectTransport()
    session = Session(_full_config(transport=transport))
    starting = asyncio.create_task(session.start())
    await transport.connect_entered.wait()
    stopping = asyncio.create_task(session.stop(force=True))

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, timeout=1)
    await asyncio.wait_for(stopping, timeout=1)

    assert session._closed
    assert not session.is_running
    assert not transport.connected


@pytest.mark.asyncio
async def test_force_stop_does_not_resurrect_after_cancel_resistant_connect():
    class CancelResistantConnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_entered = asyncio.Event()
            self.release_connect = asyncio.Event()
            self.disconnect_calls = 0

        async def connect(self) -> None:
            self.connect_entered.set()
            try:
                await self.release_connect.wait()
            except asyncio.CancelledError:
                # Simulate a provider that treats cancellation as a retryable
                # connect interruption and completes its handshake anyway.
                await self.release_connect.wait()
            await super().connect()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.connected = False
            await super().disconnect()

    transport = CancelResistantConnectTransport()
    session = Session(_full_config(transport=transport))
    starting = asyncio.create_task(session.start())
    await asyncio.wait_for(transport.connect_entered.wait(), timeout=1)

    await asyncio.wait_for(session.stop(force=True), timeout=1)
    assert session._closed
    assert not session.is_running

    transport.release_connect.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, timeout=1)

    assert session._closed
    assert not session.is_running
    assert session._audio_router.pipeline_task is None
    assert session._audio_router.outbound_task is None
    assert transport.disconnect_calls == 2
    assert not transport.connected


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
async def test_session_start_failure_rolls_back_agent_warmup_and_allows_retry() -> None:
    class RestartableWarmupAgent:
        def __init__(self) -> None:
            self.warmup_calls = 0
            self.rollback_calls = 0
            self.close_calls = 0

        async def warmup(self) -> None:
            self.warmup_calls += 1

        async def rollback_warmup(self) -> None:
            self.rollback_calls += 1

        async def aclose(self) -> None:
            self.close_calls += 1

        async def run(self, text: str) -> str:
            return text

    class FlakyTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_calls == 1:
                raise RuntimeError("boom")
            await super().connect()

    agent = RestartableWarmupAgent()
    transport = FlakyTransport()
    session = Session(_full_config(agent=agent, transport=transport))

    with pytest.raises(RuntimeError, match="boom"):
        await session.start()

    assert agent.warmup_calls == 1
    assert agent.rollback_calls == 1
    assert agent.close_calls == 0

    await session.start()

    assert agent.warmup_calls == 2
    assert agent.rollback_calls == 1
    assert session.is_running

    await session.stop()
    assert agent.close_calls == 1


@pytest.mark.asyncio
async def test_session_stop_idempotent():
    session = Session(_full_config())
    await session.stop()
    assert not session.is_running


@pytest.mark.asyncio
async def test_closed_session_stop_retries_pending_debug_backend_close() -> None:
    class PendingCloseJournal(TrackingJournal):
        @property
        def close_complete(self) -> bool:
            return self.close_calls >= 2

    journal = PendingCloseJournal()
    session = Session(_full_config(journal=journal))

    await session.stop()

    assert session._closed is True
    assert journal.close_calls == 1
    assert session._debug_backends._pending_journal_close is journal

    await session.stop()

    assert journal.close_calls == 2
    assert session._debug_backends._pending_journal_close is None


@pytest.mark.asyncio
async def test_manual_turns_are_rejected_while_session_stop_is_in_progress() -> None:
    class BlockingDisconnectTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.disconnect_started = asyncio.Event()
            self.release_disconnect = asyncio.Event()

        async def disconnect(self) -> None:
            self.disconnect_started.set()
            await self.release_disconnect.wait()
            await super().disconnect()

    transport = BlockingDisconnectTransport()
    session = Session(_full_config(transport=transport))
    stopping = asyncio.create_task(session.stop())
    await transport.disconnect_started.wait()

    try:
        assert session._stopping is True
        assert session._turn_manager._shutting_down is True
        with pytest.raises(RuntimeError, match="stopping or has been stopped"):
            await session.start_turn()
        with pytest.raises(RuntimeError, match="stopping or has been stopped"):
            await session.end_turn()
    finally:
        transport.release_disconnect.set()
        await stopping

    with pytest.raises(RuntimeError, match="stopping or has been stopped"):
        await session.start_turn()


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
async def test_cancel_turn_reclaims_late_context_for_captured_manager_turn():
    class BlockingClearTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.clear_entered = asyncio.Event()
            self.release_clear = asyncio.Event()

        async def clear_audio(self) -> None:
            self.clear_entered.set()
            await self.release_clear.wait()

    transport = BlockingClearTransport()
    session = Session(_full_config(transport=transport))
    manager_token = CancelToken()
    session._turn_manager.begin_application_turn("manager-only-turn", manager_token)
    assert session._turn is None

    cancel_task = asyncio.create_task(session.cancel_turn())
    try:
        await asyncio.wait_for(transport.clear_entered.wait(), timeout=1)
        late_turn = TurnContext("late-turn", manager_token)
        session._turn = late_turn
        session._turn_generation = late_turn.generation

        transport.release_clear.set()
        await cancel_task

        assert session._turn is None
        assert session._turn_manager.state is TurnManagerState.IDLE
    finally:
        transport.release_clear.set()
        if not cancel_task.done():
            await cancel_task


@pytest.mark.asyncio
async def test_stale_cancel_turn_cleanup_preserves_barge_in_successor():
    class BlockingFirstClearTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_clear_entered = asyncio.Event()
            self.release_first_clear = asyncio.Event()

        async def clear_audio(self) -> None:
            self.clear_calls += 1
            if self.clear_calls == 1:
                self.first_clear_entered.set()
                await self.release_first_clear.wait()

    transport = BlockingFirstClearTransport()
    session = Session(_full_config(transport=transport))
    session._is_running = True
    old_turn = TurnContext("old-turn", CancelToken())
    session._turn = old_turn
    session._turn_generation = old_turn.generation
    session._turn_manager.begin_application_turn(old_turn.id, old_turn.cancel_token)
    await session._turn_manager.bot_started_speaking()
    cancel_task = asyncio.create_task(session.cancel_turn())

    try:
        await asyncio.wait_for(transport.first_clear_entered.wait(), timeout=1)
        await session._turn_manager._handle_speech_start()

        successor = session._turn
        successor_stt_task = session._stt_committer.stt_task
        assert successor is not None and successor is not old_turn
        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
        assert successor_stt_task is not None and not successor_stt_task.done()

        transport.release_first_clear.set()
        await cancel_task

        assert session._turn is successor
        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
        assert session._stt_committer.stt_task is successor_stt_task
        assert not successor_stt_task.done()
    finally:
        transport.release_first_clear.set()
        if not cancel_task.done():
            await cancel_task
        await session._stt_committer.cancel(session._turn)
        session._turn_manager.reset()
        session._is_running = False


@pytest.mark.asyncio
async def test_stale_cancel_tts_playback_preserves_barge_in_successor():
    class OrderingTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = asyncio.Event()

        async def cancel(self) -> None:
            self.cancelled.set()

    class BlockingFirstClearTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_clear_entered = asyncio.Event()
            self.release_first_clear = asyncio.Event()

        async def clear_audio(self) -> None:
            self.clear_calls += 1
            if self.clear_calls == 1:
                self.first_clear_entered.set()
                await self.release_first_clear.wait()

    transport = BlockingFirstClearTransport()
    tts = OrderingTTS()
    session = Session(_full_config(transport=transport, tts=tts))
    session._is_running = True
    old_turn = TurnContext("old-turn", CancelToken())
    session._turn = old_turn
    session._turn_generation = old_turn.generation
    session._turn_manager.begin_application_turn(old_turn.id, old_turn.cancel_token)
    await session._turn_manager.bot_started_speaking()
    cancel_task = asyncio.create_task(session.cancel_tts_playback())

    try:
        await asyncio.wait_for(transport.first_clear_entered.wait(), timeout=1)
        assert tts.cancelled.is_set()
        await session._turn_manager._handle_speech_start()

        successor = session._turn
        assert successor is not None and successor is not old_turn
        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING

        # The successor can finish its agent work and begin playback while
        # the old transport clear remains blocked.
        session._turn_manager._state = TurnManagerState.BOT_SPEAKING

        transport.release_first_clear.set()
        await cancel_task

        assert session._turn is successor
        assert session._turn_manager.state is TurnManagerState.BOT_SPEAKING
        assert not successor.cancel_token.is_cancelled
    finally:
        transport.release_first_clear.set()
        if not cancel_task.done():
            await cancel_task
        await session._stt_committer.cancel(session._turn)
        session._turn_manager.reset()
        session._is_running = False


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
async def test_runtime_barge_in_returns_after_cutoff_before_slow_handlers_finish():
    class CutoffTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cleared = asyncio.Event()

        async def clear_audio(self) -> None:
            self.cleared.set()

    transport = CutoffTransport()
    session = Session(_full_config(transport=transport))
    turn = TurnContext("test-turn-fast-cutoff", CancelToken())
    session._turn = turn
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()
    interruptions: list[Interruption] = []

    async def slow_handler(event: Interruption) -> None:
        interruptions.append(event)
        handler_started.set()
        await handler_release.wait()

    session.event_bus.subscribe(Interruption, slow_handler)

    result = await asyncio.wait_for(session._cancel.for_barge_in(), timeout=0.25)

    assert result is True
    assert transport.cleared.is_set()
    assert turn.cancel_token.is_cancelled
    await asyncio.wait_for(handler_started.wait(), timeout=0.25)
    assert session._runtime_scope.tasks("barge_in_cleanup")
    assert interruptions[0].turn_id == turn.id

    handler_release.set()
    await session._runtime_scope.drain("barge_in_cleanup")


@pytest.mark.asyncio
async def test_graceful_stop_drains_barge_in_cleanup_before_provider_teardown():
    transport = FakeTransport()
    session = Session(_full_config(transport=transport))
    await session.start()
    session._turn = TurnContext("stop-during-barge-in", CancelToken())
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()
    handler_finished = asyncio.Event()

    async def slow_handler(_event: Interruption) -> None:
        handler_started.set()
        await handler_release.wait()
        assert not transport.disconnected
        handler_finished.set()

    session.event_bus.subscribe(Interruption, slow_handler)
    assert await session._cancel.for_barge_in() is True
    await asyncio.wait_for(handler_started.wait(), timeout=0.25)

    stopping = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    assert not transport.disconnected

    handler_release.set()
    await asyncio.wait_for(stopping, timeout=1)

    assert handler_finished.is_set()
    assert transport.disconnected
    assert not session._runtime_scope.tasks("barge_in_cleanup")


@pytest.mark.asyncio
async def test_vad_barge_in_starts_successor_while_old_notifications_drain():
    session = Session(_full_config())
    session._is_running = True
    old_turn = TurnContext("old-turn", CancelToken())
    session._turn = old_turn
    session._turn_manager._state = TurnManagerState.BOT_SPEAKING
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()

    async def slow_handler(_event: Interruption) -> None:
        handler_started.set()
        await handler_release.wait()

    session.event_bus.subscribe(Interruption, slow_handler)

    try:
        await asyncio.wait_for(
            session._turn_manager.on_vad_event(VADStartSpeaking()),
            timeout=0.25,
        )

        assert session._turn_manager.state == TurnManagerState.USER_SPEAKING
        assert old_turn.cancel_token.is_cancelled
        await asyncio.wait_for(handler_started.wait(), timeout=0.25)
    finally:
        handler_release.set()
        await session._runtime_scope.drain("barge_in_cleanup")
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_runtime_barge_in_bounds_a_stuck_transport_clear(
    monkeypatch: pytest.MonkeyPatch,
):
    class StuckClearTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.clear_started = asyncio.Event()
            self.clear_cancelled = asyncio.Event()

        async def clear_audio(self) -> None:
            self.clear_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.clear_cancelled.set()
                raise

    monkeypatch.setattr("easycat.session._session._BARGE_IN_CUTOFF_TIMEOUT_S", 0.01)
    transport = StuckClearTransport()
    session = Session(_full_config(transport=transport))
    session._turn = TurnContext("stuck-clear-turn", CancelToken())

    result = await asyncio.wait_for(session._cancel.for_barge_in(), timeout=0.1)

    assert result is True
    assert transport.clear_started.is_set()
    assert transport.clear_cancelled.is_set()
    await session._runtime_scope.drain("barge_in_cleanup")


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
