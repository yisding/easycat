"""Chapter 2 — batch transcription.

Record 5 seconds of mic audio, transcribe it in one shot, print the
result. Also writes a minimal debug bundle to ``runs/``.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import types
import wave
from pathlib import Path

import sounddevice as sd

from easycat.debug.export import export_debug_bundle
from easycat.quick import transcribe_file
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

SAMPLE_RATE = 16_000  # OpenAI + most STT providers default to 16 kHz.
DURATION_S = 5
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch02-batch-{int(time.time())}"


def record_wav(path: Path) -> None:
    print(f"Recording {DURATION_S}s at {SAMPLE_RATE} Hz... speak now.")
    samples = sd.rec(
        frames=DURATION_S * SAMPLE_RATE,
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY in your environment first.")

    journal = InMemoryRingBuffer(capacity=10_000)

    # 1) Record to a temp WAV — the easiest input for transcribe_file.
    wav_path = Path(tempfile.mkdtemp()) / "ch02-batch.wav"
    record_wav(wav_path)
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="recording.complete",
        session_id=SESSION_ID,
        data={"path": str(wav_path), "duration_s": DURATION_S},
    )

    # 2) Send it to the default OpenAI STT provider in one call.
    #    transcribe_file is the `easycat.quick` convenience helper —
    #    it is ~30 lines of code; read src/easycat/quick.py if curious.
    print("Transcribing...")
    request_start = time.monotonic()
    transcript = await transcribe_file(wav_path)
    elapsed = time.monotonic() - request_start

    journal.append(
        kind=JournalRecordKind.EVENT,
        name="stt.final",
        session_id=SESSION_ID,
        data={
            "stage": "stt",
            "event_type": "final",
            "text": transcript,
            "request_elapsed_ms": elapsed * 1000,
        },
    )

    print(f"\nTranscript ({elapsed:.2f}s wall-clock): {transcript or '<empty>'}")
    print(
        f"Perceived latency ≈ {DURATION_S:.2f}s speech + "
        f"{elapsed:.2f}s transcription = {DURATION_S + elapsed:.2f}s. "
        "That is the batch floor."
    )

    # 3) Write a bundle. The stub below is all export_debug_bundle needs:
    #    anything with a `_journal` attribute.
    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"Wrote bundle → {bundle_path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    asyncio.run(main())
