"""Session event bus and playback mark tests."""

from __future__ import annotations

import asyncio

import pytest

from easycat.audio_format import AudioChunk
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioOut,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    CallAnswered,
    Error,
    ErrorStage,
    EventBus,
    EventHandler,
    EventSubscription,
    Interruption,
    PlaybackMarkAck,
    STTFinal,
    ToolCallResult,
    ToolCallStarted,
    TransportAudioDelivered,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from tests.session._session_core_helpers import (
    FakePlaybackAckTransport,
    ReportingTransport,
    _full_config,
    _make_chunk,
)


@pytest.mark.asyncio
async def test_session_event_bus_accessible():
    session = Session(_full_config())
    assert session.event_bus is not None
    received: list = []
    token = session.subscribe_event(STTFinal, lambda e: received.append(e))
    await session.event_bus.emit(STTFinal(text="test"))
    token.unsubscribe()
    await session.event_bus.emit(STTFinal(text="ignored"))

    assert token.active is False
    assert len(received) == 1


@pytest.mark.asyncio
async def test_session_subscribe_agent_events_helper():
    session = Session(_full_config())

    deltas: list[str] = []
    finals: list[str] = []
    tools_started: list[str] = []
    tools_results: list[str] = []

    registrations = session.subscribe_agent_events(
        on_delta=lambda e: deltas.append(e.text),
        on_final=lambda e: finals.append(e.text),
        on_tool_started=lambda e: tools_started.append(e.tool_name),
        on_tool_result=lambda e: tools_results.append(e.result),
    )

    await session.event_bus.emit(AgentDelta(text="chunk"))
    await session.event_bus.emit(AgentFinal(text="done"))
    await session.event_bus.emit(ToolCallStarted(tool_name="lookup", call_id="c1"))
    await session.event_bus.emit(ToolCallResult(call_id="c1", result="42"))

    assert deltas == ["chunk"]
    assert finals == ["done"]
    assert tools_started == ["lookup"]
    assert tools_results == ["42"]

    session.unsubscribe_handlers(registrations)
    await session.event_bus.emit(AgentFinal(text="done again"))
    assert deltas == ["chunk"]
    assert finals == ["done"]


@pytest.mark.asyncio
async def test_session_on_convenience_method():
    """session.on() subscribes with unwrapped callback arguments."""
    session = Session(_full_config())

    transcripts: list[str] = []
    responses: list[str] = []
    deltas: list[str] = []
    tools: list[tuple[str, str]] = []
    tool_results: list[tuple[str, str]] = []
    lifecycle: list[str] = []
    errors: list[tuple[BaseException, str]] = []

    registrations = session.on(
        user_transcript=lambda text: transcripts.append(text),
        agent_response=lambda text: responses.append(text),
        agent_delta=lambda text: deltas.append(text),
        tool_started=lambda name, cid: tools.append((name, cid)),
        tool_result=lambda cid, result: tool_results.append((cid, result)),
        turn_started=lambda: lifecycle.append("turn_started"),
        turn_ended=lambda: lifecycle.append("turn_ended"),
        bot_started_speaking=lambda: lifecycle.append("bot_started"),
        bot_stopped_speaking=lambda: lifecycle.append("bot_stopped"),
        interruption=lambda: lifecycle.append("interruption"),
        error=lambda exc, ctx: errors.append((exc, ctx)),
    )

    # Emit events and verify callbacks receive unwrapped args.
    await session.event_bus.emit(STTFinal(text="hello"))
    await session.event_bus.emit(AgentDelta(text="hi "))
    await session.event_bus.emit(AgentFinal(text="hi there"))
    await session.event_bus.emit(ToolCallStarted(tool_name="search", call_id="c1"))
    await session.event_bus.emit(ToolCallResult(call_id="c1", result="found"))
    await session.event_bus.emit(TurnStarted())
    await session.event_bus.emit(TurnEnded())
    await session.event_bus.emit(BotStartedSpeaking())
    await session.event_bus.emit(BotStoppedSpeaking())
    await session.event_bus.emit(Interruption())
    await session.event_bus.emit(Error(exception=ValueError("boom"), stage=ErrorStage.AGENT))

    assert transcripts == ["hello"]
    assert responses == ["hi there"]
    assert deltas == ["hi "]
    assert tools == [("search", "c1")]
    assert tool_results == [("c1", "found")]
    assert lifecycle == [
        "turn_started",
        "turn_ended",
        "bot_started",
        "bot_stopped",
        "interruption",
    ]
    assert len(errors) == 1
    assert str(errors[0][0]) == "boom"
    assert errors[0][1] == "agent"

    # Unsubscribe and verify no further callbacks.
    session.unsubscribe_handlers(registrations)
    await session.event_bus.emit(STTFinal(text="ignored"))
    assert transcripts == ["hello"]


