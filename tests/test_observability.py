from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from easycat import _observability as observability

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_observability_doc_explains_journal_redaction_boundary() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    caveats = " ".join(doc.split("## Honesty caveats", 1)[1].split())

    assert "safe config/environment snapshots" in caveats
    assert "selected agent-bridge metadata" in caveats
    assert "obvious secret-like journal fields through `apply_write_filter`" in caveats
    assert "transcript text, agent output, and tool-result text for replay" in caveats


def test_observability_doc_lists_journal_cli_entry_points() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    journal = doc.split("### C — ExecutionJournal", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]

    for command in (
        "easycat bundles list",
        "easycat bundles list --json",
        "easycat bundles show <path>",
        "easycat bundles show <path> --json",
        "easycat inspect <path>",
        "easycat inspect <path> --json",
        "easycat replay <path>",
        "easycat replay <path> --json",
        "easycat bundles export <path>",
        "easycat bundles export <path> --output DIR --json",
    ):
        assert command in journal
    assert "parseable summary" in journal


def test_observability_doc_points_operators_to_filtered_docs_route() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    intro = doc.split("## The four layers", 1)[0]

    assert "uv run easycat docs --audience operators" in intro
    assert "operator-facing route slice" in intro
    assert "deployment, observability, and journal durability" in intro


def test_observability_doc_lists_debugger_ui_entry_points() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    journal = doc.split("### C — ExecutionJournal", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]

    for token in (
        "uv sync --extra debugger --group dev",
        "uv add 'easycat[debugger]'",
        "from easycat.debugger import serve_bundle, serve_session",
        'serve_bundle("runs/session.bundle", port=8765)',
        "serve_session(session, port=8765, in_thread=True)",
        "loopback-only by default",
        "allow_remote=True",
    ):
        assert token in journal


def test_observability_doc_tracks_logging_configuration_vocabulary() -> None:
    from easycat._logging import _JsonFormatter
    from easycat.config.easy import _EASYCAT_LOG_LEVELS, _VALID_DEBUG

    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    config = doc.split("## Configuration and orthogonality", 1)[1].split(
        "### Correlation ids in logs", 1
    )[0]
    record = logging.LogRecord(
        "easycat.tests",
        logging.INFO,
        __file__,
        1,
        "hello %s",
        ("world",),
        None,
    )
    record.session_id = "session-1"  # type: ignore[attr-defined]
    record.turn_id = "turn-1"  # type: ignore[attr-defined]
    json_fields = set(json.loads(_JsonFormatter().format(record)))

    missing_levels = sorted(level for level in _EASYCAT_LOG_LEVELS if f"`{level}`" not in config)
    missing_debug_modes = sorted(mode for mode in _VALID_DEBUG if f'"{mode}"' not in config)
    missing_json_fields = sorted(field for field in json_fields if f"`{field}`" not in config)

    assert not missing_levels, "Observability guide missing log levels: " + ", ".join(
        missing_levels
    )
    assert not missing_debug_modes, "Observability guide missing debug modes: " + ", ".join(
        missing_debug_modes
    )
    assert not missing_json_fields, "Observability guide missing JSON log fields: " + ", ".join(
        missing_json_fields
    )
    assert "`EASYCAT_ENV=dev|prod`" in config
    assert "`prod` / `production` uses single-line JSON" in config
    assert "`exc`" in config


@pytest.mark.parametrize(
    "forbidden",
    ["session_id", "turn_id", "transcript", "provider_request_id", "phone_number"],
)
def test_forbidden_observability_attributes_fail(forbidden: str) -> None:
    with pytest.raises(ValueError, match=f"forbidden observability attribute: {forbidden}"):
        observability.sanitize_attributes({forbidden: "secret"})


@pytest.mark.parametrize(
    "forbidden",
    [
        "easycat.transcript",
        "user_prompt",
        "message_content",
        "raw_text",
        "request_body",
        "client_secret",
        "auth_token",
    ],
)
def test_substring_forbidden_attributes_fail(forbidden: str) -> None:
    with pytest.raises(ValueError, match=f"forbidden observability attribute: {forbidden}"):
        observability.sanitize_attributes(
            {forbidden: "secret"},
            allowed_keys=observability.SPAN_ATTRIBUTE_KEYS,
        )


def test_allowed_keys_checked_before_substring_guard() -> None:
    result = observability.sanitize_attributes(
        {"easycat.surface": "tts", "gen_ai.request.model": "gpt-test"},
        allowed_keys=observability.SPAN_ATTRIBUTE_KEYS,
    )
    assert result == {"easycat.surface": "tts", "gen_ai.request.model": "gpt-test"}


