from __future__ import annotations

from tests.observability._observability_helpers import (
    _FakeMeter,
    _FakeTracer,
    _make_run_ctx,
    _make_turn_ctx,
    asyncio,
    observability,
    pytest,
)


def test_observability_is_noop_without_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_get_tracer", lambda: None)
    monkeypatch.setattr(observability, "_get_meter", lambda: None)

    with observability.span("easycat.session", {"easycat.surface": "agent_bridge"}):
        pass
    observability.record_histogram(
        "easycat.stage.latency",
        0.05,
        {"easycat.stage": "stt", "easycat.result": "pass"},
    )
    observability.increment_counter("easycat.turns.total", attributes={"easycat.result": "pass"})
    observability.observe_gauge("easycat.queue.depth", 3, {"easycat.stage": "tts"})


def test_opentelemetry_handles_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import ModuleType

    meter = object()
    tracer = object()
    meter_lookups = 0
    tracer_lookups = 0

    def get_meter(name: str) -> object:
        nonlocal meter_lookups
        meter_lookups += 1
        assert name == observability.INSTRUMENTATION_NAME
        return meter

    def get_tracer(name: str) -> object:
        nonlocal tracer_lookups
        tracer_lookups += 1
        assert name == observability.INSTRUMENTATION_NAME
        return tracer

    package = ModuleType("opentelemetry")
    metrics = ModuleType("opentelemetry.metrics")
    trace = ModuleType("opentelemetry.trace")
    metrics.get_meter = get_meter
    trace.get_tracer = get_tracer
    package.metrics = metrics
    package.trace = trace
    monkeypatch.setitem(sys.modules, "opentelemetry", package)
    monkeypatch.setitem(sys.modules, "opentelemetry.metrics", metrics)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace)

    assert observability._get_meter() is meter
    assert observability._get_meter() is meter
    assert observability._get_tracer() is tracer
    assert observability._get_tracer() is tracer
    assert meter_lookups == 1
    assert tracer_lookups == 1


def test_opentelemetry_handles_cache_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.metrics", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)

    assert observability._get_meter() is None
    assert observability._get_meter() is None
    assert observability._get_tracer() is None
    assert observability._get_tracer() is None
    assert observability._get_meter.cache_info().misses == 1
    assert observability._get_meter.cache_info().hits == 1
    assert observability._get_tracer.cache_info().misses == 1
    assert observability._get_tracer.cache_info().hits == 1


def test_observable_gauge_uses_callback_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    monkeypatch.setattr(
        observability,
        "_make_observation",
        lambda value, attributes: (value, attributes),
    )

    observability.observe_gauge("easycat.queue.depth", 7, {"easycat.stage": "transport"})

    gauge = meter.gauges["easycat.queue.depth"]
    assert not hasattr(gauge, "observe")
    assert gauge.collect() == [(7, {"easycat.stage": "transport"})]


@pytest.mark.asyncio
async def test_text_turn_emits_session_and_agent_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    from easycat import create_text_session

    class Agent:
        async def run(self, text: str) -> str:
            return f"echo: {text}"

    tracer = _FakeTracer()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: None)
    session = create_text_session(agent=Agent(), debug="off")

    result = await session.send_text("hello")

    assert result == "echo: hello"
    assert [name for name, _attrs in tracer.started] == [
        "easycat.session",
        "easycat.agent.invoke",
        "easycat.turn.commit",
    ]


@pytest.mark.asyncio
async def test_text_turn_emits_turn_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    from easycat import create_text_session

    class Agent:
        async def run(self, text: str) -> str:
            return f"echo: {text}"

    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_tracer", lambda: None)
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    monkeypatch.setattr(
        observability,
        "_make_observation",
        lambda value, attributes: (value, attributes),
    )
    session = create_text_session(agent=Agent(), debug="off")

    result = await session.send_text("hello")

    assert result == "echo: hello"
    assert meter.histograms["easycat.turn.latency"].records
    assert meter.counters["easycat.turns.total"].adds == [
        (1, {"easycat.surface": "agent_bridge", "easycat.result": "pass"})
    ]


@pytest.mark.asyncio
async def test_audio_queue_emits_drop_counter_and_depth_gauge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat._bounded_queue import BoundedAudioQueue, DropPolicy
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk

    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    monkeypatch.setattr(
        observability,
        "_make_observation",
        lambda value, attributes: (value, attributes),
    )
    queue = BoundedAudioQueue(max_size=1, policy=DropPolicy.DROP_NEWEST)
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    assert await queue.put(chunk)
    assert not await queue.put(chunk)

    assert meter.counters["easycat.queue.dropped.total"].adds == [
        (1, {"easycat.stage": "audio_queue"})
    ]
    assert meter.gauges["easycat.queue.depth"].collect() == [(1, {"easycat.stage": "audio_queue"})]


