#!/usr/bin/env python3
"""Model OpenAI TTS first-audio buffering before and after low-latency rechunking.

The benchmark isolates EasyCat's HTTP-stream buffering contribution.  It
models a provider delivering 10 ms / 480-byte PCM pieces at a fixed cadence:
the former fixed 4800-byte policy waits for ten pieces, while the current
policy releases a 960-byte first frame after two and returns to 4800-byte
steady-state frames afterwards.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from easycat.tts.openai_tts import (
    _FIRST_AUDIO_CHUNK_BYTES,
    _STEADY_AUDIO_CHUNK_BYTES,
)


def _first_dispatch(*, target_bytes: int, network_chunk_bytes: int, interval_ms: float) -> dict:
    chunks = math.ceil(target_bytes / network_chunk_bytes)
    return {
        "network_chunks": chunks,
        "latency_ms": chunks * interval_ms,
        "payload_bytes": target_bytes,
    }


def compare(*, network_chunk_bytes: int = 480, interval_ms: float = 10.0) -> dict[str, Any]:
    """Return modeled legacy and low-latency first-dispatch measurements."""
    if network_chunk_bytes <= 0:
        raise ValueError("network_chunk_bytes must be positive")
    if interval_ms < 0:
        raise ValueError("interval_ms must be non-negative")

    legacy = _first_dispatch(
        target_bytes=_STEADY_AUDIO_CHUNK_BYTES,
        network_chunk_bytes=network_chunk_bytes,
        interval_ms=interval_ms,
    )
    low_latency = _first_dispatch(
        target_bytes=_FIRST_AUDIO_CHUNK_BYTES,
        network_chunk_bytes=network_chunk_bytes,
        interval_ms=interval_ms,
    )
    saved_ms = legacy["latency_ms"] - low_latency["latency_ms"]
    return {
        "schema_version": 1,
        "source_format": "pcm_s16le_24000_mono",
        "network_chunk_bytes": network_chunk_bytes,
        "network_interval_ms": interval_ms,
        "legacy_fixed_chunk": legacy,
        "low_latency_first_chunk": low_latency,
        "steady_state_chunk_bytes": _STEADY_AUDIO_CHUNK_BYTES,
        "saved_ms": saved_ms,
        "reduction_percent": (
            saved_ms / legacy["latency_ms"] * 100.0 if legacy["latency_ms"] else 0.0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-chunk-bytes", type=int, default=480)
    parser.add_argument("--interval-ms", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        payload = compare(
            network_chunk_bytes=args.network_chunk_bytes,
            interval_ms=args.interval_ms,
        )
    except ValueError as exc:
        parser.error(str(exc))

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