@pytest.mark.asyncio
async def test_shared_event_bus_scopes_session_convenience_handlers() -> None:
    bus = EventBus(handler_error_policy="raise")
    victim = Session(_full_config(event_bus=bus, session_id="victim-session"))
    other = Session(_full_config(event_bus=bus, session_id="other-session"))
    transcripts: list[str] = []
    agent_finals: list[str] = []
    on_registrations = victim.on(user_transcript=transcripts.append)
    agent_registrations = victim.subscribe_agent_events(
        on_final=lambda event: agent_finals.append(event.text)
    )

    try:
        await bus.emit(STTFinal(text="other transcript", session_id=other.session_id))
        await bus.emit(AgentFinal(text="other response", session_id=other.session_id))
        await bus.emit(STTFinal(text="ambiguous transcript"))
        await bus.emit(AgentFinal(text="ambiguous response"))

        assert transcripts == []
        assert agent_finals == []

        await bus.emit(STTFinal(text="victim transcript", session_id=victim.session_id))
        await bus.emit(AgentFinal(text="victim response", session_id=victim.session_id))

        assert transcripts == ["victim transcript"]
        assert agent_finals == ["victim response"]

        victim.unsubscribe_handlers(on_registrations)
        victim.unsubscribe_handlers(agent_registrations)
        registration_handlers = {
            handler for _event_type, handler in (*on_registrations, *agent_registrations)
        }
        assert all(
            subscription.handler not in registration_handlers
            for subscription in victim._event_subscriptions
        )
        await bus.emit(STTFinal(text="after unsubscribe", session_id=victim.session_id))
        await bus.emit(AgentFinal(text="after unsubscribe", session_id=victim.session_id))

        assert transcripts == ["victim transcript"]
        assert agent_finals == ["victim response"]
    finally:
        await victim.stop(force=True)
        await other.stop(force=True)


@pytest.mark.asyncio
async def test_session_stop_releases_convenience_handlers_from_shared_bus() -> None:
    bus = EventBus(handler_error_policy="raise")
    session = Session(_full_config(event_bus=bus, session_id="owned-session"))
    transcripts: list[str] = []
    agent_finals: list[str] = []
    external_events: list[STTFinal] = []

    def observe_external(event: STTFinal) -> None:
        external_events.append(event)

    external = bus.subscribe(STTFinal, observe_external)
    on_registrations = session.on(user_transcript=transcripts.append)
    agent_registrations = session.subscribe_agent_events(
        on_final=lambda event: agent_finals.append(event.text)
    )

    try:
        await session.stop(force=True)
        late_transcript = STTFinal(text="late transcript", session_id=session.session_id)
        await bus.emit(late_transcript)
        await bus.emit(AgentFinal(text="late response", session_id=session.session_id))

        assert transcripts == []
        assert agent_finals == []
        assert external_events == [late_transcript]
        assert all(
            handler not in bus.subscribers(event_type)
            for event_type, handler in (*on_registrations, *agent_registrations)
        )
        assert session._event_subscriptions == []
        assert external.active is True
    finally:
        external.unsubscribe()


