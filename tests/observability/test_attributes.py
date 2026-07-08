from __future__ import annotations

from tests.observability._observability_helpers import (
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