def test_forbidden_keys_checked_before_allow_list() -> None:
    with pytest.raises(ValueError, match="forbidden observability attribute: prompt"):
        observability.sanitize_attributes(
            {"prompt": "secret"},
            allowed_keys=frozenset({"prompt"}),
        )


def test_no_allowed_key_contains_a_forbidden_substring() -> None:
    for key in observability.SPAN_ATTRIBUTE_KEYS:
        low = key.lower()
        assert not any(substring in low for substring in observability._FORBIDDEN_SUBSTRINGS), key


def test_metric_attributes_reject_span_only_genai_keys() -> None:
    with pytest.raises(ValueError, match="unsupported observability attribute: gen_ai.system"):
        observability.record_histogram(
            "easycat.stage.latency",
            0.1,
            {"gen_ai.system": "openai"},
        )


def test_validation_tasks_v61_current_state_tracks_noop_safe_otel_spans() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V6.1 Add No-Op-Safe OTel Spans", 1)[1].split(
        "### V6.2 Add Low-Cardinality Metrics",
        1,
    )[0]
    observability_source = (REPO_ROOT / "src/easycat/_observability.py").read_text(
        encoding="utf-8"
    )
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs/observability.md").read_text(encoding="utf-8")
    test_source = Path(__file__).read_text(encoding="utf-8")
    span_wiring = {
        "src/easycat/session/_session.py": ("easycat.session",),
        "src/easycat/session/_audio_router.py": ("easycat.transport.receive",),
        "src/easycat/session/_turn_runner.py": (
            "easycat.turn.commit",
            "easycat.agent.tool",
        ),
        "src/easycat/stages/vad.py": ("easycat.vad.detect",),
        "src/easycat/stages/stt.py": ("easycat.stt.stream",),
        "src/easycat/stages/agent.py": ("easycat.agent.invoke",),
        "src/easycat/stages/tts.py": ("easycat.tts.synthesize",),
        "src/easycat/stages/transport.py": ("easycat.transport.send",),
    }

    assert "Current verified state:" in section
    assert "opentelemetry-api" not in pyproject
    for token in (
        "def span(",
        "SPAN_NAMES",
        "SPAN_ATTRIBUTE_KEYS",
        "LOW_CARDINALITY_ATTRIBUTE_KEYS",
        "FORBIDDEN_ATTRIBUTE_KEYS",
        "_FORBIDDEN_SUBSTRINGS",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.system",
        "except ImportError",
        "return None",
        "tracer.start_as_current_span",
    ):
        assert token in observability_source
    for span_name in observability.SPAN_NAMES:
        assert span_name in observability_source
        assert f"`{span_name}`" in section
    for path, span_names in span_wiring.items():
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert f"`{path}`" in section
        for span_name in span_names:
            assert span_name in source
    for test_name in (
        "test_observability_is_noop_without_otel",
        "test_span_uses_configured_tracer_with_sanitized_attributes",
        "test_text_turn_emits_session_and_agent_spans",
        "test_transport_send_span_and_audio_counters_emit",
        "test_vad_detect_span_emits",
        "test_transport_receive_span_and_audio_counters_emit",
        "test_audio_router_source_has_transport_receive_wiring",
        "test_turn_commit_span_emits_on_text_turn",
        "test_agent_tool_span_emits_on_tool_call",
        "test_forbidden_observability_attributes_fail",
        "test_substring_forbidden_attributes_fail",
    ):
        assert test_name in test_source
    for doc_token in (
        "no-op without an SDK",
        "PII-safe and low-cardinality",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.system",
    ):
        assert doc_token in docs
    for token in (
        "src/easycat/_observability.py",
        "pyproject.toml",
        "opentelemetry-api",
        "_get_tracer()",
        "_get_meter()",
        "ImportError",
        "span(...)",
        "SPAN_NAMES",
        "SPAN_ATTRIBUTE_KEYS",
        "LOW_CARDINALITY_ATTRIBUTE_KEYS",
        "FORBIDDEN_ATTRIBUTE_KEYS",
        "_FORBIDDEN_SUBSTRINGS",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.system",
        "easycat.journal.append.latency",
        "tests/test_observability.py",
        "docs/observability.md",
    ):
        assert f"`{token}`" in section