@pytest.mark.asyncio
async def test_session_events_include_correlation_ids():
    session = Session(_full_config())
    seen: list[TurnStarted | Interruption] = []
    session.event_bus.subscribe(TurnStarted, lambda e: seen.append(e))
    session.event_bus.subscribe(Interruption, lambda e: seen.append(e))

    await session._emit(TurnStarted())
    await session.cancel_turn(barge_in=True)

    assert seen
    for event in seen:
        assert event.session_id == session.session_id


@pytest.mark.asyncio
async def test_shared_event_bus_scopes_session_owned_turn_handlers() -> None:
    bus = EventBus(handler_error_policy="raise")
    victim_journal = InMemoryRingBuffer()
    other_journal = InMemoryRingBuffer()
    victim = Session(
        _full_config(
            event_bus=bus,
            session_id="victim-session",
            journal=victim_journal,
        )
    )
    other = Session(
        _full_config(
            event_bus=bus,
            session_id="other-session",
            journal=other_journal,
        )
    )
    victim._is_running = True
    other._is_running = True

    try:
        await bus.emit(TurnStarted(turn_id="ambiguous-turn"))

        assert victim.current_turn is None
        assert other.current_turn is None

        await bus.emit(TurnStarted(session_id=victim.session_id, turn_id="victim-turn"))

        assert victim.current_turn is not None
        assert victim.current_turn.id == "victim-turn"
        assert other.current_turn is None
        assert any(record.name == "turn_started" for record in victim_journal.read())
        assert other_journal.read() == []
    finally:
        await victim.stop(force=True)
        await other.stop(force=True)


@pytest.mark.asyncio
async def test_shared_event_bus_stays_ambiguous_after_peer_stops() -> None:
    bus = EventBus(handler_error_policy="raise")
    survivor = Session(_full_config(event_bus=bus, session_id="survivor-session"))
    peer = Session(_full_config(event_bus=bus, session_id="peer-session"))
    survivor._is_running = True

    await peer.stop(force=True)
    try:
        await bus.emit(TurnStarted(turn_id="late-peer-turn"))

        assert survivor.current_turn is None
    finally:
        await survivor.stop(force=True)


@pytest.mark.asyncio
async def test_failed_second_session_does_not_poison_private_bus_compatibility() -> None:
    class FailNextSubscriptionBus(EventBus):
        fail_next_subscription = False

        def subscribe(self, event_type: type, handler: EventHandler) -> EventSubscription:
            if self.fail_next_subscription:
                self.fail_next_subscription = False
                raise RuntimeError("subscribe failed")
            return super().subscribe(event_type, handler)

    bus = FailNextSubscriptionBus(handler_error_policy="raise")
    survivor = Session(_full_config(event_bus=bus, session_id="survivor-session"))
    survivor._is_running = True
    bus.fail_next_subscription = True

    with pytest.raises(RuntimeError, match="subscribe failed"):
        Session(_full_config(event_bus=bus, session_id="failed-session"))

    try:
        await bus.emit(TurnStarted(turn_id="private-bus-turn"))

        assert survivor.current_turn is not None
        assert survivor.current_turn.id == "private-bus-turn"
        assert not getattr(bus, "_easycat_was_shared_by_sessions", False)
    finally:
        await survivor.stop(force=True)


