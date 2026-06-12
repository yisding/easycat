from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from easycat.validation.latency import (
    LatencyMode,
    LatencySample,
    LatencyStageDurations,
    build_latency_artifact,
)
from easycat.validation.runner import CommandResult


def _latency_artifact_for_comparison(
    *,
    total_ms: float,
    count: int = 3,
    condition_id: str = "baseline",
    provider: dict[str, str] | None = None,
    model: dict[str, str] | None = None,
    transport: dict[str, str] | None = None,
    debug: dict[str, str] | None = None,
) -> dict[str, object]:
    samples = [
        LatencySample(
            sample_id=f"{condition_id}-{index}",
            condition_id=condition_id,
            warmup=False,
            timestamp_source="time.monotonic",
            provider=provider or {"stt": "openai-realtime", "region": "us-east-1"},
            model=model or {"llm": "gpt-5.4", "tts": "gpt-4o-mini-tts"},
            transport=transport or {"kind": "websocket"},
            debug=debug or {"journal": "off"},
            stages=LatencyStageDurations(total_ms=total_ms),
        )
        for index in range(count)
    ]
    return build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
        baseline={
            "comparison": "baseline",
            "conditions": {condition_id: {"version": "2026-05-22"}},
        },
    )


def _baseline_aware_command_runner(total_ms: float):
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        samples_path = Path(env["EASYCAT_LATENCY_SAMPLES_PATH"])
        samples = [
            LatencySample(
                sample_id=f"baseline-{index}",
                condition_id="baseline",
                warmup=False,
                timestamp_source="time.monotonic",
                provider={"stt": "openai-realtime", "region": "us-east-1"},
                model={"llm": "gpt-5.4", "tts": "gpt-4o-mini-tts"},
                transport={"kind": "websocket"},
                debug={"journal": "off"},
                stages=LatencyStageDurations(total_ms=total_ms),
            ).to_dict()
            for index in range(3)
        ]
        samples_path.write_text(json.dumps(samples))
        return CommandResult(exit_code=0, stdout="", stderr="")

    return fake_command_runner
