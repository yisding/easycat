"""Chapter 0 — Hello, Audio.

Record 3 seconds of mic audio, play it back, show the byte math,
then replay at different chunk sizes while simulating the wait for
the first live chunk so the reader can *hear* the latency difference.

Dependency:
    uv sync --extra local --group dev
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
    """Play the buffer in fixed-size chunks with a live-source startup delay.

    This recording is already complete, so every chunk would otherwise
    be ready immediately. A real source has to collect one full chunk
    before it can hand that chunk downstream. Sleeping once before the
    first write makes that source-buffering cost explicit in the demo.

    ``latency='low'`` and a matching ``blocksize`` keep PortAudio
    from letting a large host buffer hide the source-side delay that
    this demo is meant to expose.
    """
    chunk_samples = SAMPLE_RATE * chunk_ms // 1000
    requested_at = time.monotonic()
    print(f"  chunk_ms={chunk_ms:>4}  collecting first chunk...", flush=True)

    # The recording is already in memory, so its first chunk would otherwise
    # be available instantly. Model the time a live source needs to accumulate
    # one complete chunk before downstream playback can begin.
    time.sleep(chunk_ms / 1000)

    # OutputStream's context starts the stream on entry and stops + closes it
    # on every exit, including a failed write or Ctrl-C.
    with sd.OutputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=chunk_samples,
        latency="low",
    ) as stream:
        first_chunk = samples[:chunk_samples].reshape(-1, CHANNELS)
        stream.write(first_chunk)
        first_write_return = time.monotonic()
        for offset in range(chunk_samples, len(samples), chunk_samples):
            block = samples[offset : offset + chunk_samples].reshape(-1, CHANNELS)
            stream.write(block)

    total = time.monotonic() - requested_at
    print(
        "    "
        f"time-to-first-write-return={1000 * (first_write_return - requested_at):6.1f}ms  "
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

    # Chunk-size demo. The full recording is already in memory, so each
    # replay waits one chunk before its first write to model the time a
    # live source spends filling that chunk. 10 ms feels instant; 200 ms
    # feels slow-start. Device scheduling adds latency of its own.
    print("\nPlayback — chunked:")
    for chunk_ms in (10, 50, 200):
        play_chunked(samples, chunk_ms)


if __name__ == "__main__":
    main()