@pytest.mark.asyncio
async def test_hand_built_turn_started_installs_identity_before_public_observation() -> None:
    bus = EventBus(handler_error_policy="raise")
    observed_turn_ids: list[tuple[str, str | None]] = []

    def observe(source: str, _event: TurnStarted) -> None:
        turn = session.current_turn
        observed_turn_ids.append((source, turn.id if turn is not None else None))

    global_external = bus.subscribe_all(lambda event: observe("global", event))
    external = bus.subscribe(TurnStarted, lambda event: observe("exact", event))
    session = Session(_full_config(event_bus=bus, session_id="command-session"))
    session._is_running = True
    try:
        await bus.emit(TurnStarted(session_id=session.session_id, turn_id="hand-built-turn"))

        assert session.current_turn is not None
        assert session.current_turn.id == "hand-built-turn"
        assert observed_turn_ids == [
            ("global", "hand-built-turn"),
            ("exact", "hand-built-turn"),
        ]
    finally:
        global_external.unsubscribe()
        external.unsubscribe()
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_internal_voice_publication_precedes_existing_public_observers() -> None:
    bus = EventBus(handler_error_policy="raise")
    observations: list[tuple[str, str | None, bool]] = []
    public_events: list[TurnStarted] = []

    def observe(source: str, _event: TurnStarted) -> None:
        turn = session.current_turn
        observations.append(
            (
                source,
                turn.id if turn is not None else None,
                session._stt_committer.is_active,
            )
        )

    global_external = bus.subscribe_all(lambda event: observe("global", event))

    def observe_exact(event: TurnStarted) -> None:
        public_events.append(event)
        observe("exact", event)

    external = bus.subscribe(TurnStarted, observe_exact)
    session = Session(_full_config(event_bus=bus, session_id="private-publication-session"))
    session._is_running = True
    try:
        await session._turn_manager.start_turn()

        assert session.current_turn is not None
        turn_id = session.current_turn.id
        assert observations == [
            ("global", turn_id, True),
            ("exact", turn_id, True),
        ]
        assert len(public_events) == 1
        assert not hasattr(public_events[0], "activity")
        assert not hasattr(public_events[0], "identity")
    finally:
        global_external.unsubscribe()
        external.unsubscribe()
        await session.stop(force=True)


@pytest.mark.asyncio
async def test_session_stop_releases_only_session_owned_event_handlers() -> None:
    bus = EventBus(handler_error_policy="raise")
    observed: list[TurnStarted] = []

    def observe(event: TurnStarted) -> None:
        observed.append(event)

    external = bus.subscribe(TurnStarted, observe)
    session = Session(
        _full_config(
            event_bus=bus,
            session_id="stopped-session",
            greeting="hello",
            journal=InMemoryRingBuffer(),
        )
    )

    await session.stop(force=True)

    assert external.active is True
    assert bus.subscribers(TurnStarted) == [observe]
    assert bus._reserved_handlers.get(TurnStarted, []) == []
    for event_type in (
        PlaybackMarkAck,
        TransportAudioDelivered,
        CallAnswered,
        VADStartSpeaking,
        VADStopSpeaking,
        STTFinal,
        TurnEnded,
    ):
        assert bus.subscribers(event_type) == []

    event = TurnStarted(session_id="another-session", turn_id="another-turn")
    await bus.emit(event)
    assert observed == [event]


@pytest.mark.asyncio
async def test_failed_session_stop_releases_owned_event_handlers() -> None:
    class FailingDisconnectTransport(ReportingTransport):
        async def disconnect(self) -> None:
            raise RuntimeError("disconnect failed")

    bus = EventBus(handler_error_policy="raise")
    observed: list[TurnStarted] = []

    def observe(event: TurnStarted) -> None:
        observed.append(event)

    external = bus.subscribe(TurnStarted, observe)
    session = Session(
        _full_config(
            event_bus=bus,
            session_id="failed-stop-session",
            transport=FailingDisconnectTransport(),
        )
    )

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await session.stop(force=True)

    assert external.active is True
    assert bus.subscribers(TurnStarted) == [observe]
    assert bus._reserved_handlers.get(TurnStarted, []) == []
    assert session._event_subscriptions == []

    replacement = Session(
        _full_config(
            event_bus=bus,
            session_id="replacement-session",
        )
    )
    replacement._is_running = True
    try:
        late_event = TurnStarted(turn_id="late-failed-session-turn")
        await bus.emit(late_event)

        assert replacement.current_turn is None
        assert observed == [late_event]
        assert getattr(bus, "_easycat_was_shared_by_sessions", False)
    finally:
        await replacement.stop(force=True)


def test_begin_turn_exposes_coherent_test_and_replay_seam() -> None:
    session = Session(_full_config())

    turn = session.begin_turn("turn-harness")

    assert session.current_turn is turn
    assert turn.id == "turn-harness"
    assert session.cancel_token is turn.cancel_token
    with pytest.raises(ValueError, match="non-empty"):
        session.begin_turn("  ")