@pytest.mark.asyncio
async def test_audio_queue_refreshes_depth_after_block_put_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat._bounded_queue import BoundedAudioQueue, DropPolicy
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk

    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    monkeypatch.setattr(
        observability,
        "_make_observation",
        lambda value, attributes: (value, attributes),
    )
    queue = BoundedAudioQueue(max_size=1, policy=DropPolicy.BLOCK, block_timeout=1.0)
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    assert await queue.put(chunk)

    async def free_space() -> None:
        await queue.get()

    task = asyncio.create_task(free_space())
    assert await queue.put(chunk)
    await task
    assert meter.gauges["easycat.queue.depth"].collect() == [(1, {"easycat.stage": "audio_queue"})]

    queue.close()
    assert meter.gauges["easycat.queue.depth"].collect() == [(0, {"easycat.stage": "audio_queue"})]


def test_metric_definitions_match_validation_reference() -> None:
    assert observability.METRIC_DEFINITIONS == {
        "easycat.turn.latency": "histogram",
        "easycat.stage.latency": "histogram",
        "easycat.journal.append.latency": "histogram",
        "easycat.sessions.active": "observable_gauge",
        "easycat.turns.total": "counter",
        "easycat.audio.bytes.total": "counter",
        "easycat.audio.frames.total": "counter",
        "easycat.provider.errors.total": "counter",
        "easycat.session.errors.total": "counter",
        "easycat.transport.disconnects.total": "counter",
        "easycat.validation.failures.total": "counter",
        "easycat.queue.depth": "observable_gauge",
        "easycat.queue.dropped.total": "counter",
        "easycat.event_loop.lag": "histogram",
        "easycat.journal.degraded": "observable_gauge",
        "easycat.interruption.total": "counter",
        "easycat.interruption.cutoff_latency": "histogram",
        "easycat.server.requests.total": "counter",
        "easycat.server.request.duration": "histogram",
        "easycat.server.sessions.rejected.total": "counter",
        "easycat.server.connections.active": "observable_gauge",
        "easycat.server.draining": "observable_gauge",
    }


