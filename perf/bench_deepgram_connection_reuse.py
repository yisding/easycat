"""Benchmark Deepgram TTS connection setup on the reply-critical path.

This deterministic harness models an 80 ms DNS/TLS/WebSocket handshake and
compares the explicit one-shot path with EasyCat's warmed persistent socket.
It performs no network calls or billed synthesis.

Run with::

    uv run python perf/bench_deepgram_connection_reuse.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig


class _BenchmarkSocket:
    def __init__(self, *, connect_delay_s: float) -> None:
        self._connect_delay_s = connect_delay_s
        self._connected = False
        self._closed = False
        self._queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    async def connect(self) -> None:
        await asyncio.sleep(self._connect_delay_s)
        self._connected = True

    async def send(self, frame: str) -> None:
        if json.loads(frame)["type"] == "Flush":
            await self._queue.put(b"\x00\x00" * 240)
            await self._queue.put(json.dumps({"type": "Flushed"}))

    async def recv_iter(self):
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        self._closed = True
        self._connected = False
        await self._queue.put(None)


async def _first_audio_ms(provider: DeepgramTTS) -> float:
    started = time.perf_counter()
    first_audio_ms: float | None = None
    async for event in provider.synthesize("Latency benchmark."):
        if event.audio is not None and first_audio_ms is None:
            first_audio_ms = (time.perf_counter() - started) * 1000.0
    if first_audio_ms is None:
        raise RuntimeError("benchmark provider emitted no audio")
    return first_audio_ms


async def _run(samples: int, connect_delay_ms: float) -> tuple[list[float], list[float]]:
    one_shot = DeepgramTTS(DeepgramTTSConfig(api_key="benchmark", persistent_ws=False))
    one_shot._create_ws = lambda: _BenchmarkSocket(  # type: ignore[method-assign]
        connect_delay_s=connect_delay_ms / 1000.0
    )

    persistent = DeepgramTTS(DeepgramTTSConfig(api_key="benchmark"))
    socket = _BenchmarkSocket(connect_delay_s=connect_delay_ms / 1000.0)
    persistent._create_ws = lambda: socket  # type: ignore[method-assign]
    await persistent.warmup()

    one_shot_samples: list[float] = []
    persistent_samples: list[float] = []
    try:
        for _ in range(samples):
            one_shot_samples.append(await _first_audio_ms(one_shot))
            persistent_samples.append(await _first_audio_ms(persistent))
    finally:
        await one_shot.close()
        await persistent.close()
    return one_shot_samples, persistent_samples


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

    one_shot, persistent = await _run(args.samples, args.connect_delay_ms)
    old_p50 = statistics.median(one_shot)
    new_p50 = statistics.median(persistent)
    print(f"samples={args.samples} modeled_handshake={args.connect_delay_ms:.1f}ms")
    print(f"one-shot:          p50={old_p50:.2f}ms p90={_percentile(one_shot, 0.9):.2f}ms")
    print(f"warmed persistent: p50={new_p50:.2f}ms p90={_percentile(persistent, 0.9):.2f}ms")
    print(f"reply-path savings: {old_p50 - new_p50:.2f}ms")


if __name__ == "__main__":
    asyncio.run(main())
