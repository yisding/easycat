"""Audio-health heuristics over stored PCM artifacts (stdlib only).

This module decodes the raw little-endian 16-bit mono PCM blobs that the TTS
and STT stages capture as journal artifacts and flags three classes of
audio defect for the issues engine:

- **Clipping** — a long run of near-full-scale samples, which sounds like
  harsh distortion.  Detected on both the bot's synthesized output
  (``tts_frame`` ``output_ref``) and the caller's captured mic input
  (the ``stt`` stage's ``stage_start`` ``input_ref``).
- **Near-silent capture** — the caller's mic input is so quiet (low RMS)
  that STT will struggle, usually a dead/muted mic or a gain problem.
- **Dead air** — a long wall-clock gap inside an active turn with no
  ``tts_frame`` output and no caller input, i.e. the bot went silent.

Everything here is stdlib-only (``array``/``struct`` — **no numpy**): numpy
is not a dependency of core or the ``debugger`` extra.  Decoding is bounded
so a bundle with tens of thousands of artifacts stays fast: we only analyze
the first/last :data:`_BYTE_CAP` bytes of each blob and stride-subsample by
``IssueThresholds.audio_stride``.  Only PCM16 mono artifacts are analyzed;
telephony mu-law (``sample_width == 1``) blobs are skipped because decoding
them as int16 would be meaningless.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable, Mapping
from typing import Any

from easycat.debug._pcm import decode_pcm_mono
from easycat.debug._turn_timeline import record_wall_ns, safe_turn_id

# Per-blob byte cap for analysis.  We scan at most the first and last
# ``_BYTE_CAP`` bytes of any artifact so a multi-megabyte concatenated blob
# never dominates the issues scan; clipping/silence defects show up at the
# edges as readily as the middle and the stride keeps the sample budget low.
_BYTE_CAP = 64 * 1024

# Sample magnitude (int16) at/above which a sample is "clipped".  Full scale
# is 32767; 32760 leaves a small guard band for near-max codec noise.
_CLIP_LEVEL = 32760

# Encodings we treat as raw signed 16-bit little-endian PCM.  ``None`` and the
# empty string mean "unspecified", which is the common in-tree default.
_PCM16_ENCODINGS = frozenset({"", "pcm", "pcm16", "pcm_s16le", "linear16"})


def _iter_int16(blob: bytes | bytearray, *, stride: int) -> array[int]:
    """Decode *blob* as little-endian int16 samples, stride-subsampled.

    Only the first and last :data:`_BYTE_CAP` bytes are decoded so the work
    stays bounded for large blobs.  ``stride`` keeps every ``stride``-th
    sample.  Trailing odd bytes (a half sample) are dropped.  Returns an
    empty ``array('h')`` for empty input.

    The per-chunk byte→sample decode is delegated to the shared
    :func:`easycat.debug._pcm.decode_pcm_mono` (mono int16, byte-order
    normalised); the byte-cap windowing and stride-subsampling stay here
    because they are specific to the bounded issues scan.
    """
    view = memoryview(blob)
    if len(view) > 2 * _BYTE_CAP:
        head = view[:_BYTE_CAP]
        tail = view[-_BYTE_CAP:]
    else:
        head = view
        tail = view[0:0]

    samples: array[int] = array("h")
    for chunk in (head, tail):
        decoded = decode_pcm_mono(bytes(chunk), sample_width=2, channels=1)
        if not decoded:
            continue
        if stride > 1:
            samples.extend(decoded[::stride])
        else:
            samples.extend(decoded)
    return samples


def detect_clipping(samples: array[int], *, max_run: int) -> int:
    """Return the longest run of consecutive clipped samples in *samples*.

    A sample clips when ``abs(sample) >= _CLIP_LEVEL``.  Callers compare the
    returned run length against ``IssueThresholds.clip_consecutive``; the
    ``max_run`` argument is the threshold and lets this short-circuit once a
    qualifying run is found (large blobs need not be fully scanned twice).
    """
    longest = 0
    current = 0
    for sample in samples:
        if sample >= _CLIP_LEVEL or sample <= -_CLIP_LEVEL:
            current += 1
            if current > longest:
                longest = current
                if longest >= max_run:
                    # Already over threshold; no need to keep counting higher.
                    return longest
        else:
            current = 0
    return longest


def compute_rms(samples: array[int]) -> float:
    """Root-mean-square amplitude of *samples* (0.0 for empty input)."""
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return (total / len(samples)) ** 0.5


def _is_pcm16_mono(data: Mapping[str, Any]) -> bool:
    """True when the record's audio-format fields describe int16 PCM.

    Telephony mu-law artifacts carry ``sample_width == 1`` and are skipped.
    ``sample_width`` defaults to "unknown" (analyzed) when absent because the
    in-tree OpenAI path records PCM16 without always stamping the width.
    """
    width = data.get("sample_width")
    if width is not None and width != 2:
        return False
    encoding = data.get("encoding")
    if encoding is None:
        return True
    if not isinstance(encoding, str):
        return False
    return encoding.strip().lower() in _PCM16_ENCODINGS


def _record_data(record: Mapping[str, Any]) -> dict[str, Any]:
    data = record.get("data")
    return data if isinstance(data, dict) else {}


def _record_name(record: Mapping[str, Any]) -> str:
    name = record.get("name")
    return name if isinstance(name, str) else ""


def _resolve_samples(
    record: Mapping[str, Any],
    ref_key: str,
    resolver: Callable[[str], bytes | None],
    *,
    stride: int,
) -> array[int] | None:
    """Resolve *ref_key* on *record* to decoded PCM16 samples, or ``None``.

    Returns ``None`` (skip) when the record is not PCM16 mono, the ref is
    missing, the artifact cannot be resolved, or it decodes to no samples.
    """
    data = _record_data(record)
    if not _is_pcm16_mono(data):
        return None
    ref = record.get(ref_key)
    if not isinstance(ref, str) or not ref:
        return None
    blob = resolver(ref)
    if not blob:
        return None
    samples = _iter_int16(blob, stride=stride)
    return samples if samples else None


def collect_clipping(
    records: list[dict[str, Any]],
    resolver: Callable[[str], bytes | None],
    *,
    stride: int,
    clip_consecutive: int,
) -> list[tuple[str, str | None, int | None]]:
    """Find clipping in bot (``tts_frame``) and caller (``stt`` input) audio.

    Returns ``(side, turn_id, sequence)`` tuples where ``side`` is ``"bot"``
    or ``"caller"`` — one per clipping artifact.
    """
    hits: list[tuple[str, str | None, int | None]] = []
    for record in records:
        name = _record_name(record)
        if name == "tts_frame":
            side, ref_key = "bot", "output_ref"
        elif name == "stage_start" and _record_data(record).get("stage") == "stt":
            side, ref_key = "caller", "input_ref"
        else:
            continue
        samples = _resolve_samples(record, ref_key, resolver, stride=stride)
        if samples is None:
            continue
        run = detect_clipping(samples, max_run=clip_consecutive)
        if run >= clip_consecutive:
            turn_id = safe_turn_id(record.get("turn_id"))
            seq = record.get("sequence")
            seq = seq if isinstance(seq, int) else None
            hits.append((side, turn_id, seq))
    return hits


def collect_caller_silence(
    records: list[dict[str, Any]],
    resolver: Callable[[str], bytes | None],
    *,
    stride: int,
    silence_rms: float,
) -> dict[str, float]:
    """Aggregate caller-input RMS per turn; flag turns below *silence_rms*.

    Returns ``{turn_id: peak_rms}`` only for turns whose loudest caller-input
    artifact stays below the silence threshold (a dead/muted mic).  Turns with
    at least one audible artifact are excluded.
    """
    per_turn_peak: dict[str, float] = {}
    seen_turns: set[str] = set()
    for record in records:
        if _record_name(record) != "stage_start":
            continue
        if _record_data(record).get("stage") != "stt":
            continue
        turn_id = safe_turn_id(record.get("turn_id"))
        if turn_id is None:
            continue
        samples = _resolve_samples(record, "input_ref", resolver, stride=stride)
        if samples is None:
            continue
        seen_turns.add(turn_id)
        rms = compute_rms(samples)
        prev = per_turn_peak.get(turn_id)
        if prev is None or rms > prev:
            per_turn_peak[turn_id] = rms
    return {
        turn_id: peak
        for turn_id, peak in per_turn_peak.items()
        if turn_id in seen_turns and peak < silence_rms
    }


# Records that count as "the turn is producing/consuming audio" for dead-air
# gap detection.  A gap between two such records inside one turn means neither
# the bot nor the caller emitted audio for that span.
_ACTIVE_AUDIO_NAMES = frozenset({"tts_frame", "stage_start"})


def detect_dead_air(
    records: list[dict[str, Any]],
    *,
    dead_air_ms: float,
) -> dict[str, float]:
    """Find the largest intra-turn wall gap between active-audio records.

    Returns ``{turn_id: gap_ms}`` for turns whose maximum gap between
    consecutive ``tts_frame``/``stt`` ``stage_start`` records exceeds
    *dead_air_ms* (the bot went silent mid-turn).
    """
    by_turn: dict[str, list[int]] = {}
    for record in records:
        name = _record_name(record)
        if name not in _ACTIVE_AUDIO_NAMES:
            continue
        if name == "stage_start" and _record_data(record).get("stage") != "stt":
            continue
        turn_id = safe_turn_id(record.get("turn_id"))
        if turn_id is None:
            continue
        wall = record_wall_ns(record)
        if wall is None:
            continue
        by_turn.setdefault(turn_id, []).append(wall)

    flagged: dict[str, float] = {}
    for turn_id, walls in by_turn.items():
        walls.sort()
        max_gap_ns = 0
        for prev, nxt in zip(walls, walls[1:]):
            gap = nxt - prev
            if gap > max_gap_ns:
                max_gap_ns = gap
        gap_ms = max_gap_ns / 1_000_000
        if gap_ms > dead_air_ms:
            flagged[turn_id] = gap_ms
    return flagged


__all__ = [
    "collect_caller_silence",
    "collect_clipping",
    "compute_rms",
    "detect_clipping",
    "detect_dead_air",
]
