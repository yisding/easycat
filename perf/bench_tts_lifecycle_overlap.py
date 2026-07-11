#!/usr/bin/env python3
"""Benchmark serial versus overlapped bot-start lifecycle and TTS first byte.

The benchmark uses EasyCat's real EventBus, TurnManager, TTSSynthesizer, and
first-event barrier with deterministic delayed doubles.  It isolates the
framework-owned serialization that previously made lifecycle handler time and
provider TTFB additive.
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

from easycat._bounded_queue import BoundedAudioQueue
from easycat._tts_synthesizer import TTSSynthesizer
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import BotStartedSpeaking, EventBus, TTSEvent, TTSEventType
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManager


class _DelayedTTS:
    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        await asyncio.sleep(self._delay_s)
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\x00" * 320, format=PCM16_MONO_16K),
        )

    async def cancel(self) -> None:
        pass


def modeled_comparison(*, handler_ms: float, provider_ms: float) -> dict[str, float]:
    """Return the ideal serial/overlap timing relationship."""
    if handler_ms < 0 or provider_ms < 0:
        raise ValueError("handler_ms and provider_ms must be non-negative")
    serial_ms = handler_ms + provider_ms
    overlapped_ms = max(handler_ms, provider_ms)
    return {
        "serial_ms": serial_ms,
        "overlapped_ms": overlapped_ms,
        "saved_ms": serial_ms - overlapped_ms,
    }


async def _measure_once(*, handler_s: float, provider_s: float, overlap: bool) -> float:
    bus = EventBus()

    async def _lifecycle_handler(_event: BotStartedSpeaking) -> None:
        await asyncio.sleep(handler_s)

    bus.subscribe(BotStartedSpeaking, _lifecycle_handler)
    turn_manager = TurnManager(bus)
    synth = TTSSynthesizer(
        tts=_DelayedTTS(provider_s),
        event_bus=bus,
        outbound_queue=BoundedAudioQueue(max_size=8, name="benchmark"),
    )
    started = time.perf_counter()
    if overlap:
        barrier = asyncio.Event()
        task = asyncio.create_task(
            synth.synthesize(TTSInput("hello"), None, start_barrier=barrier)
        )
        await asyncio.sleep(0)
        await turn_manager.bot_started_speaking()
        barrier.set()
        await task
    else:
        await turn_manager.bot_started_speaking()
        await synth.synthesize(TTSInput("hello"), None)
    return (time.perf_counter() - started) * 1000.0


async def compare(
    *,
    handler_ms: float = 80.0,
    provider_ms: float = 120.0,
    iterations: int = 5,
) -> dict[str, Any]:
    """Measure p50 first-audio time for serial and overlapped execution."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    model = modeled_comparison(handler_ms=handler_ms, provider_ms=provider_ms)
    handler_s = handler_ms / 1000.0
    provider_s = provider_ms / 1000.0
    serial = [
        await _measure_once(handler_s=handler_s, provider_s=provider_s, overlap=False)
        for _ in range(iterations)
    ]
    overlapped = [
        await _measure_once(handler_s=handler_s, provider_s=provider_s, overlap=True)
        for _ in range(iterations)
    ]
    serial_p50 = statistics.median(serial)
    overlapped_p50 = statistics.median(overlapped)
    saved_ms = serial_p50 - overlapped_p50
    return {
        "schema_version": 1,
        "iterations": iterations,
        "handler_ms": handler_ms,
        "provider_ms": provider_ms,
        "modeled": model,
        "serial": {"samples_ms": serial, "p50_ms": serial_p50},
        "overlapped": {"samples_ms": overlapped, "p50_ms": overlapped_p50},
        "saved_p50_ms": saved_ms,
        "reduction_percent": saved_ms / serial_p50 * 100.0 if serial_p50 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handler-ms", type=float, default=80.0)
    parser.add_argument("--provider-ms", type=float, default=120.0)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(
            compare(
                handler_ms=args.handler_ms,
                provider_ms=args.provider_ms,
                iterations=args.iterations,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
