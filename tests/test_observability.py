from __future__ import annotations

import asyncio

import pytest

from easycat import observability


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
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.bounded_queue import BoundedAudioQueue, DropPolicy

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
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.bounded_queue import BoundedAudioQueue, DropPolicy

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
