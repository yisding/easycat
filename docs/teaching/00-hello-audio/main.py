"""Chapter 0 — Hello, Audio.

Record 3 seconds of mic audio, play it back, show the byte math,
then replay at different chunk sizes so the reader can *hear* the
latency difference.

Dependency:
    uv sync --extra quickstart --group dev
"""

from __future__ import annotations

import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
DURATION_S = 3
CHANNELS = 1
DTYPE = np.int16


def record(seconds: int) -> np.ndarray:
    """Block for `seconds` while capturing mono int16 at 16 kHz."""
    print(f"Recording {seconds}s... speak now.")
    samples = sd.rec(
        frames=seconds * SAMPLE_RATE,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
    )
    sd.wait()
    return samples[:, 0]  # drop the channel dim; we're mono


def play_one_shot(samples: np.ndarray) -> None:
    """Play the whole buffer in a single blocking call."""
    sd.play(samples, SAMPLE_RATE)
    sd.wait()


def play_chunked(samples: np.ndarray, chunk_ms: int) -> None:
    """Play the buffer in fixed-size chunks so the reader can feel
    the chunking tradeoff.

    ``latency='low'`` and a matching ``blocksize`` keep PortAudio
    from pre-buffering a full second of audio before it starts —
    which would hide the whole point of the demo.
    """
    chunk_samples = SAMPLE_RATE * chunk_ms // 1000
    stream = sd.OutputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=chunk_samples,
        latency="low",
    )
    stream.start()
    open_time = time.monotonic()
    first_chunk = samples[:chunk_samples].reshape(-1, CHANNELS)
    stream.write(first_chunk)
    first_sound = time.monotonic()
    for offset in range(chunk_samples, len(samples), chunk_samples):
        block = samples[offset : offset + chunk_samples].reshape(-1, CHANNELS)
        stream.write(block)
    stream.stop()
    stream.close()
    total = time.monotonic() - open_time
    print(
        f"  chunk_ms={chunk_ms:>4}  "
        f"time-to-first-sound={1000 * (first_sound - open_time):6.1f}ms  "
        f"total={total:.2f}s"
    )


def explain_bytes(samples: np.ndarray) -> None:
    buffer = samples.tobytes()
    predicted = DURATION_S * SAMPLE_RATE * np.dtype(DTYPE).itemsize * CHANNELS
    print(
        f"Math: {DURATION_S}s × {SAMPLE_RATE} samples/s × "
        f"{np.dtype(DTYPE).itemsize} bytes/sample × {CHANNELS} ch "
        f"= {predicted} B"
    )
    print(f"Actual: len(buffer.tobytes()) = {len(buffer)} B")
    print(f"First 10 samples: {samples[:10].tolist()}")
    mn, mx = int(samples.min()), int(samples.max())
    print(f"Range: [{mn}, {mx}] (int16 clips at ±32767)")


def main() -> None:
    samples = record(DURATION_S)

    print("\nBytes:")
    explain_bytes(samples)

    print("\nPlayback — one-shot:")
    play_one_shot(samples)

    # Chunk-size demo. 10ms feels instant; 200ms feels slow-start.
    # We're not changing the audio — only how we *feed it* to the
    # speaker. Perceived latency = chunk size + scheduling jitter.
    print("\nPlayback — chunked:")
    for chunk_ms in (10, 50, 200):
        play_chunked(samples, chunk_ms)


if __name__ == "__main__":
    main()
