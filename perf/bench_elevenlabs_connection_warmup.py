"""Benchmark ElevenLabs connection setup on the reply-critical path.

The deterministic harness models an 80 ms DNS/TLS/WebSocket handshake for
both supported transports. It performs no network calls or billed synthesis.

Run with::

    uv run python perf/bench_elevenlabs_connection_warmup.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import time
from collections.abc import AsyncIterator

from easycat.tts.elevenlabs_tts import (
    ElevenLabsStreamMode,
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
)


class _HTTPResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int = 4800) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class _HTTPStream:
    def __init__(self, client: _HTTPClient) -> None:
        self._client = client
        self._response = _HTTPResponse([b"\x00\x00" * 240])

    async def __aenter__(self) -> _HTTPResponse:
        await self._client.connect_if_needed()
        return self._response

    async def __aexit__(self, *_args) -> None:
        await self._response.aclose()


class _HTTPClient:
    def __init__(self, *, connect_delay_s: float) -> None:
        self._connect_delay_s = connect_delay_s
        self._connected = False

    async def connect_if_needed(self) -> None:
        if not self._connected:
            await asyncio.sleep(self._connect_delay_s)
            self._connected = True

    async def get(self, _url: str) -> _HTTPResponse:
        await self.connect_if_needed()
        return _HTTPResponse([])

    def stream(self, *_args, **_kwargs) -> _HTTPStream:
        return _HTTPStream(self)

    async def aclose(self) -> None:
        self._connected = False


class _WSSocket:
    def __init__(self, *, connect_delay_s: float) -> None:
        self._connect_delay_s = connect_delay_s
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def connect(self) -> None:
        await asyncio.sleep(self._connect_delay_s)

    async def send(self, frame: str) -> None:
        message = json.loads(frame)
        if message.get("text") != "":
            return
        context_id = message.get("context_id")
        audio = base64.b64encode(b"\x00\x00" * 240).decode()
        response = {"audio": audio}
        terminal = {"isFinal": True}
        if context_id is not None:
            response["contextId"] = context_id
            terminal["contextId"] = context_id
        await self._queue.put(json.dumps(response))
        await self._queue.put(json.dumps(terminal))

    async def recv_iter(self) -> AsyncIterator[str]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        await self._queue.put(None)


async def _first_audio_ms(provider: ElevenLabsTTS) -> float:
    started = time.perf_counter()
    first_audio_ms: float | None = None
    async for event in provider.synthesize("Latency benchmark."):
        if event.audio is not None and first_audio_ms is None:
            first_audio_ms = (time.perf_counter() - started) * 1000.0
    if first_audio_ms is None:
        raise RuntimeError("benchmark provider emitted no audio")
    return first_audio_ms


async def _http_samples(samples: int, connect_delay_s: float) -> tuple[list[float], list[float]]:
    cold_samples: list[float] = []
    warm_samples: list[float] = []
    for _ in range(samples):
        cold = ElevenLabsTTS(
            ElevenLabsTTSConfig(
                api_key="benchmark",
                stream_mode=ElevenLabsStreamMode.HTTP,
            )
        )
        cold._client = _HTTPClient(connect_delay_s=connect_delay_s)  # type: ignore[assignment]
        cold_samples.append(await _first_audio_ms(cold))
        await cold.close()

        warm = ElevenLabsTTS(
            ElevenLabsTTSConfig(
                api_key="benchmark",
                stream_mode=ElevenLabsStreamMode.HTTP,
            )
        )
        warm._client = _HTTPClient(connect_delay_s=connect_delay_s)  # type: ignore[assignment]
        await warm.warmup()
        warm_samples.append(await _first_audio_ms(warm))
        await warm.close()
    return cold_samples, warm_samples


async def _websocket_samples(
    samples: int, connect_delay_s: float
) -> tuple[list[float], list[float]]:
    one_shot = ElevenLabsTTS(
        ElevenLabsTTSConfig(
            api_key="benchmark",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            persistent_ws=False,
        )
    )
    one_shot._make_ws = (  # type: ignore[method-assign]
        lambda _url, _hook: _WSSocket(connect_delay_s=connect_delay_s)
    )

    persistent = ElevenLabsTTS(ElevenLabsTTSConfig(api_key="benchmark"))
    socket = _WSSocket(connect_delay_s=connect_delay_s)
    persistent._build_multi_ws = lambda _hook: socket  # type: ignore[method-assign]
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


def _print_result(label: str, baseline: list[float], optimized: list[float]) -> None:
    old_p50 = statistics.median(baseline)
    new_p50 = statistics.median(optimized)
    print(label)
    print(f"  baseline:  p50={old_p50:.2f}ms p90={_percentile(baseline, 0.9):.2f}ms")
    print(f"  optimized: p50={new_p50:.2f}ms p90={_percentile(optimized, 0.9):.2f}ms")
    print(f"  reply-path savings: {old_p50 - new_p50:.2f}ms")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--connect-delay-ms", type=float, default=80.0)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    if args.connect_delay_ms < 0:
        parser.error("--connect-delay-ms must be non-negative")

    delay_s = args.connect_delay_ms / 1000.0
    http = await _http_samples(args.samples, delay_s)
    websocket = await _websocket_samples(args.samples, delay_s)
    print(f"samples={args.samples} modeled_handshake={args.connect_delay_ms:.1f}ms")
    _print_result("HTTP first reply", *http)
    _print_result("WebSocket replies", *websocket)


if __name__ == "__main__":
    asyncio.run(main())
