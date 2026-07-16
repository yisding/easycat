"""Provider-free frame trace for Chapter 4's pre-roll detector."""

from __future__ import annotations

import asyncio
import json

from main import MiniTurnDetector

from easycat.events import VADStartSpeaking, VADStopSpeaking


class ScriptedVAD:
    """Emit one predetermined event list for each input frame."""

    def __init__(self, events_by_frame: list[list[object]]) -> None:
        self._events = iter(events_by_frame)

    async def process(self, _chunk):
        for event in next(self._events):
            yield event


async def _audio():
    for frame in ("cached-1", "cached-2", "trigger", "live", "stop"):
        yield frame


async def trace(preroll_frames: int) -> list[dict[str, str | None]]:
    vad = ScriptedVAD(
        [
            [],
            [],
            [VADStartSpeaking()],
            [],
            [VADStopSpeaking()],
        ]
    )
    detector = MiniTurnDetector(vad, preroll_frames=preroll_frames)
    return [{"event": event, "frame": frame} async for event, frame in detector.frames(_audio())]


async def probe() -> dict[str, object]:
    return {
        "input_frames": ["cached-1", "cached-2", "trigger", "live", "stop"],
        "with_preroll": await trace(preroll_frames=2),
        "without_preroll": await trace(preroll_frames=0),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