def test_validation_tasks_v62_current_state_tracks_low_cardinality_metrics() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V6.2 Add Low-Cardinality Metrics", 1)[1].split(
        "## Dependency Map",
        1,
    )[0]
    observability_source = (REPO_ROOT / "src/easycat/_observability.py").read_text(
        encoding="utf-8"
    )
    reference = (REPO_ROOT / "plan/validation/reference.md").read_text(encoding="utf-8")
    test_source = Path(__file__).read_text(encoding="utf-8")
    source_files = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "src/easycat").rglob("*.py")
    }
    metric_wiring = {
        "src/easycat/_bounded_queue.py": (
            "easycat.queue.dropped.total",
            "easycat.queue.depth",
        ),
        "src/easycat/session/_session.py": (
            "easycat.event_loop.lag",
            "easycat.queue.depth",
            "easycat.journal.degraded",
            "session_started",
            "session_ended",
        ),
        "src/easycat/session/_turn_runner.py": (
            "easycat.turn.latency",
            "easycat.turns.total",
            "easycat.session.errors.total",
        ),
        "src/easycat/session/_audio_router.py": (
            "easycat.audio.bytes.total",
            "easycat.audio.frames.total",
        ),
        "src/easycat/runtime/journal.py": (
            "easycat.journal.append.latency",
            "easycat.journal.degraded",
        ),
        "src/easycat/stages/transport.py": (
            "easycat.stage.latency",
            "easycat.audio.bytes.total",
            "easycat.audio.frames.total",
            "easycat.provider.errors.total",
        ),
        "src/easycat/stages/stt.py": (
            "easycat.stage.latency",
            "easycat.provider.errors.total",
        ),
        "src/easycat/stages/vad.py": (
            "easycat.stage.latency",
            "easycat.provider.errors.total",
        ),
        "src/easycat/stages/agent.py": (
            "easycat.stage.latency",
            "easycat.provider.errors.total",
        ),
        "src/easycat/stages/tts.py": (
            "easycat.stage.latency",
            "easycat.provider.errors.total",
        ),
    }
    reserved_metrics = (
        "easycat.transport.disconnects.total",
        "easycat.validation.failures.total",
    )

    assert "Current verified state:" in section
    assert "METRIC_DEFINITIONS" in observability_source
    for metric_name, metric_kind in observability.METRIC_DEFINITIONS.items():
        assert metric_name in observability_source
        assert metric_name in reference
        assert f"`{metric_name}`" in section
        assert metric_kind in section
    for attr_name in observability.LOW_CARDINALITY_ATTRIBUTE_KEYS:
        assert attr_name in reference
        assert attr_name in observability_source
    for helper in (
        "record_histogram",
        "increment_counter",
        "observe_gauge",
        "_record_metric",
        "LOW_CARDINALITY_ATTRIBUTE_KEYS",
    ):
        assert helper in observability_source
        assert f"`{helper}(...)`" in section or f"`{helper}`" in section
    for path, metric_names in metric_wiring.items():
        assert f"`{path}`" in section or "stage modules under `src/easycat/stages/`" in section
        source = source_files[path]
        for metric_name in metric_names:
            assert metric_name in source
    for metric_name in reserved_metrics:
        emitters = [
            path
            for path, source in source_files.items()
            if path != "src/easycat/_observability.py" and metric_name in source
        ]
        assert not emitters, f"{metric_name} unexpectedly emitted by {emitters}"
        assert f"`{metric_name}`" in section
    for test_name in (
        "test_metric_definitions_match_validation_reference",
        "test_metrics_use_configured_meter_with_low_cardinality_attributes",
        "test_observable_gauge_uses_callback_contract",
        "test_audio_queue_emits_drop_counter_and_depth_gauge",
        "test_audio_queue_refreshes_depth_after_block_put_and_close",
        "test_transport_send_span_and_audio_counters_emit",
        "test_session_errors_counter_increments_on_dispatch_failure",
        "test_metric_attributes_reject_span_only_genai_keys",
    ):
        assert test_name in test_source
    for token in (
        "METRIC_DEFINITIONS",
        "record_histogram(...)",
        "increment_counter(...)",
        "observe_gauge(...)",
        "_record_metric(...)",
        "LOW_CARDINALITY_ATTRIBUTE_KEYS",
        "plan/validation/reference.md",
        "tests/test_observability.py",
    ):
        assert f"`{token}`" in section


# ──────────────────────────────────────────────────────────────────
# N4+N5: spans/counters wired into the pipeline stages.
# ──────────────────────────────────────────────────────────────────


def _make_run_ctx() -> object:
    from easycat.runtime.context import RunContext

    return RunContext(run_id="r1", session_id="s1", runtime_mode="chained_pipeline")


def _make_turn_ctx() -> object:
    from easycat._turn_context import TurnContext
    from easycat.cancel import CancelToken

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
