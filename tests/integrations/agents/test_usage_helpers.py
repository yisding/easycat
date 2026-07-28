from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from easycat.integrations.agents._helpers import record_usage_from_result


class _UsageRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_usage(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_usage_extraction_continues_after_malformed_accessor() -> None:
    class Result:
        @property
        def usage(self) -> Any:
            raise RuntimeError("broken accessor")

        context_wrapper = SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=2))

    recorder = _UsageRecorder()
    await record_usage_from_result(
        recorder,  # type: ignore[arg-type]
        Result(),
        provider="test",
        model="model",
    )

    assert recorder.calls[0]["input_tokens"] == 5
    assert recorder.calls[0]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_usage_extraction_does_not_await_provider_accessor() -> None:
    class Result:
        async def usage(self) -> dict[str, int]:
            raise AssertionError("must not be awaited")

        ctx = SimpleNamespace(usage={"input_tokens": 4})

    recorder = _UsageRecorder()
    await record_usage_from_result(
        recorder,  # type: ignore[arg-type]
        Result(),
        provider="test",
        model=None,
    )

    assert recorder.calls[0]["input_tokens"] == 4


@pytest.mark.asyncio
async def test_usage_extraction_omits_zero_and_invalid_cached_counts() -> None:
    zero_recorder = _UsageRecorder()
    await record_usage_from_result(
        zero_recorder,  # type: ignore[arg-type]
        SimpleNamespace(usage={"input_tokens": 0, "output_tokens": 0}),
        provider="test",
        model=None,
    )
    assert zero_recorder.calls == []

    recorder = _UsageRecorder()
    await record_usage_from_result(
        recorder,  # type: ignore[arg-type]
        SimpleNamespace(usage={"input_tokens": 2, "cached_input_tokens": 3}),
        provider="test",
        model=None,
    )
    assert recorder.calls[0]["cached_input_tokens"] is None
