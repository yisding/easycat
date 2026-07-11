from __future__ import annotations

import pytest

from perf.bench_tts_lifecycle_overlap import modeled_comparison


def test_tts_lifecycle_overlap_model_replaces_sum_with_maximum() -> None:
    assert modeled_comparison(handler_ms=80.0, provider_ms=120.0) == {
        "serial_ms": 200.0,
        "overlapped_ms": 120.0,
        "saved_ms": 80.0,
    }


@pytest.mark.parametrize(
    ("handler_ms", "provider_ms"),
    [(-1.0, 10.0), (10.0, -1.0)],
)
def test_tts_lifecycle_overlap_model_rejects_negative_delays(
    handler_ms: float,
    provider_ms: float,
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        modeled_comparison(handler_ms=handler_ms, provider_ms=provider_ms)
