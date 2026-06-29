"""AEC (acoustic echo cancellation) diagnostics — pure-stdlib analysis.

The debugger aligns three audio tracks for one turn — the captured mic-in
(near-end), the bot playback fed to the echo canceller as the far-end
*reference*, and the *post-AEC* residual — and derives whether the canceller
is actually subtracting the bot's own voice from the mic.

All three tracks come from the journal:

- **mic-in** — the audio stage's ``stage_start`` ``input_ref`` (raw mic bytes
  before NR/AEC).
- **reference** — the ``aec_reference_frame`` ``output_ref`` captured by
  :meth:`easycat.stages.audio.AudioStage.record_reference` (the bot playback
  handed to ``feed_reference``).
- **post-AEC** — the audio stage's ``stage_complete`` ``output_ref`` (the
  residual after NR + AEC).

Diagnostics computed here:

- **ERLE** (Echo Return Loss Enhancement): per-frame
  ``10*log10(near_power / residual_power)`` in dB.  Higher is better — it is
  how much echo the canceller removed.
- **double-talk**: frames where both the reference (bot) and the mic (caller)
  carry energy at once, where AEC is hardest and ERLE is least trustworthy.
- **self-echo**: residual energy spikes in the post-AEC track that do NOT
  coincide with a recorded interruption — i.e. the bot hearing itself, which
  can trip a false barge-in.

DEPENDENCY CONSTRAINT: ``numpy`` is NOT available to the debugger (the extra
ships only ``aiohttp``).  Everything here is pure stdlib (``array``,
``math``) so it works in a bare install.
"""

from __future__ import annotations

import math
from typing import Any

from easycat.debug._pcm import decode_pcm_mono
from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME

# Track keys in the aligned view returned by :func:`align_tracks`.
_MIC_IN = "mic_in"
_REFERENCE = "reference"
_POST_AEC = "post_aec"


def _record_sequence(record: dict[str, Any]) -> int | None:
    seq = record.get("sequence")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def _record_mono_ns(record: dict[str, Any]) -> int:
    """Best-effort monotonic timestamp for ordering aligned frames."""
    timing = record.get("timing")
    if isinstance(timing, dict):
        value = timing.get("mono_ns")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    seq = _record_sequence(record)
    return seq if seq is not None else 0


