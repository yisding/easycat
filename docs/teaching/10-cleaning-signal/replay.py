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
import array
import asyncio
import math
import sys
import time
import types
import wave
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.debug.export import export_debug_bundle
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.events import VADStartSpeaking
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.runtime.capabilities import close_if_supported
from easycat.vad import VADConfig
from easycat.vad.factory import create_vad

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


def _pcm16_power(data: bytes) -> tuple[int, int]:
    """Return sum-of-squares and sample count for little-endian PCM16."""
    if len(data) % 2:
        raise ValueError("PCM16 data must contain whole samples")
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return sum(sample * sample for sample in samples), len(samples)


def _rms(power: int, sample_count: int) -> float:
    return 0.0 if sample_count == 0 else math.sqrt(power / sample_count)


def _rms_change_db(input_rms: float, cleaned_rms: float) -> float | None:
    if input_rms <= 0 or cleaned_rms <= 0:
        return None
    return 20 * math.log10(cleaned_rms / input_rms)


def _validate_reference(
    mic_data: bytes,
    ref_data: bytes,
    audio_format: AudioFormat,
    ref_path: Path | None,
    aec_flag: str,
) -> None:
    frame_bytes = audio_format.sample_rate * FRAME_MS // 1000 * audio_format.frame_size
    mic_frame_count = len(mic_data) // frame_bytes
    ref_frame_count = len(ref_data) // frame_bytes
    if aec_flag == "on" and ref_path is None:
        raise SystemExit("--ref is required when --aec on")
    if aec_flag == "on" and ref_frame_count != mic_frame_count:
        raise SystemExit(
            f"mic and ref frame counts differ for AEC: {mic_frame_count} vs {ref_frame_count}"
        )


@dataclass
class ReplayMetrics:
    vad_starts: int = 0
    processed_frames: int = 0
    reference_frames_fed: int = 0
    input_power: int = 0
    input_samples: int = 0
    cleaned_power: int = 0
    cleaned_samples: int = 0

    async def measure_frame(self, mic_chunk, cleaned, ref_fed: bool, vad) -> dict[str, object]:
        self.processed_frames += 1
        self.reference_frames_fed += int(ref_fed)
        frame_input_power, frame_input_samples = _pcm16_power(mic_chunk.data)
        frame_cleaned_power, frame_cleaned_samples = _pcm16_power(cleaned.data)
        self.input_power += frame_input_power
        self.input_samples += frame_input_samples
        self.cleaned_power += frame_cleaned_power
        self.cleaned_samples += frame_cleaned_samples
        frame_vad_starts = 0
        async for event in vad.process(cleaned):
            if isinstance(event, VADStartSpeaking):
                frame_vad_starts += 1
        self.vad_starts += frame_vad_starts
        return {
            "stage": "audio",
            "frame_index": self.processed_frames,
            "input_rms": round(_rms(frame_input_power, frame_input_samples), 3),
            "cleaned_rms": round(_rms(frame_cleaned_power, frame_cleaned_samples), 3),
            "reference_fed": ref_fed,
            "vad_starts": frame_vad_starts,
        }

    def summary(self) -> dict[str, object]:
        input_rms = _rms(self.input_power, self.input_samples)
        cleaned_rms = _rms(self.cleaned_power, self.cleaned_samples)
        change_db = _rms_change_db(input_rms, cleaned_rms)
        return {
            "stage": "audio",
            "vad_starts": self.vad_starts,
            "mic_frames": self.processed_frames,
            "reference_frames_fed": self.reference_frames_fed,
            "input_rms": round(input_rms, 3),
            "cleaned_rms": round(cleaned_rms, 3),
            "rms_change_db": None if change_db is None else round(change_db, 3),
        }


async def run(
    mic_path: Path, ref_path: Path | None, nr_flag: str, aec_flag: str
) -> dict[str, object]:
    mic_data, mic_fmt = _read_wav(mic_path)
    ref_data, ref_fmt = _read_wav(ref_path) if ref_path else (b"", mic_fmt)
    if ref_path and mic_fmt != ref_fmt:
        raise SystemExit(f"mic and ref formats differ: {mic_fmt} vs {ref_fmt}")

    _validate_reference(mic_data, ref_data, mic_fmt, ref_path, aec_flag)

    journal = InMemoryRingBuffer(capacity=10_000)
    session_id = f"ch10-replay-{mic_path.stem}-nr{nr_flag}-aec{aec_flag}-{int(time.time())}"
    metrics = ReplayMetrics()

    async with AsyncExitStack() as resources:
        nr = create_noise_reducer(NoiseReducerConfig()) if nr_flag == "on" else _Passthrough()
        resources.push_async_callback(close_if_supported, nr)
        aec = (
            create_echo_canceller(EchoCancellationConfig(enabled=True))
            if aec_flag == "on"
            else _Passthrough()
        )
        resources.push_async_callback(close_if_supported, aec)
        vad = create_vad(VADConfig())
        resources.push_async_callback(close_if_supported, vad)

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
        for mic_chunk in mic_iter:
            ref_chunk = next(ref_iter, None)
            if ref_chunk is not None:
                aec.feed_reference(ref_chunk)
            cleaned = await nr.process(mic_chunk)
            cleaned = await aec.process(cleaned)
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="replay.frame",
                session_id=session_id,
                data=await metrics.measure_frame(mic_chunk, cleaned, ref_chunk is not None, vad),
            )

    summary = metrics.summary()
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="replay.summary",
        session_id=session_id,
        data=summary,
    )

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{session_id}.bundle"
    shim = types.SimpleNamespace(journal=journal)
    export_debug_bundle(shim, bundle_path, overwrite=True)
    print(f"VAD speech-starts: {metrics.vad_starts}")
    print(
        f"RMS input → cleaned: {summary['input_rms']} → {summary['cleaned_rms']} "
        f"({summary['rms_change_db']} dB)"
    )
    print(f"Wrote bundle → {bundle_path.relative_to(Path.cwd())}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic", type=Path, required=True)
    ap.add_argument("--ref", type=Path, default=None, help="Far-end reference (required for AEC).")
    ap.add_argument("--nr", choices=("on", "off"), default="off")
    ap.add_argument("--aec", choices=("on", "off"), default="off")
    args = ap.parse_args()
    if not args.mic.exists():
        raise SystemExit(f"{args.mic} does not exist. Run generate_fixtures.py first.")
    if args.ref is not None and not args.ref.exists():
        raise SystemExit(f"{args.ref} does not exist. Run generate_fixtures.py first.")
    asyncio.run(run(args.mic, args.ref, args.nr, args.aec))


if __name__ == "__main__":
    main()
