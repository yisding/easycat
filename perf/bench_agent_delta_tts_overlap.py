#!/usr/bin/env python3
"""Benchmark first-TTS admission during asynchronous AgentDelta handlers.

The benchmark drives a real Session with deterministic agent/TTS doubles. Its
serial branch reinstates the old emit-then-admit ordering; its overlap branch
uses the production implementation. Provider start time is measured directly,
isolating framework-owned observer serialization from provider TTFB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import AgentDelta, STTEvent, STTEventType, TTSEvent, TTSEventType
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.session._session import Session
from easycat.session._streaming import _AgentStreamConsumer
from easycat.session._types import SessionConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig


class _Transport:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        if False:
            yield AudioChunk(data=b"", format=PCM16_MONO_16K)

    async def send_audio(self, chunk: AudioChunk) -> bool:
        _ = chunk
        return True

    async def clear_audio(self) -> None: ...


class _VAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[object]:
        _ = chunk
        if False:
            yield None

    def configure(self, **kwargs: object) -> None:
        _ = kwargs


class _STT:
    async def start_stream(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None:
        _ = chunk

    async def end_stream(self) -> None: ...

    async def events(self) -> AsyncIterator[STTEvent]:
        if False:
            yield STTEvent(type=STTEventType.PARTIAL, text="")


class _NoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


class _Agent:
    async def run(self, text: str) -> str:
        _ = text
        return "Reply."


class _TimedTTS:
    def __init__(self) -> None:
        self.started_at: float | None = None

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        _ = payload
        self.started_at = time.perf_counter()
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\x00" * 320, format=PCM16_MONO_16K),
        )

    async def cancel(self) -> None: ...
    async def stop(self) -> None: ...


async def _serial_consume_text_update(self: _AgentStreamConsumer, event: Any) -> None:
    """Reinstate the pre-optimization ordering for the benchmark baseline."""
    update = self._text_stream.apply(event)
    if update is None:
        return
    self.result.text = update.text
    if update.text == update.previous_text:
        return
    await self._emit(
        AgentDelta(
            text=event.text,
            part_index=update.part_index,
            replacement=update.operation == "replace",
        )
    )
    if self._turn.first_agent_time is None:
        self._turn.first_agent_time = time.monotonic()
    queued = await self._queue_text_update(update)
    self._resolve_first_tts_payload_gate(queued)


async def _measure_once(*, handler_ms: float, overlap: bool) -> float:
    tts = _TimedTTS()
    session = Session(
        SessionConfig(
            transport=_Transport(),
            vad=_VAD(),
            stt=_STT(),
            agent=AgentRunner(_Agent()),
            tts=tts,
            noise_reducer=_NoiseReducer(),
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
        )
    )

    async def _slow_delta_handler(_event: AgentDelta) -> None:
        await asyncio.sleep(handler_ms / 1000.0)

    session.event_bus.subscribe(AgentDelta, _slow_delta_handler)
    session._turn = TurnContext("agent-delta-benchmark", CancelToken())

    original = _AgentStreamConsumer._consume_text_update
    if not overlap:
        _AgentStreamConsumer._consume_text_update = _serial_consume_text_update
    started = time.perf_counter()
    try:
        await session._turn_runner.run_streaming_agent("hello", token=None)
    finally:
        _AgentStreamConsumer._consume_text_update = original

    assert tts.started_at is not None
    return (tts.started_at - started) * 1000.0


async def compare(*, handler_ms: float = 80.0, iterations: int = 5) -> dict[str, Any]:
    """Measure serial and overlapped p50 time to provider start."""
    if handler_ms < 0:
        raise ValueError("handler_ms must be non-negative")
    if iterations < 1:
        raise ValueError("iterations must be positive")

    await _measure_once(handler_ms=handler_ms, overlap=False)
    await _measure_once(handler_ms=handler_ms, overlap=True)
    serial: list[float] = []
    overlapped: list[float] = []
    # Alternate the order so changing runner contention cannot systematically
    # bias one mode. This matters in the minimum-dependency CI job, where the
    # benchmark shares a machine with several xdist workers.
    for iteration in range(iterations):
        modes = (False, True) if iteration % 2 == 0 else (True, False)
        for overlap in modes:
            sample = await _measure_once(handler_ms=handler_ms, overlap=overlap)
            (overlapped if overlap else serial).append(sample)
    serial_p50 = statistics.median(serial)
    overlapped_p50 = statistics.median(overlapped)
    saved_ms = serial_p50 - overlapped_p50
    return {
        "schema_version": 1,
        "iterations": iterations,
        "warmup_runs_per_mode": 1,
        "handler_ms": handler_ms,
        "serial": {"samples_ms": serial, "p50_ms": serial_p50},
        "overlapped": {"samples_ms": overlapped, "p50_ms": overlapped_p50},
        "saved_p50_ms": saved_ms,
        "reduction_percent": saved_ms / serial_p50 * 100.0 if serial_p50 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handler-ms", type=float, default=80.0)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(compare(handler_ms=args.handler_ms, iterations=args.iterations))
    except ValueError as exc:
        parser.error(str(exc))

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
