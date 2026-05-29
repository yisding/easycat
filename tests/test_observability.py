from __future__ import annotations

import asyncio

import pytest

from easycat import _observability as observability


class _FakeSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class _FakeTracer:
    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, object]]] = []

    def start_as_current_span(self, name: str, attributes: dict[str, object]):
        self.started.append((name, attributes))
        return _FakeSpan()


class _FakeCounter:
    def __init__(self) -> None:
        self.adds: list[tuple[int | float, dict[str, object]]] = []

    def add(self, value: int | float, attributes: dict[str, object]) -> None:
        self.adds.append((value, attributes))


class _FakeHistogram:
    def __init__(self) -> None:
        self.records: list[tuple[int | float, dict[str, object]]] = []

    def record(self, value: int | float, attributes: dict[str, object]) -> None:
        self.records.append((value, attributes))


class _FakeObservableGauge:
    def __init__(self, callbacks: list[object]) -> None:
        self._callbacks = callbacks

    def collect(self) -> list[tuple[int | float, dict[str, object]]]:
        observations: list[tuple[int | float, dict[str, object]]] = []
        for callback in self._callbacks:
            observations.extend(callback(None))
        return observations


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}
        self.histograms: dict[str, _FakeHistogram] = {}
        self.gauges: dict[str, _FakeObservableGauge] = {}

    def create_counter(self, name: str) -> _FakeCounter:
        counter = _FakeCounter()
        self.counters[name] = counter
        return counter

    def create_histogram(self, name: str) -> _FakeHistogram:
        histogram = _FakeHistogram()
        self.histograms[name] = histogram
        return histogram

    def create_observable_gauge(
        self,
        name: str,
        callbacks: list[object] | None = None,
    ) -> _FakeObservableGauge:
        gauge = _FakeObservableGauge(callbacks or [])
        self.gauges[name] = gauge
        return gauge


@pytest.fixture(autouse=True)
def reset_observability_state() -> None:
    observability._COUNTERS.clear()
    observability._HISTOGRAMS.clear()
    observability._GAUGES.clear()
    observability._GAUGE_VALUES.clear()
    with observability._ACTIVE_SESSIONS_LOCK:
        observability._ACTIVE_SESSIONS = 0


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


def test_span_uses_configured_tracer_with_sanitized_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr(observability, "_get_tracer", lambda: tracer)

    with observability.span(
        "easycat.agent.invoke",
        {"easycat.provider_family": "openai", "gen_ai.request.model": "gpt-test"},
    ):
        pass

    assert tracer.started == [
        (
            "easycat.agent.invoke",
            {"easycat.provider_family": "openai", "gen_ai.request.model": "gpt-test"},
        )
    ]


def test_metrics_use_configured_meter_with_low_cardinality_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    monkeypatch.setattr(
        observability,
        "_make_observation",
        lambda value, attributes: (value, attributes),
    )

    observability.record_histogram(
        "easycat.stage.latency",
        0.25,
        {"easycat.stage": "agent", "easycat.result": "pass"},
    )
    observability.increment_counter(
        "easycat.turns.total",
        attributes={"easycat.feature_set": "default"},
    )
    observability.observe_gauge(
        "easycat.queue.depth",
        2,
        {"easycat.stage": "transport"},
    )

    assert meter.histograms["easycat.stage.latency"].records == [
        (0.25, {"easycat.stage": "agent", "easycat.result": "pass"})
    ]
    assert meter.counters["easycat.turns.total"].adds == [(1, {"easycat.feature_set": "default"})]
    assert meter.gauges["easycat.queue.depth"].collect() == [(2, {"easycat.stage": "transport"})]


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
    }


@pytest.mark.parametrize(
    "forbidden",
    ["session_id", "turn_id", "transcript", "provider_request_id", "phone_number"],
)
def test_forbidden_observability_attributes_fail(forbidden: str) -> None:
    with pytest.raises(ValueError, match=f"forbidden observability attribute: {forbidden}"):
        observability.sanitize_attributes({forbidden: "secret"})


def test_metric_attributes_reject_span_only_genai_keys() -> None:
    with pytest.raises(ValueError, match="unsupported observability attribute: gen_ai.system"):
        observability.record_histogram(
            "easycat.stage.latency",
            0.1,
            {"gen_ai.system": "openai"},
        )


# ──────────────────────────────────────────────────────────────────
# N4+N5: spans/counters wired into the pipeline stages.
# ──────────────────────────────────────────────────────────────────


def _make_run_ctx() -> object:
    from easycat.runtime.context import RunContext

    return RunContext(run_id="r1", session_id="s1", runtime_mode="chained_pipeline")


def _make_turn_ctx() -> object:
    from easycat.cancel import CancelToken
    from easycat.session._turn_context import TurnContext

    return TurnContext(turn_id="turn-1", cancel_token=CancelToken())


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
    with pytest.raises(RuntimeError, match="dispatch-broke"):
        await session.send_text("hi")

    err_counter = meter.counters.get("easycat.session.errors.total")
    assert err_counter is not None
    assert err_counter.adds, "expected session.errors.total to be incremented"
    _value, attrs = err_counter.adds[0]
    assert attrs["easycat.surface"] == "agent_bridge"
    assert attrs["easycat.error_type"] == "RuntimeError"
