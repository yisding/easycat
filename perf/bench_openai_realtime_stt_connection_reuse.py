"""Benchmark OpenAI Realtime STT connection setup on the turn-critical path.

The deterministic harness models an 80 ms DNS/TCP/TLS/WebSocket handshake.
It performs no network calls and sends no provider-billed audio.

Run with::

    uv run python perf/bench_openai_realtime_stt_connection_reuse.py
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
from easycat.stt.openai_realtime_provider import (
    OpenAIRealtimeSTT,
    OpenAIRealtimeSTTConfig,
)


class _Socket:
    _STOP = object()

    def __init__(self) -> None:
        self.close_code: int | None = None
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._commit_count = 0

    async def send(self, data: str | bytes) -> None:
        if not isinstance(data, str):
            return
        message = json.loads(data)
        message_type = message.get("type")
        if message_type == "session.update":
            await self._put({"type": "transcription_session.updated", "session": {}})
        elif message_type == "input_audio_buffer.commit":
            self._commit_count += 1
            item_id = f"item-{self._commit_count}"
            await self._put({"type": "input_audio_buffer.committed", "item_id": item_id})
            await self._put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": item_id,
                    "transcript": "benchmark transcript",
                }
            )
        await asyncio.sleep(0)

    async def _put(self, message: dict[str, Any]) -> None:
        await self._queue.put(json.dumps(message))

    async def close(self) -> None:
        if self.close_code is not None:
            return
        self.close_code = 1000
        await self._queue.put(self._STOP)

    def __aiter__(self) -> _Socket:
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


async def _turn_to_final_ms(provider: OpenAIRealtimeSTT) -> float:
    first_final_ms: float | None = None
    started = time.perf_counter()

    async def collect() -> None:
        nonlocal first_final_ms
        async for event in provider.events():
            if event.type is STTEventType.FINAL and first_final_ms is None:
                first_final_ms = (time.perf_counter() - started) * 1000.0

    await provider.start_stream()
    collector = asyncio.create_task(collect())
    await provider.send_audio(AudioChunk(data=b"\x00\x00" * 2400, format=PCM16_MONO_16K))
    await provider.end_stream()
    await collector
    if first_final_ms is None:
        raise RuntimeError("benchmark provider emitted no final transcript")
    return first_final_ms


async def _samples(samples: int, connect_delay_s: float) -> tuple[list[float], list[float]]:
    one_shot = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(
            api_key="benchmark",
            persistent_ws=False,
            ws_connect=_SocketFactory(connect_delay_s),
        )
    )
    persistent = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(
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