@pytest.mark.asyncio
async def test_playback_mark_ack_scoped_to_current_turn():
    """Playback marks are scoped to the current TurnContext.
    Each new turn has its own playback_mark_to_bytes map, so marks
    from a previous turn are naturally absent from the current turn's map."""
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    # Use a small interval so a single test chunk triggers a mark.
    session._playback_mark_bytes_interval = 1

    # ── First turn ──
    first_turn = session.begin_turn("turn-first")
    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()
    first_turn_marks = list(first_turn.playback_mark_to_bytes)
    assert len(first_turn_marks) == 1

    # ── Second turn (replaces the TurnContext) ──
    second_turn = session.begin_turn("turn-second")

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()
    second_turn_marks = list(second_turn.playback_mark_to_bytes)
    assert len(second_turn_marks) == 1

    # Ack for the second turn's mark works.
    session._audio_router.on_playback_ack(PlaybackMarkAck(mark_name=second_turn_marks[0]))
    assert len(second_turn.playback_ack_log) == 1
    assert second_turn.playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_playback_mark_ack_tracks_transport_confirmed_name():
    class CanonicalizingPlaybackAckTransport(FakePlaybackAckTransport):
        async def send_playback_mark(self, name: str | None = None) -> str:
            requested_name = name or f"mark_{len(self.playback_marks) + 1}"
            canonical_name = f"canonical::{requested_name}"
            self.playback_marks.append(canonical_name)
            return canonical_name

    transport = CanonicalizingPlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 1
    turn = session.begin_turn("test-turn")

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()

    canonical_mark = transport.playback_marks[-1]
    session._audio_router.on_playback_ack(PlaybackMarkAck(mark_name=canonical_mark))

    assert len(turn.playback_ack_log) == 1
    assert turn.playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_trailing_playback_mark_emitted_while_session_running():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 10_000

    await session.start()
    try:
        session.begin_turn("test-turn")
        await session._outbound_queue.put(_make_chunk())

        await asyncio.wait_for(transport.playback_mark_sent.wait(), timeout=1.0)

        assert len(transport.playback_marks) == 1
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_trailing_playback_mark_flushed_after_speaking_turn_already_drained():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._audio_router._playback_mark_bytes_interval = 10_000

    await session.start()
    try:
        turn = session.begin_turn("test-turn")
        await session._turn_manager.bot_started_speaking()
        await session._outbound_queue.put(_make_chunk())

        def drained_without_mark() -> bool:
            return (
                turn.bytes_since_last_mark > 0
                and session._outbound_queue.empty()
                and session._audio_router._outbound_in_flight == 0
            )

        for _ in range(100):
            if drained_without_mark():
                break
            await asyncio.sleep(0.01)

        assert drained_without_mark()
        assert transport.playback_marks == []

        await session._tts_scheduler.finalize_speaking_turn(turn)

        assert len(transport.playback_marks) == 1
        assert turn.bytes_since_last_mark == 0
        assert list(turn.playback_mark_to_bytes.values()) == [320]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_trailing_playback_mark_not_flushed_for_replaced_turn():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._audio_router._playback_mark_bytes_interval = 10_000

    await session.start()
    try:
        old_turn = session.begin_turn("old-turn")
        await session._turn_manager.bot_started_speaking()
        await session._outbound_queue.put(_make_chunk())

        def old_turn_drained_without_mark() -> bool:
            return (
                old_turn.bytes_since_last_mark > 0
                and session._outbound_queue.empty()
                and session._audio_router._outbound_in_flight == 0
            )

        for _ in range(100):
            if old_turn_drained_without_mark():
                break
            await asyncio.sleep(0.01)

        assert old_turn_drained_without_mark()
        assert transport.playback_marks == []

        new_turn = session.begin_turn("new-turn")

        await session._tts_scheduler.finalize_speaking_turn(
            old_turn,
            turn_generation=old_turn.generation,
        )

        assert transport.playback_marks == []
        assert old_turn.bytes_since_last_mark == 320
        assert session.current_turn is new_turn
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_buffered_transport_delivery_is_counted_only_after_report() -> None:
    transport = ReportingTransport()
    session = Session(_full_config(transport=transport))
    turn = session.begin_turn("test-turn")
    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    chunk = _make_chunk()
    await session._outbound_queue.put(chunk)
    await session._audio_router._drain_outbound_audio()

    assert transport.sent == [chunk]
    assert turn.audio_bytes_sent == 0
    assert seen == []

    await session.event_bus.emit(
        TransportAudioDelivered(
            chunk=chunk,
            turn_id=turn.id,
            turn_ref=turn,
        )
    )

    assert turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].chunk is chunk
    assert seen[0].turn_id == "test-turn"


