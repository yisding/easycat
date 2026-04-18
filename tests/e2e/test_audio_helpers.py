"""Tests for the test-support audio helpers in ``tests/e2e/_audio.py``.

These helpers are used by the live-provider benchmarks; they need to
be deterministic on their own so benchmark flakiness stays isolated
to the network-facing parts.
"""

from __future__ import annotations

import struct

from tests.e2e._audio import measure_rms, silence_pcm16, trim_trailing_silence


def _pcm_tone(duration_ms: int, sample_rate: int = 24000, amp: int = 16000) -> bytes:
    """Generate ``duration_ms`` of non-zero PCM16 samples (constant amp)."""
    n = int(sample_rate * duration_ms / 1000)
    return struct.pack(f"<{n}h", *([amp] * n))


def test_trim_trailing_silence_keeps_bounded_tail():
    """A tone followed by silence should trim to the tone plus ``keep_tail_ms``."""
    tone = _pcm_tone(500)  # 500ms of audio
    tail = silence_pcm16(duration_s=2.0, sample_rate=24000)  # 2s trailing silence
    pcm = tone + tail
    trimmed = trim_trailing_silence(pcm, sample_rate=24000, keep_tail_ms=60)
    # Expect ~ 500ms tone + 60ms tail = 560ms = 24000*0.56*2 bytes = 26880
    assert 24000 * 2 * 0.55 <= len(trimmed) <= 24000 * 2 * 0.60
    # The original was ~120000 bytes (2.5s); trimmed should be much smaller.
    assert len(trimmed) < len(pcm) / 3


def test_trim_trailing_silence_noop_on_pure_silence():
    """Pure silence should not be truncated to empty — that would break
    round-trip for edge-case fixtures."""
    pcm = silence_pcm16(duration_s=1.0, sample_rate=24000)
    trimmed = trim_trailing_silence(pcm, sample_rate=24000)
    assert trimmed == pcm


def test_trim_trailing_silence_noop_on_no_trailing_silence():
    """If the buffer ends in voiced audio, only the bounded tail past
    the last voiced window is left alone — buffer stays close to its
    original length."""
    pcm = _pcm_tone(500)
    trimmed = trim_trailing_silence(pcm, sample_rate=24000)
    # May add up to keep_tail_ms (60ms) on the end, but never chop voiced audio.
    assert len(trimmed) >= len(pcm) - 1  # at most one sample shy


def test_trim_trailing_silence_preserves_leading_silence():
    """Leading silence before speech shouldn't be touched — some VADs
    use pre-roll to get a noise floor estimate."""
    leading = silence_pcm16(duration_s=0.2, sample_rate=24000)
    tone = _pcm_tone(300)
    trailing = silence_pcm16(duration_s=1.0, sample_rate=24000)
    pcm = leading + tone + trailing
    trimmed = trim_trailing_silence(pcm, sample_rate=24000, keep_tail_ms=60)
    # Leading silence still present.
    assert trimmed.startswith(leading)
    # Tail trimmed to roughly 60ms after the tone ends.
    expected_tail_bytes = int(24000 * 2 * 0.06)
    assert abs(len(trimmed) - (len(leading) + len(tone) + expected_tail_bytes)) < 2400


def test_measure_rms_matches_expectation():
    """Sanity check on the RMS helper the trimmer depends on."""
    assert measure_rms(silence_pcm16(duration_s=0.1)) == 0.0
    tone_rms = measure_rms(_pcm_tone(100, sample_rate=16000, amp=16000))
    # 16000 amp / 32768 full-scale ≈ 0.488
    assert 0.45 < tone_rms < 0.52
