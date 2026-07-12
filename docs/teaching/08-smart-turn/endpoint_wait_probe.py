"""Decompose Chapter 8 endpoint waits without audio devices or providers.

Run with::

    uv run python docs/teaching/08-smart-turn/endpoint_wait_probe.py
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import main as chapter


class ProbeJournal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **row) -> None:
        self.rows.append(row)


class ScriptedVAD:
    def __init__(self, clock: dict[str, float], stop_at: float, fallback_at: float | None) -> None:
        self._clock = clock
        self._stop_at = stop_at
        self._fallback_at = fallback_at
        self._calls = 0

    async def process(self, _chunk):
        self._calls += 1
        if self._calls == 1:
            yield chapter.VADStartSpeaking()
        elif self._calls == 2:
            self._clock["now"] = self._stop_at
            yield chapter.VADStopSpeaking()
        elif self._fallback_at is not None:
            self._clock["now"] = self._fallback_at


class ScriptedSmartTurn:
    def __init__(self, clock: dict[str, float], probability: float) -> None:
        self._clock = clock
        self._probability = probability

    async def detect(self, _audio):
        self._clock["now"] += 0.04
        return SimpleNamespace(
            probability=self._probability,
            prediction="complete" if self._probability >= 0.5 else "incomplete",
        )


async def audio(chunk_count: int):
    for index in range(chunk_count):
        yield f"chunk-{index}"


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


async def run_case(*, mode: str, probability: float | None = None) -> dict[str, object]:
    clock = {"now": 0.0}
    journal = ProbeJournal()
    fallback = probability is not None and probability < chapter.SMART_THRESHOLD
    stop_at = 1.2 if mode == "smart" else 1.8
    detector = chapter.MiniTurnDetector(
        ScriptedVAD(
            clock,
            stop_at=stop_at,
            # Step infinitesimally beyond the inclusive 800 ms boundary
            # so binary float representation cannot leave it just below.
            fallback_at=2.040000000000001 if fallback else None,
        ),
        smart_turn=(ScriptedSmartTurn(clock, probability) if probability is not None else None),
        silence_wait_ms=(
            chapter.SMART_EARLY_SILENCE_MS if mode == "smart" else chapter.VAD_BASELINE_SILENCE_MS
        ),
        fallback_ms=chapter.SMART_FALLBACK_MS,
        journal=journal,
        session_id=mode,
    )

    real_monotonic = chapter.time.monotonic
    chapter.time.monotonic = lambda: clock["now"]
    try:
        events = [event async for event in detector.frames(audio(3 if fallback else 2))]
    finally:
        chapter.time.monotonic = real_monotonic
    endpoint = next(row["data"] for row in journal.rows if row["name"] == "turn.endpoint_commit")
    components = (
        endpoint["silence_wait_ms"]
        + (endpoint["classification_inference_ms"] or 0.0)
        + endpoint["pending_wait_ms"]
    )
    return {
        "classification_inference_ms": rounded(endpoint["classification_inference_ms"]),
        "components_match_total": rounded(components) == rounded(endpoint["endpoint_wait_ms"]),
        "endpoint_wait_ms": rounded(endpoint["endpoint_wait_ms"]),
        "pending_wait_ms": rounded(endpoint["pending_wait_ms"]),
        "reason": endpoint["reason"],
        "silence_wait_ms": endpoint["silence_wait_ms"],
        "speech_ended": any(tag == "speech_ended" for tag, _value in events),
    }


async def probe() -> dict[str, dict[str, object]]:
    return {
        "baseline_vad": await run_case(mode="vad"),
        "smart_accept": await run_case(mode="smart", probability=0.9),
        "smart_fallback": await run_case(mode="smart", probability=0.1),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
