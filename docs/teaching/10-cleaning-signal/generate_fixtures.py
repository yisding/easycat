"""Chapter 10 — generate synthetic audio fixtures for the NR / AEC demo.

Real field recordings would be better, but we can reproduce the
shape of each test condition with pure NumPy:

- ``noisy_alone.wav`` — a sine-wave "voice" at 500 Hz plus wideband
  white noise. Exercises NR.
- ``speakerphone_loop.mic.wav`` + ``speakerphone_loop.ref.wav`` — a
  sine-wave "bot" leaking back into a silent mic with ~30 ms echo
  delay and -18 dB attenuation. Exercises AEC. The mic and ref
  WAVs are sample-aligned; the replay harness feeds them in lockstep.
- ``hard_mode.mic.wav`` + ``hard_mode.ref.wav`` — both problems
  simultaneously (noise + bot bleed).

Run once; fixtures are checked in.

    uv run python docs/teaching/10-cleaning-signal/generate_fixtures.py
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 24_000
DURATION_S = 4
HERE = Path(__file__).parent
RECORDINGS = HERE / "recordings"


def _save_wav(path: Path, samples: np.ndarray) -> None:
    RECORDINGS.mkdir(exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _voice(duration_s: int, freq_hz: int = 500, amp: float = 0.2) -> np.ndarray:
    t = np.arange(0, duration_s * SAMPLE_RATE) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq_hz * t) * 32767).astype(np.int16)


def _noise(duration_s: int, amp: float = 0.05) -> np.ndarray:
    rng = np.random.default_rng(42)  # deterministic fixtures
    return (rng.normal(0, amp, duration_s * SAMPLE_RATE) * 32767).astype(np.int16)


def _echoed(ref: np.ndarray, delay_ms: int = 30, atten_db: float = -18) -> np.ndarray:
    delay_samples = SAMPLE_RATE * delay_ms // 1000
    atten = 10 ** (atten_db / 20)
    out = np.zeros_like(ref)
    out[delay_samples:] = (ref[:-delay_samples] * atten).astype(np.int16)
    return out


def main() -> None:
    print("Generating synthetic fixtures...")
    # User voice mixed with background hiss — NR target.
    user = _voice(DURATION_S, freq_hz=440, amp=0.35)
    hiss = _noise(DURATION_S, amp=0.15)
    _save_wav(RECORDINGS / "noisy_alone.wav", user + hiss)

    # Bot voice (distinct freq so you can tell the two apart on a
    # spectrogram) playing over a silent mic — AEC target.
    bot_ref = _voice(DURATION_S, freq_hz=800, amp=0.4)
    bot_echo = _echoed(bot_ref)
    _save_wav(RECORDINGS / "speakerphone_loop.mic.wav", bot_echo)
    _save_wav(RECORDINGS / "speakerphone_loop.ref.wav", bot_ref)

    # Hard mode: user voice + bot echo + hiss. Both stages needed.
    _save_wav(RECORDINGS / "hard_mode.mic.wav", user + bot_echo + hiss)
    _save_wav(RECORDINGS / "hard_mode.ref.wav", bot_ref)

    print(f"  wrote five files into {RECORDINGS.relative_to(Path.cwd())}/")


if __name__ == "__main__":
    main()