def align_tracks(
    records: list[dict[str, Any]],
    *,
    source: Any,
    turn_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Group one turn's AEC tracks, ordered by ``timing.mono_ns``.

    Returns ``{"mic_in": [...], "reference": [...], "post_aec": [...]}`` where
    each entry is ``{"sequence", "mono_ns", "ref", "pcm", "data"}``.  ``source``
    must expose an ``artifact(ref) -> bytes | None`` resolver; frames whose
    artifact cannot be resolved are dropped (the diagnostics degrade rather
    than raise).  ``turn_id`` of ``None`` matches every turn.
    """
    tracks: dict[str, list[dict[str, Any]]] = {_MIC_IN: [], _REFERENCE: [], _POST_AEC: []}
    resolve = getattr(source, "artifact", None)
    for record in records:
        if turn_id is not None and record.get("turn_id") != turn_id:
            continue
        name = record.get("name")
        data = record.get("data")
        data = data if isinstance(data, dict) else {}
        track: str | None = None
        ref: Any = None
        if name == AEC_REFERENCE_FRAME_NAME:
            track = _REFERENCE
            ref = record.get("output_ref")
        elif data.get("stage") == "audio" and name == "stage_start":
            track = _MIC_IN
            ref = record.get("input_ref")
        elif data.get("stage") == "audio" and name == "stage_complete":
            track = _POST_AEC
            ref = record.get("output_ref")
        if track is None or not ref:
            continue
        pcm = resolve(ref) if callable(resolve) else None
        if not pcm:
            continue
        sequence = _record_sequence(record)
        if sequence is None:
            continue
        tracks[track].append(
            {
                "sequence": sequence,
                "mono_ns": _record_mono_ns(record),
                "ref": ref,
                "pcm": pcm,
                "data": data,
            }
        )
    for entries in tracks.values():
        entries.sort(key=lambda item: (item["mono_ns"], item["sequence"]))
    return tracks


def _frame_rms(samples: list[int], start: int, end: int) -> float:
    """Root-mean-square magnitude of ``samples[start:end]`` (0.0 when empty)."""
    if end <= start:
        return 0.0
    acc = 0.0
    for i in range(start, end):
        s = samples[i]
        acc += s * s
    return math.sqrt(acc / (end - start))


def frame_rms_series(
    pcm: bytes,
    *,
    sample_width: int = 2,
    channels: int = 1,
    sample_rate: int = 16000,
    frame_ms: int = 20,
) -> list[float]:
    """Per-frame RMS magnitudes for ``pcm`` at a fixed ``frame_ms`` window.

    Unsupported widths (notably 8-bit mu-law) decode to an empty sample list
    and therefore yield no frames, so the caller never sees mis-decoded
    garbage.
    """
    samples = decode_pcm_mono(pcm, sample_width=sample_width, channels=channels)
    if not samples:
        return []
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    out: list[float] = []
    for start in range(0, len(samples), frame_len):
        out.append(_frame_rms(samples, start, min(start + frame_len, len(samples))))
    return out


def compute_erle(
    mic_in_pcm: bytes,
    post_aec_pcm: bytes,
    *,
    sample_width: int = 2,
    channels: int = 1,
    sample_rate: int = 16000,
    frame_ms: int = 20,
) -> dict[str, Any]:
    """Per-frame Echo Return Loss Enhancement (ERLE) in dB.

    For each aligned ``frame_ms`` window ERLE is
    ``10*log10(near_power / residual_power)`` where *near* is the mic-in frame
    and *residual* is the post-AEC frame.  A frame whose mic power is below a
    tiny floor (near-silence) is skipped — ERLE is undefined when there is no
    echo to cancel.  A near-zero residual is clamped to a small floor so a
    perfect cancellation reports a large finite dB value instead of ``inf``.

    Returns ``{"frame_ms", "frames": [<dB | None>...], "mean_db", "min_db",
    "max_db"}`` where per-frame ``None`` marks a skipped (silent) frame and the
    summary stats are computed over the measured frames only (``None`` when no
    frame qualified).
    """
    near = frame_rms_series(
        mic_in_pcm,
        sample_width=sample_width,
        channels=channels,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
    )
    residual = frame_rms_series(
        post_aec_pcm,
        sample_width=sample_width,
        channels=channels,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
    )
    n = min(len(near), len(residual))
    # Near-silence floor (below this the mic frame carries no echo to enhance).
    silence_floor = 1.0
    frames: list[float | None] = []
    measured: list[float] = []
    for i in range(n):
        near_rms = near[i]
        if near_rms <= silence_floor:
            frames.append(None)
            continue
        # Clamp the residual away from zero so a perfect null reports a finite,
        # large positive dB rather than dividing by zero.
        residual_rms = max(residual[i], 1e-6)
        erle_db = 20.0 * math.log10(near_rms / residual_rms)
        frames.append(erle_db)
        measured.append(erle_db)
    summary: dict[str, Any] = {
        "frame_ms": frame_ms,
        "frames": frames,
        "mean_db": (sum(measured) / len(measured)) if measured else None,
        "min_db": min(measured) if measured else None,
        "max_db": max(measured) if measured else None,
        "measured_frames": len(measured),
    }
    return summary


def detect_double_talk(
    reference_rms: list[float],
    mic_rms: list[float],
    *,
    thresh: float = 200.0,
) -> list[dict[str, int]]:
    """Find contiguous frame bands where reference AND mic both carry energy.

    Double-talk is when the bot (reference/far-end) and the caller (mic/near-end)
    speak simultaneously — the regime where AEC struggles and ERLE is least
    trustworthy.  A frame counts as double-talk when both per-frame RMS series
    exceed ``thresh``.  Returns a list of ``{"start": <frame>, "end": <frame>}``
    half-open bands (``end`` exclusive) so the UI can shade them.
    """
    n = min(len(reference_rms), len(mic_rms))
    bands: list[dict[str, int]] = []
    run_start: int | None = None
    for i in range(n):
        both = reference_rms[i] > thresh and mic_rms[i] > thresh
        if both and run_start is None:
            run_start = i
        elif not both and run_start is not None:
            bands.append({"start": run_start, "end": i})
            run_start = None
    if run_start is not None:
        bands.append({"start": run_start, "end": n})
    return bands


def detect_self_echo(
    post_aec_pcm: bytes,
    interruption_frames: list[int],
    *,
    sample_width: int = 2,
    channels: int = 1,
    sample_rate: int = 16000,
    frame_ms: int = 20,
    spike_thresh: float = 1500.0,
    guard_frames: int = 5,
) -> list[dict[str, int]]:
    """Find post-AEC energy spikes that do NOT coincide with an interruption.

    A residual spike in the post-AEC track means the canceller failed to remove
    the bot's own voice — *unless* the caller really did speak there (a genuine
    barge-in), in which case a ``turn_state_changed``/interruption record sits
    nearby.  ``interruption_frames`` is the list of frame indices at which an
    interruption was recorded; a spike within ``guard_frames`` of any of them is
    treated as a real barge-in and NOT flagged.  Spikes with no coincident
    interruption are returned as ``{"frame": <i>, "rms": <int>}`` self-echo hits.
    """
    rms = frame_rms_series(
        post_aec_pcm,
        sample_width=sample_width,
        channels=channels,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
    )
    interruptions = sorted(set(int(f) for f in interruption_frames))

    def _near_interruption(frame: int) -> bool:
        return any(abs(frame - it) <= guard_frames for it in interruptions)

    hits: list[dict[str, int]] = []
    for i, value in enumerate(rms):
        if value <= spike_thresh:
            continue
        if _near_interruption(i):
            continue
        hits.append({"frame": i, "rms": int(value)})
    return hits


__all__ = [
    "align_tracks",
    "compute_erle",
    "detect_double_talk",
    "detect_self_echo",
    "frame_rms_series",
]
