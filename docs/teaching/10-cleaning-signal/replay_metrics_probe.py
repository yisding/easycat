"""Exercise Chapter 10's replay contract without native audio backends.

Run with::

    uv run python docs/teaching/10-cleaning-signal/replay_metrics_probe.py
"""

from __future__ import annotations

import array
import asyncio
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory


def load_replay():
    path = Path(__file__).with_name("replay.py")
    spec = importlib.util.spec_from_file_location("teaching_ch10_replay_metrics", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def pcm16_frame(value: int, sample_count: int) -> bytes:
    samples = array.array("h", [value] * sample_count)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


class ScaleFilter:
    def __init__(self, replay, scale: float) -> None:
        self._replay = replay
        self._scale = scale

    async def process(self, chunk):
        samples = array.array("h")
        samples.frombytes(chunk.data)
        if sys.byteorder == "big":
            samples.byteswap()
        scaled = array.array("h", [round(sample * self._scale) for sample in samples])
        if sys.byteorder == "big":
            scaled.byteswap()
        return self._replay.AudioChunk(data=scaled.tobytes(), format=chunk.format)

    def feed_reference(self, _chunk) -> None:
        return None

    def version_info(self):
        return {"provider": "scale-0.5"}

    async def close(self) -> None:
        return None


class OneStartVAD:
    def __init__(self, replay) -> None:
        self._replay = replay
        self._calls = 0

    async def process(self, _chunk):
        self._calls += 1
        if self._calls == 1:
            yield self._replay.VADStartSpeaking()

    async def close(self) -> None:
        return None


async def probe() -> dict[str, object]:
    replay = load_replay()
    audio_format = replay.AudioFormat(sample_rate=24_000, channels=1, sample_width=2)
    samples_per_frame = audio_format.sample_rate * replay.FRAME_MS // 1000
    mic_frame = pcm16_frame(1000, samples_per_frame)
    ref_frame = pcm16_frame(500, samples_per_frame)

    def read_wav(path: Path):
        if path.name == "mic.wav":
            return mic_frame * 2, audio_format
        if path.name == "ref.wav":
            return ref_frame * 2, audio_format
        if path.name == "short-ref.wav":
            return ref_frame, audio_format
        raise AssertionError(path)

    replay._read_wav = read_wav
    errors: dict[str, str] = {}
    for name, ref_path in (
        ("missing_reference", None),
        ("short_reference", Path("short-ref.wav")),
    ):
        try:
            await replay.run(Path("mic.wav"), ref_path, "on", "on")
        except SystemExit as exc:
            errors[name] = str(exc)

    captured: dict[str, object] = {}

    def capture_bundle(session, *_args, **_kwargs) -> None:
        captured["records"] = session.journal.read()

    replay.create_noise_reducer = lambda _config: ScaleFilter(replay, 0.5)
    replay.create_echo_canceller = lambda _config: ScaleFilter(replay, 0.5)
    replay.create_vad = lambda _config: OneStartVAD(replay)
    replay.export_debug_bundle = capture_bundle

    # replay.run() always creates its configured output directory, even though
    # this probe intercepts bundle bytes below. Keep that teaching artifact out
    # of the checkout so the credential-free spine is workspace-clean.
    with TemporaryDirectory(dir=Path.cwd()) as temp_dir, redirect_stdout(io.StringIO()):
        replay.RUNS_DIR = Path(temp_dir)
        summary = await replay.run(Path("mic.wav"), Path("ref.wav"), "on", "on")

    records = captured["records"]
    frames = [record.data for record in records if record.name == "replay.frame"]
    return {
        "aligned": {
            "first_frame": frames[0],
            "frame_records": len(frames),
            "summary": summary,
        },
        "errors": errors,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
