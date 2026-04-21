"""Chapter 10 — replay a WAV pair through NR + AEC, dump a bundle.

The point is the *lockstep* reference feed: AEC is dual-input, so
a single-track replay would never give the adaptive filter
anything to subtract against. We march the mic and ref WAVs
frame-by-frame into ``nr.process(chunk)`` and
``aec.feed_reference(ref_chunk)`` in matched time.

    uv run python docs/teaching/10-cleaning-signal/replay.py \\
        --mic recordings/speakerphone_loop.mic.wav \\
        --ref recordings/speakerphone_loop.ref.wav \\
        --nr on --aec on

Produces a bundle in ``runs/`` with per-frame NR/AEC output stats
and an ``audio.config`` record of which backends are live.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import types
import wave
from pathlib import Path

from easycat import (
    EchoCancellationConfig,
    NoiseReducerConfig,
    create_echo_canceller,
    create_noise_reducer,
    create_vad,
)
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.debug.export import export_debug_bundle
from easycat.events import VADStartSpeaking
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.vad import VADConfig

FRAME_MS = 20
RUNS_DIR = Path(__file__).parent / "runs"


class _Passthrough:
    async def process(self, chunk):
        return chunk

    def feed_reference(self, chunk):
        pass

    def version_info(self):
        return {"provider": "off"}


def _read_wav(path: Path) -> tuple[bytes, AudioFormat]:
    with wave.open(str(path), "rb") as wf:
        assert wf.getsampwidth() == 2
        fmt = AudioFormat(
            sample_rate=wf.getframerate(),
            channels=wf.getnchannels(),
            sample_width=2,
        )
        data = wf.readframes(wf.getnframes())
    return data, fmt


def _chunks(data: bytes, fmt: AudioFormat):
    # AEC + VAD want whole frames. Drop any trailing short tail so a
    # reader-supplied WAV that isn't an even multiple of 20 ms doesn't
    # hand Silero/LiveKit a misaligned chunk.
    frame_bytes = fmt.sample_rate * FRAME_MS // 1000 * fmt.frame_size
    usable = (len(data) // frame_bytes) * frame_bytes
    for offset in range(0, usable, frame_bytes):
        yield AudioChunk(data=data[offset : offset + frame_bytes], format=fmt)


async def run(mic_path: Path, ref_path: Path | None, nr_flag: str, aec_flag: str) -> None:
    mic_data, mic_fmt = _read_wav(mic_path)
    ref_data, ref_fmt = _read_wav(ref_path) if ref_path else (b"", mic_fmt)
    if ref_path and mic_fmt != ref_fmt:
        raise SystemExit(f"mic and ref formats differ: {mic_fmt} vs {ref_fmt}")

    nr = create_noise_reducer(NoiseReducerConfig()) if nr_flag == "on" else _Passthrough()
    aec = (
        create_echo_canceller(EchoCancellationConfig(enabled=True))
        if aec_flag == "on"
        else _Passthrough()
    )
    vad = create_vad(VADConfig())

    journal = InMemoryRingBuffer(capacity=10_000)
    session_id = f"ch10-replay-{mic_path.stem}-nr{nr_flag}-aec{aec_flag}-{int(time.time())}"
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="audio.config",
        session_id=session_id,
        data={
            "stage": "audio",
            "nr": nr.version_info().get("provider", "unknown"),
            "aec": aec.version_info().get("provider", "unknown"),
            "mic": str(mic_path),
            "ref": str(ref_path) if ref_path else None,
        },
    )

    mic_iter = _chunks(mic_data, mic_fmt)
    ref_iter = _chunks(ref_data, ref_fmt) if ref_path else iter([])
    vad_starts = 0

    for mic_chunk in mic_iter:
        ref_chunk = next(ref_iter, None)
        if ref_chunk is not None:
            aec.feed_reference(ref_chunk)
        cleaned = await nr.process(mic_chunk)
        cleaned = await aec.process(cleaned)
        async for ev in vad.process(cleaned):
            if isinstance(ev, VADStartSpeaking):
                vad_starts += 1

    frame_bytes = mic_fmt.sample_rate * FRAME_MS // 1000 * mic_fmt.frame_size
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="replay.summary",
        session_id=session_id,
        data={
            "stage": "audio",
            "vad_starts": vad_starts,
            "mic_frames": len(mic_data) // frame_bytes,
        },
    )

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{session_id}.bundle"
    shim = types.SimpleNamespace(journal=journal)
    export_debug_bundle(shim, bundle_path, overwrite=True)
    print(f"VAD speech-starts: {vad_starts}")
    print(f"Wrote bundle → {bundle_path.relative_to(Path.cwd())}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic", type=Path, required=True)
    ap.add_argument("--ref", type=Path, default=None, help="Far-end reference (required for AEC).")
    ap.add_argument("--nr", choices=("on", "off"), default="off")
    ap.add_argument("--aec", choices=("on", "off"), default="off")
    args = ap.parse_args()
    if not args.mic.exists():
        raise SystemExit(f"{args.mic} does not exist. Run generate_fixtures.py first.")
    asyncio.run(run(args.mic, args.ref, args.nr, args.aec))


if __name__ == "__main__":
    main()
