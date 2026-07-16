"""Chapter 2 — batch transcription.

Record 5 seconds of mic audio, transcribe it in one shot, print the
result. Also writes a minimal debug bundle to ``runs/``.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...
    uv run easycat doctor
    uv run easycat doctor --env-file .env         # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
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
from easycat.recipes import transcribe_file
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

# A narrowband-friendly teaching rate. transcribe_file reads the rate from the
# WAV header rather than assuming one provider-wide default.
SAMPLE_RATE = 16_000
DURATION_S = 5
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch02-batch-{int(time.time())}"


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


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

    # 1) Record to a scoped temp WAV — the easiest input for
    #    transcribe_file. TemporaryDirectory deletes the raw microphone
    #    bytes on normal return and on provider/interrupt exceptions.
    with tempfile.TemporaryDirectory(prefix="easycat-ch02-") as directory:
        wav_path = Path(directory) / "ch02-batch.wav"
        record_wav(wav_path)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="recording.complete",
            session_id=SESSION_ID,
            data={
                "duration_s": DURATION_S,
                "filename": wav_path.name,
                "retention": "temporary",
            },
        )

        # 2) Send it to the default OpenAI STT provider in one call.
        #    transcribe_file is the `easycat.recipes` convenience helper —
        #    it is ~30 lines of code; read src/easycat/recipes.py if curious.
        print("Transcribing...")
        request_start = time.monotonic()
        transcript = await transcribe_file(wav_path)
        elapsed = time.monotonic() - request_start

    journal.append(
        kind=JournalRecordKind.EVENT,
        name="recording.cleaned",
        session_id=SESSION_ID,
        data={"deleted": not wav_path.exists(), "filename": wav_path.name},
    )

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
    #    anything with a `journal` or `_journal` attribute.
    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"Wrote bundle → {_display_path(bundle_path)}")


if __name__ == "__main__":
    asyncio.run(main())