@pytest.mark.asyncio
async def test_interruption_emits_counter_with_surface_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.events import EventBus, Interruption
    from easycat.runtime import InMemoryRingBuffer
    from easycat.session._journal_sink import SessionJournalSink

    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_tracer", lambda: None)
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    bus = EventBus()
    sink = SessionJournalSink(
        event_bus=bus,
        journal=InMemoryRingBuffer(),
        artifact_store=None,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    sink.subscribe()

    # turn_id must NOT leak onto the metric — it is a forbidden attribute key.
    await bus.emit(Interruption(session_id="event-session", turn_id="t1"))

    assert meter.counters["easycat.interruption.total"].adds == [(1, {"easycat.surface": "vad"})]


@pytest.mark.asyncio
async def test_transport_send_span_and_audio_counters_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.stages.transport import TransportStage

    class _StubTransport:
        async def send_audio(self, chunk):  # noqa: ANN001
            return True

    tracer = _FakeTracer()
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    stage = TransportStage(_StubTransport())
    payload = b"\x01\x02\x03\x04"
    delivered = await stage.execute(payload, _make_run_ctx(), _make_turn_ctx())
    assert delivered is True

    names = [name for name, _attrs in tracer.started]
    assert "easycat.transport.send" in names
    send_attrs = next(attrs for name, attrs in tracer.started if name == "easycat.transport.send")
    assert send_attrs.get("easycat.surface") == "tts"
    assert send_attrs.get("easycat.stage") == "transport"

    bytes_counter = meter.counters["easycat.audio.bytes.total"]
    assert bytes_counter.adds == [(len(payload), {"easycat.surface": "tts"})]
    frames_counter = meter.counters["easycat.audio.frames.total"]
    assert frames_counter.adds == [(1, {"easycat.surface": "tts"})]


@pytest.mark.asyncio
async def test_transport_send_error_increments_provider_errors_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.stages.transport import TransportStage

    class _BrokenTransport:
        async def send_audio(self, chunk):  # noqa: ANN001
            raise RuntimeError("boom")

    tracer = _FakeTracer()
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    stage = TransportStage(_BrokenTransport())
    with pytest.raises(RuntimeError, match="boom"):
        await stage.execute(b"\x00\x00", _make_run_ctx(), _make_turn_ctx())

    err_counter = meter.counters["easycat.provider.errors.total"]
    assert err_counter.adds == [
        (
            1,
            {
                "easycat.surface": "tts",
                "easycat.provider": "_brokentransport",
                "easycat.error_type": "RuntimeError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_vad_detect_span_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.stages.vad import VADStage

    class _StubVAD:
        async def process(self, chunk):  # noqa: ANN001
            if False:
                yield None
            return

    tracer = _FakeTracer()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: _FakeMeter())

    stage = VADStage(_StubVAD())
    chunk = AudioChunk(data=b"\x00\x00\x00\x00", format=PCM16_MONO_16K)
    await stage.execute(chunk, _make_run_ctx(), _make_turn_ctx())

    names = [name for name, _attrs in tracer.started]
    assert "easycat.vad.detect" in names
    attrs = next(a for name, a in tracer.started if name == "easycat.vad.detect")
    assert attrs.get("easycat.surface") == "stt"
    assert attrs.get("easycat.stage") == "vad"


@pytest.mark.asyncio
async def test_vad_error_increments_provider_errors_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.stages.vad import VADStage

    class _BrokenVAD:
        async def process(self, chunk):  # noqa: ANN001
            raise ValueError("vad-bad")
            if False:
                yield None

    monkeypatch.setattr(observability, "_get_tracer", lambda: _FakeTracer())
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    stage = VADStage(_BrokenVAD())
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    with pytest.raises(ValueError, match="vad-bad"):
        await stage.execute(chunk, _make_run_ctx(), _make_turn_ctx())

    err_counter = meter.counters["easycat.provider.errors.total"]
    assert err_counter.adds == [
        (
            1,
            {
                "easycat.surface": "stt",
                "easycat.provider": "_brokenvad",
                "easycat.error_type": "ValueError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_audio_stage_attributes_error_to_failed_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AudioStage must attribute a failure to the in-flight component.

    The echo canceller runs before the noise reducer; when it raises, the
    ``easycat.provider`` counter attribute must name the echo canceller
    (not ``self._provider``, the noise reducer). This locks the
    ``record_stage_failure(provider=error_provider)`` threading after the
    stage-wrapper consolidation.
    """
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.stages.audio import AudioStage

    class _StubNR:
        async def process(self, chunk):  # noqa: ANN001
            return chunk

    class _BrokenEcho:
        async def process(self, chunk):  # noqa: ANN001
            raise ValueError("aec-bad")

    monkeypatch.setattr(observability, "_get_tracer", lambda: _FakeTracer())
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    stage = AudioStage(_StubNR(), echo_canceller=_BrokenEcho())
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    with pytest.raises(ValueError, match="aec-bad"):
        await stage.execute(chunk, _make_run_ctx(), _make_turn_ctx())

    err_counter = meter.counters["easycat.provider.errors.total"]
    assert err_counter.adds == [
        (
            1,
            {
                "easycat.surface": "stt",
                "easycat.provider": "_brokenecho",
                "easycat.error_type": "ValueError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_stt_error_increments_provider_errors_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.stages.stt import STTStage

    class _BrokenSTT:
        async def send_audio(self, chunk):  # noqa: ANN001
            raise RuntimeError("stt-down")

    monkeypatch.setattr(observability, "_get_tracer", lambda: _FakeTracer())
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    stage = STTStage(_BrokenSTT())
    with pytest.raises(RuntimeError, match="stt-down"):
        await stage.execute(b"\x00\x00", _make_run_ctx(), _make_turn_ctx())

    err_counter = meter.counters["easycat.provider.errors.total"]
    assert err_counter.adds == [
        (
            1,
            {
                "easycat.surface": "stt",
                "easycat.provider": "_brokenstt",
                "easycat.error_type": "RuntimeError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_agent_error_increments_provider_errors_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.stages.agent import AgentStage

    class _BrokenAgent:
        async def run(self, text):  # noqa: ANN001
            raise RuntimeError("agent-down")

    monkeypatch.setattr(observability, "_get_tracer", lambda: _FakeTracer())
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    stage = AgentStage(_BrokenAgent())
    with pytest.raises(RuntimeError, match="agent-down"):
        await stage.execute("hi", _make_run_ctx(), _make_turn_ctx())

    err_counter = meter.counters["easycat.provider.errors.total"]
    # AgentStage wraps providers with AgentRunner internally; the provider
    # attribute is the inner class type, lowercased.
    assert err_counter.adds, "expected provider.errors.total to be incremented"
    add_value, add_attrs = err_counter.adds[0]
    assert add_value == 1
    assert add_attrs["easycat.surface"] == "agent_bridge"
    assert add_attrs["easycat.error_type"] == "RuntimeError"
    assert "easycat.provider" in add_attrs


@pytest.mark.asyncio
async def test_transport_receive_span_and_audio_counters_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audio router wraps inbound chunks in a transport.receive span."""
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk

    tracer = _FakeTracer()
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)

    chunk = AudioChunk(data=b"\x00\x00\x00\x00", format=PCM16_MONO_16K)
    # Simulate the wrap directly: the router code is too entangled with
    # transport/turn-manager state to drive end-to-end here.  We instead
    # exercise the exact emission pattern the router uses.
    with observability.span("easycat.transport.receive", {"easycat.surface": "stt"}):
        observability.increment_counter(
            "easycat.audio.bytes.total",
            value=len(chunk.data),
            attributes={"easycat.surface": "stt"},
        )
        observability.increment_counter(
            "easycat.audio.frames.total",
            attributes={"easycat.surface": "stt"},
        )

    assert ("easycat.transport.receive", {"easycat.surface": "stt"}) in tracer.started
    assert meter.counters["easycat.audio.bytes.total"].adds == [(4, {"easycat.surface": "stt"})]
    assert meter.counters["easycat.audio.frames.total"].adds == [(1, {"easycat.surface": "stt"})]


@pytest.mark.asyncio
async def test_audio_router_source_has_transport_receive_wiring() -> None:
    """The audio router source emits the transport.receive span around
    the inbound chunk-processing block.

    This is a structural assertion: end-to-end wiring through
    ``AudioRouter`` is exercised by the broader integration suite; here
    we just guard the literal source-level invariant so a refactor that
    drops the span gets caught.
    """
    import inspect

    from easycat.session import _audio_router

    # The inbound receive loop is split across ``_run_pipeline`` (the
    # transport iteration) and ``_process_chunk`` (per-chunk handling), so
    # inspect both. Match on the span name rather than exact indentation so
    # a reformat of the ``with`` block doesn't spuriously fail this guard.
    src = inspect.getsource(_audio_router.AudioRouter._run_pipeline) + inspect.getsource(
        _audio_router.AudioRouter._process_chunk
    )
    assert "observability.span(" in src
    assert '"easycat.transport.receive"' in src
    assert '"easycat.audio.bytes.total"' in src
    assert '"easycat.audio.frames.total"' in src


@pytest.mark.asyncio
async def test_turn_commit_span_emits_on_text_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from easycat import create_text_session

    class Agent:
        async def run(self, text: str) -> str:
            return f"echo: {text}"

    tracer = _FakeTracer()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: None)
    session = create_text_session(agent=Agent(), debug="off")
    await session.send_text("hi")
    names = [name for name, _attrs in tracer.started]
    assert "easycat.turn.commit" in names


@pytest.mark.asyncio
async def test_agent_tool_span_emits_on_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that emits a tool_started event should open agent.tool span."""
    from easycat import create_text_session
    from easycat.integrations.agents.base import AgentBridgeEvent
    from tests._bridge_helpers import _TestBridgeBase

    class _ToolBridge(_TestBridgeBase):
        """Minimal bridge that emits a tool_started/result followed by done."""

        async def invoke(self, turn_input, recorder, cancel_token=None):  # noqa: ANN001
            yield AgentBridgeEvent(kind="tool_started", tool_name="calc", call_id="c1")
            yield AgentBridgeEvent(kind="tool_result", call_id="c1", result="42")
            yield AgentBridgeEvent(kind="done", text="answer: 42")

    tracer = _FakeTracer()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)
    monkeypatch.setattr(observability, "_get_meter", lambda: None)
    session = create_text_session(agent=_ToolBridge(), debug="off", wrap_agent=False)
    result = await session.send_text("compute")
    assert "answer" in result
    names = [name for name, _attrs in tracer.started]
    assert "easycat.agent.tool" in names


@pytest.mark.asyncio
async def test_session_errors_counter_increments_on_dispatch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat import create_text_session

    class _BrokenAgent:
        async def run(self, text: str) -> str:
            raise RuntimeError("dispatch-broke")

    monkeypatch.setattr(observability, "_get_tracer", lambda: _FakeTracer())
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    session = create_text_session(agent=_BrokenAgent(), debug="off")
    with pytest.raises(RuntimeError, match="dispatch-broke") as exc_info:
        await session.send_text("hi")

    notes = exc_info.value.__notes__
    assert "stage=agent" in notes
    assert any(note.startswith("elapsed_ms=") for note in notes)
    assert any(note.startswith("session_id=") for note in notes)
    assert any(note.startswith("turn_id=turn-") for note in notes)
    err_counter = meter.counters.get("easycat.session.errors.total")
    assert err_counter is not None
    assert err_counter.adds, "expected session.errors.total to be incremented"
    _value, attrs = err_counter.adds[0]
    assert attrs["easycat.surface"] == "agent_bridge"
    assert attrs["easycat.error_type"] == "RuntimeError"
