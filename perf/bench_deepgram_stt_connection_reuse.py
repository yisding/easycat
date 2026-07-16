"""Benchmark Deepgram STT connection setup on the turn-critical path.

The deterministic harness models an 80 ms DNS/TCP/TLS/WebSocket handshake.
It performs no network calls and sends no provider-billed audio.

Run with::

    uv run python perf/bench_deepgram_stt_connection_reuse.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import STTEventType
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig


def _final_result() -> str:
    return json.dumps(
        {
            "type": "Results",
            "channel": {
                "alternatives": [{"transcript": "benchmark transcript", "confidence": 1.0}]
            },
            "is_final": True,
            "from_finalize": True,
        }
    )


class _Socket:
    _STOP = object()

    def __init__(self) -> None:
        self.close_code: int | None = None
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()

    async def send(self, data: bytes | str) -> None:
        if not isinstance(data, str):
            return
        message = json.loads(data)
        if message.get("type") in {"Finalize", "CloseStream"}:
            await self._queue.put(_final_result())
        if message.get("type") == "CloseStream":
            await self._queue.put(self._STOP)

    async def close(self) -> None:
        if self.close_code is not None:
            return
        self.close_code = 1000
        await self._queue.put(self._STOP)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self._queue.get()
        if message is self._STOP:
            raise StopAsyncIteration
        assert isinstance(message, str)
        return message


class _SocketFactory:
    def __init__(self, connect_delay_s: float) -> None:
        self._connect_delay_s = connect_delay_s

    async def __call__(self, _url: str, **_kwargs: Any) -> _Socket:
        await asyncio.sleep(self._connect_delay_s)
        return _Socket()


async def _turn_to_final_ms(provider: DeepgramSTT) -> float:
    first_final_ms: float | None = None
    started = time.perf_counter()

    async def collect() -> None:
        nonlocal first_final_ms
        async for event in provider.events():
            if event.type == STTEventType.FINAL and first_final_ms is None:
                first_final_ms = (time.perf_counter() - started) * 1000.0

    await provider.start_stream()
    collector = asyncio.create_task(collect())
    await provider.send_audio(AudioChunk(data=b"\x00\x00" * 320, format=PCM16_MONO_16K))
    await provider.end_stream()
    await collector
    if first_final_ms is None:
        raise RuntimeError("benchmark provider emitted no final transcript")
    return first_final_ms


async def _samples(samples: int, connect_delay_s: float) -> tuple[list[float], list[float]]:
    one_shot = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="benchmark",
            persistent_ws=False,
            ws_connect=_SocketFactory(connect_delay_s),
        )
    )
    persistent = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="benchmark",
            ws_connect=_SocketFactory(connect_delay_s),
        )
    )
    await persistent.warmup()

    baseline: list[float] = []
    optimized: list[float] = []
    try:
        for _ in range(samples):
            baseline.append(await _turn_to_final_ms(one_shot))
            optimized.append(await _turn_to_final_ms(persistent))
    finally:
        await one_shot.aclose()
        await persistent.aclose()
    return baseline, optimized


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--connect-delay-ms", type=float, default=80.0)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    if args.connect_delay_ms < 0:
        parser.error("--connect-delay-ms must be non-negative")

    baseline, optimized = await _samples(args.samples, args.connect_delay_ms / 1000.0)
    baseline_p50 = statistics.median(baseline)
    optimized_p50 = statistics.median(optimized)
    print(f"samples={args.samples} modeled_handshake={args.connect_delay_ms:.1f}ms")
    print(f"one-shot:  p50={baseline_p50:.2f}ms p90={_percentile(baseline, 0.9):.2f}ms")
    print(f"persistent: p50={optimized_p50:.2f}ms p90={_percentile(optimized, 0.9):.2f}ms")
    print(f"turn-path savings: {baseline_p50 - optimized_p50:.2f}ms")


if __name__ == "__main__":
    asyncio.run(main())
