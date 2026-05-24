from __future__ import annotations

import asyncio
import time

import pytest

from easycat.validation.latency import (
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
    build_latency_artifact,
)

# ---------------------------------------------------------------------------
# 1. EventLoopLagSampler is public and importable
# ---------------------------------------------------------------------------


def test_event_loop_lag_sampler_is_importable_from_validation() -> None:
    from easycat.validation import EventLoopLagSampler  # noqa: F401


async def test_event_loop_lag_sampler_measures_blocking_call() -> None:
    from easycat.validation import EventLoopLagSampler

    sampler = EventLoopLagSampler()
    await sampler.start()
    # Yield once so the sampler task can take its first tick.
    await asyncio.sleep(0.02)
    time.sleep(0.05)
    lag_ms = await sampler.stop()
    assert lag_ms is not None
    assert lag_ms >= 30.0
    assert lag_ms < 500.0


async def test_event_loop_lag_sampler_returns_none_when_not_started() -> None:
    from easycat.validation import EventLoopLagSampler

    sampler = EventLoopLagSampler()
    result = await sampler.stop()
    assert result is None


# ---------------------------------------------------------------------------
# 2. MemoryGrowthSampler is public and importable
# ---------------------------------------------------------------------------


def test_memory_growth_sampler_is_importable_from_validation() -> None:
    from easycat.validation import MemoryGrowthSampler  # noqa: F401


def test_memory_growth_sampler_returns_kib_delta() -> None:
    from easycat.validation import MemoryGrowthSampler

    sampler = MemoryGrowthSampler()
    sampler.start()
    _hold = bytearray(2 * 1024 * 1024)
    assert len(_hold) == 2 * 1024 * 1024  # keep allocation live until after stop
    growth_kib = sampler.stop()
    assert growth_kib is None or growth_kib >= 0


def test_memory_growth_sampler_returns_none_when_not_started() -> None:
    from easycat.validation import MemoryGrowthSampler

    sampler = MemoryGrowthSampler()
    assert sampler.stop() is None


# ---------------------------------------------------------------------------
# 3. capture_reliability_sample policy helper
# ---------------------------------------------------------------------------


def test_capture_reliability_sample_smoke_is_informational_not_eligible() -> None:
    from easycat.validation import capture_reliability_sample

    sample = capture_reliability_sample(
        sample_id="s1",
        condition_id="c1",
        mode=LatencyMode.SMOKE,
        event_loop_lag_ms=12.5,
    )
    assert isinstance(sample, ReliabilitySample)
    assert sample.mode == "smoke"
    assert sample.informational is True
    assert sample.eligible is False


def test_capture_reliability_sample_sweep_is_eligible_not_informational() -> None:
    from easycat.validation import capture_reliability_sample

    sample = capture_reliability_sample(
        sample_id="s2",
        condition_id="c2",
        mode=LatencyMode.SWEEP,
        event_loop_lag_ms=12.5,
    )
    assert sample.mode == "sweep"
    assert sample.informational is False
    assert sample.eligible is True


def test_capture_reliability_sample_stress_mode_is_eligible_not_informational() -> None:
    from easycat.validation import capture_reliability_sample

    sample = capture_reliability_sample(
        sample_id="s3",
        condition_id="c3",
        mode="stress",
        event_loop_lag_ms=12.5,
    )
    assert sample.mode == "stress"
    assert sample.informational is False
    assert sample.eligible is True


def test_capture_reliability_sample_all_none_sets_unavailable_reason() -> None:
    from easycat.validation import capture_reliability_sample

    sample = capture_reliability_sample(
        sample_id="s4",
        condition_id="c4",
        mode=LatencyMode.SMOKE,
        event_loop_lag_ms=None,
        queue_depth=None,
        dropped_frames=None,
        journal_degraded=None,
        active_sessions=None,
        memory_growth_kib=None,
    )
    assert isinstance(sample.signals, ReliabilitySignals)
    assert sample.signals.unavailable_reason
    assert isinstance(sample.signals.unavailable_reason, str)


def test_capture_reliability_sample_preserves_provided_signals() -> None:
    from easycat.validation import capture_reliability_sample

    sample = capture_reliability_sample(
        sample_id="s5",
        condition_id="c5",
        mode=LatencyMode.SMOKE,
        event_loop_lag_ms=12.5,
        journal_degraded=True,
    )
    assert sample.signals.event_loop_lag_ms == 12.5
    assert sample.signals.journal_degraded is True
    assert sample.signals.queue_depth is None
    assert sample.signals.dropped_frames is None
    assert sample.signals.active_sessions is None
    assert sample.signals.memory_growth_kib is None
    assert sample.signals.unavailable_reason is None


# ---------------------------------------------------------------------------
# 4. Latency artifact attaches reliability samples
# ---------------------------------------------------------------------------


def test_build_latency_artifact_passes_through_reliability_samples() -> None:
    latency_sample = LatencySample(
        sample_id="l1",
        condition_id="c1",
        warmup=False,
        timestamp_source="event_monotonic",
        stages=LatencyStageDurations(total_ms=500.0),
    )
    reliability_sample = ReliabilitySample(
        sample_id="s1",
        condition_id="c1",
        mode="smoke",
        informational=True,
        eligible=False,
        signals=ReliabilitySignals(event_loop_lag_ms=4.0),
    )
    artifact = build_latency_artifact(
        mode=LatencyMode.SMOKE,
        samples=[latency_sample],
        reliability_samples=[reliability_sample],
    )
    assert "reliability_samples" in artifact
    assert len(artifact["reliability_samples"]) == 1
    assert artifact["reliability_samples"][0]["sample_id"] == "s1"
    assert artifact["reliability_samples"][0]["condition_id"] == "c1"


# ---------------------------------------------------------------------------
# 5. Public re-exports
# ---------------------------------------------------------------------------


def test_validation_package_reexports_reliability_helpers() -> None:
    from easycat.validation import (  # noqa: F401
        EventLoopLagSampler,
        MemoryGrowthSampler,
        ReliabilitySample,
        ReliabilitySignals,
        capture_reliability_sample,
    )


@pytest.mark.parametrize(
    "name",
    [
        "EventLoopLagSampler",
        "MemoryGrowthSampler",
        "ReliabilitySample",
        "ReliabilitySignals",
        "capture_reliability_sample",
    ],
)
def test_validation_package_all_lists_reliability_helpers(name: str) -> None:
    import easycat.validation as validation_pkg

    assert name in validation_pkg.__all__
