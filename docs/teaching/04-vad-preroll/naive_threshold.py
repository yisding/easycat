"""Chapter 4 — wrong-version-first warm-up.

Before reaching for Silero, here's what a naïve "is this chunk
loud?" classifier does. It measures RMS energy on every chunk and
calls anything above a fixed threshold "speech." Spoiler: it
classifies keyboard clicks as speech, drops out mid-vowel for soft
talkers, and never fires at all next to a fan.

Each chunk gets a `naive_vad.classify` journal record with the
RMS, threshold, and decision. Open the bundle afterwards to see
exactly where it goes wrong. The script does not call STT or TTS
— it just classifies live mic audio and prints / journals each
frame.

This is the predecessor `main.py` improves on. The contrast is
the chapter's whole pedagogical hook.

Dependencies:
    uv sync --extra quickstart --group dev
"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import time
import types
from pathlib import Path

from easycat import LocalTransportConfig
from easycat.audio_format import PCM16_MONO_24K
from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.transports.local import LocalTransport

RUNS_DIR = Path(__file__).parent / "runs"


def rms_energy(pcm16_bytes: bytes) -> float:
    """Root-mean-square of a PCM16 mono buffer, normalised to [0, 1]."""
    if not pcm16_bytes:
        return 0.0
    samples = struct.unpack(f"<{len(pcm16_bytes) // 2}h", pcm16_bytes)
    mean_sq = sum(s * s for s in samples) / len(samples)
    return math.sqrt(mean_sq) / 32768.0


async def classify_loop(transport, journal, session_id: str, threshold: float) -> None:
    """Classify each chunk as speech / not-speech, journal every decision."""
    state = "idle"
    last_state = state
    fire_count = 0

    async for chunk in transport.receive_audio():
        energy = rms_energy(chunk.data)
        is_speech = energy > threshold
        new_state = "speech" if is_speech else "idle"

        journal.append(
            kind=JournalRecordKind.EVENT,
            name="naive_vad.classify",
            session_id=session_id,
            data={
                "stage": "vad",
                "t_ms": time.monotonic() * 1000,
                "rms": round(energy, 4),
                "threshold": threshold,
                "decision": new_state,
            },
        )

        if new_state != last_state:
            if new_state == "speech":
                fire_count += 1
                print(f"  speech-on  (rms={energy:.3f})  ← fire #{fire_count}")
            else:
                print(f"  speech-off (rms={energy:.3f})")
            last_state = new_state
        state = new_state
    _ = state


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.05,
        help=(
            "RMS threshold for 'speech'. 0.05 is loud; 0.01 catches whispers"
            " but everything else too."
        ),
    )
    args = ap.parse_args()

    session_id = f"ch04-naive-threshold-{int(time.time())}"
    journal = InMemoryRingBuffer(capacity=20_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))

    await transport.connect()
    print(f"Naïve RMS-threshold VAD running. threshold={args.threshold}")
    print("Try each of these and watch what happens:")
    print("  - Say 'hello' normally — should fire once.")
    print("  - Whisper 'hello'        — may not fire at all (below threshold).")
    print("  - Type loudly            — fires on every keystroke.")
    print("  - Sit quietly near a fan — depends on fan loudness.")
    print("  - Say 'apples, bananas, pears' — drops out during the commas.")
    print("Ctrl-C to stop.\n")

    try:
        await classify_loop(transport, journal, session_id, args.threshold)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await transport.disconnect()

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{session_id}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")
    print("Open it and count: how many `decision='speech'` records line up with")
    print("real speech vs keystrokes / fans / coughs? Compare to main.py's bundle.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
