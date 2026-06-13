from __future__ import annotations

from tests.observability._observability_helpers import (
    REPO_ROOT,
    Path,
    _FakeMeter,
    _FakeTracer,
    observability,
    pytest,
)


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
        "tests/observability/test_attributes.py",
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
        "src/easycat/runtime/journal_memory.py": (
            "easycat.journal.append.latency",
            "easycat.journal.degraded",
        ),
        "src/easycat/runtime/journal_sql.py": (
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
        "src/easycat/session/_journal_sink.py": ("easycat.interruption.total",),
    }
    reserved_metrics = (
        "easycat.transport.disconnects.total",
        "easycat.validation.failures.total",
        "easycat.interruption.cutoff_latency",
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
        "tests/observability/test_attributes.py",
    ):
        assert f"`{token}`" in section