@pytest.mark.asyncio
async def test_buffered_transport_delivery_on_shared_bus_stays_session_scoped() -> None:
    bus = EventBus()
    victim = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="victim-session",
        )
    )
    other = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="other-session",
        )
    )
    victim_turn = victim.begin_turn("victim-turn")

    seen: list[AudioOut] = []
    bus.subscribe(AudioOut, lambda event: seen.append(event))
    chunk = _make_chunk(16)

    await bus.emit(
        TransportAudioDelivered(
            chunk=chunk,
            turn_id=victim_turn.id,
            turn_ref=victim_turn,
        )
    )

    assert victim_turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].session_id == "victim-session"
    assert seen[0].turn_id == "victim-turn"

    scoped_chunk = _make_chunk(24)
    await bus.emit(
        TransportAudioDelivered(
            chunk=scoped_chunk,
            session_id=victim.session_id,
            turn_id=victim_turn.id,
            turn_ref=victim_turn,
        )
    )

    assert victim_turn.audio_bytes_sent == len(chunk.data) + len(scoped_chunk.data)
    assert len(seen) == 2
    assert {event.session_id for event in seen} == {"victim-session"}
    assert all(event.turn_id == "victim-turn" for event in seen)
    assert other.session_id not in {event.session_id for event in seen}


@pytest.mark.asyncio
async def test_buffered_transport_delivery_unscoped_falls_back_to_active_turn() -> None:
    # A custom reporting transport on a private (single-session) bus may emit
    # a bare TransportAudioDelivered with no session_id/turn_id/turn_ref. That
    # fully-unscoped callback must still count bytes against the active turn
    # and emit exactly one AudioOut — otherwise older reporting transports
    # regress to silently dropping buffered-playback accounting.
    session = Session(_full_config(transport=ReportingTransport()))
    turn = session.begin_turn("test-turn")

    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    chunk = _make_chunk(16)
    await session.event_bus.emit(TransportAudioDelivered(chunk=chunk))

    assert turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].chunk is chunk
    assert seen[0].turn_id == "test-turn"


@pytest.mark.asyncio
async def test_buffered_transport_delivery_unscoped_is_dropped_on_shared_bus() -> None:
    bus = EventBus()
    victim = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="victim-session",
        )
    )
    other = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="other-session",
        )
    )
    victim_turn = victim.begin_turn("victim-turn")
    other_turn = other.begin_turn("other-turn")

    seen: list[AudioOut] = []
    bus.subscribe(AudioOut, lambda event: seen.append(event))

    await bus.emit(TransportAudioDelivered(chunk=_make_chunk(16)))

    assert victim_turn.audio_bytes_sent == 0
    assert other_turn.audio_bytes_sent == 0
    assert seen == []


@pytest.mark.asyncio
async def test_buffered_transport_delivery_unscoped_idle_session_is_dropped() -> None:
    session = Session(_full_config(transport=ReportingTransport()))

    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    await session.event_bus.emit(TransportAudioDelivered(chunk=_make_chunk(16)))

    assert seen == []


@pytest.mark.asyncio
async def test_failed_send_does_not_emit_audio_out_or_count_bytes() -> None:
    class RejectingTransport(FakePlaybackAckTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            return False

    transport = RejectingTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 1
    turn = session.begin_turn("test-turn")
    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()

    assert turn.audio_bytes_sent == 0
    assert turn.bytes_since_last_mark == 0
    assert transport.playback_marks == []
    assert seen == []
