"""Test-only Goertzel DTMF decoder used to check synthesized tones.

Implements a conventional receiver: 205-sample analysis blocks at 8 kHz
(25.6 ms) evaluated at the eight Q.23 frequencies, sliding by a short hop.
A block counts as a key when

* the block carries real energy,
* the strongest low- and high-group components dominate the block energy,
* each group's strongest component stands clear of the runner-up, and
* the twist (high minus low group level) is within -8 / +4 dB.

Consecutive blocks with the same key form a segment; segments shorter than
40 ms are discarded as talk-off. Silence or noise between two segments of the
same key splits them, so ``"11"`` decodes as two presses.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from easycat._dtmf_tones import DTMF_SAMPLE_RATE

BLOCK_SAMPLES = 205
HOP_SAMPLES = 40
MIN_TONE_MS = 40

_LOW_HZ = (697, 770, 852, 941)
_HIGH_HZ = (1209, 1336, 1477, 1633)
# Spelled out independently of the renderer's ``DTMF_FREQUENCIES`` so a swapped
# pair in the renderer cannot be mirrored (and hidden) by the decoder.
EXPECTED_Q23_PAIRS: dict[str, tuple[int, int]] = {
    "1": (697, 1209),
    "2": (697, 1336),
    "3": (697, 1477),
    "A": (697, 1633),
    "4": (770, 1209),
    "5": (770, 1336),
    "6": (770, 1477),
    "B": (770, 1633),
    "7": (852, 1209),
    "8": (852, 1336),
    "9": (852, 1477),
    "C": (852, 1633),
    "*": (941, 1209),
    "0": (941, 1336),
    "#": (941, 1477),
    "D": (941, 1633),
}
_KEY_BY_PAIR = {pair: key for key, pair in EXPECTED_Q23_PAIRS.items()}

_MIN_BLOCK_RMS = 50.0
_MIN_TONE_FRACTION = 0.6
_MIN_PEAK_RATIO_DB = 6.0
_MAX_FORWARD_TWIST_DB = 4.0
_MAX_REVERSE_TWIST_DB = 8.0


@dataclass(frozen=True)
class DecodedKey:
    key: str
    start_sample: int
    end_sample: int

    @property
    def duration_ms(self) -> float:
        return (self.end_sample - self.start_sample) * 1000 / DTMF_SAMPLE_RATE


def _basis(sample_rate: int) -> np.ndarray:
    n = np.arange(BLOCK_SAMPLES)
    freqs = np.array(_LOW_HZ + _HIGH_HZ, dtype=np.float64)
    return np.exp(-2j * np.pi * np.outer(n, freqs) / sample_rate)


def _pcm16_to_array(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float64)


def tone_powers(samples: np.ndarray, sample_rate: int = DTMF_SAMPLE_RATE) -> np.ndarray:
    """Return per-block signal energy attributed to each Q.23 frequency.

    Shape ``(blocks, 8)``; columns are the four low then four high frequencies.
    For a pure sine of amplitude ``A`` the value is ``A**2 * N / 2``, the
    sine's energy in the block, so it compares directly with ``sum(x**2)``.
    """
    windows = np.lib.stride_tricks.sliding_window_view(samples, BLOCK_SAMPLES)[::HOP_SAMPLES]
    spectrum = windows @ _basis(sample_rate)
    return 2.0 * np.abs(spectrum) ** 2 / BLOCK_SAMPLES


def _classify(block: np.ndarray, powers: np.ndarray) -> str | None:
    energy = float(np.dot(block, block))
    if energy <= 0 or np.sqrt(energy / BLOCK_SAMPLES) < _MIN_BLOCK_RMS:
        return None
    low, high = powers[:4], powers[4:]
    low_order = np.argsort(low)[::-1]
    high_order = np.argsort(high)[::-1]
    low_peak, high_peak = low[low_order[0]], high[high_order[0]]
    if low_peak <= 0 or high_peak <= 0:
        return None
    if (low_peak + high_peak) / energy < _MIN_TONE_FRACTION:
        return None
    for peak, runner_up in ((low_peak, low[low_order[1]]), (high_peak, high[high_order[1]])):
        if runner_up > 0 and 10 * np.log10(peak / runner_up) < _MIN_PEAK_RATIO_DB:
            return None
    twist_db = 10 * np.log10(high_peak / low_peak)
    if twist_db > _MAX_FORWARD_TWIST_DB or twist_db < -_MAX_REVERSE_TWIST_DB:
        return None
    return _KEY_BY_PAIR[(_LOW_HZ[low_order[0]], _HIGH_HZ[high_order[0]])]


def decode_segments(pcm: bytes, sample_rate: int = DTMF_SAMPLE_RATE) -> list[DecodedKey]:
    """Decode PCM16 audio into DTMF key segments with sample positions."""
    samples = _pcm16_to_array(pcm)
    if sample_rate != DTMF_SAMPLE_RATE:
        raise ValueError("decoder expects 8 kHz audio")
    if len(samples) < BLOCK_SAMPLES:
        return []
    powers = tone_powers(samples, sample_rate)
    labels: list[str | None] = []
    for index, block_powers in enumerate(powers):
        start = index * HOP_SAMPLES
        labels.append(_classify(samples[start : start + BLOCK_SAMPLES], block_powers))

    segments: list[DecodedKey] = []
    run_key: str | None = None
    run_start = 0
    for index, label in enumerate([*labels, None]):
        if label == run_key:
            continue
        if run_key is not None:
            end = (index - 1) * HOP_SAMPLES + BLOCK_SAMPLES
            segment = DecodedKey(run_key, run_start * HOP_SAMPLES, end)
            if segment.duration_ms >= MIN_TONE_MS:
                segments.append(segment)
        run_key = label
        run_start = index
    return segments


def decode_dtmf(pcm: bytes, sample_rate: int = DTMF_SAMPLE_RATE) -> str:
    """Decode PCM16 audio into the string of DTMF keys it contains."""
    return "".join(segment.key for segment in decode_segments(pcm, sample_rate))
