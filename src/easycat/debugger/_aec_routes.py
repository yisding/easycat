"""AEC diagnostics and VAD what-if payload builders for the debugger.

Pure, aiohttp-free route helpers split out of :mod:`easycat.debugger.server`
(QS3): the per-turn AEC diagnostics payload (ERLE / double-talk / self-echo /
truncation-bounded tracks) built on top of :mod:`easycat.debugger._aec`, plus
the VAD what-if input collection. The heavy pure-stdlib signal math lives in
``_aec.py``; this module aligns it to journal records and shapes the JSON.

``server.py`` re-exports every name here so the historical
``from easycat.debugger.server import _helper`` import sites keep resolving.
"""

from __future__ import annotations

from typing import Any

from easycat.debug._pcm import is_supported_width as _is_supported_width
from easycat.debugger._aec import align_tracks as _align_aec_tracks
from easycat.debugger._aec import compute_erle as _compute_erle
from easycat.debugger._aec import detect_double_talk as _detect_double_talk
from easycat.debugger._aec import detect_self_echo as _detect_self_echo
from easycat.debugger._aec import frame_rms_series as _frame_rms_series
from easycat.debugger._sources import DebuggerSource

# Default decode geometry used when the journal frames carry no explicit
# PCM format fields (debugger-internal fixtures, malformed captures).
_AEC_DEFAULT_FMT = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
_AEC_FRAME_MS = 20
# Per-request cap for AEC diagnostics PCM processed per track. The debugger
# may open untrusted bundles with hundreds of MB of artifacts, and the pure
# Python diagnostics expand PCM into arrays/lists. Keep the analysis bounded
# and report truncation instead of allocating unbounded joined tracks.
_AEC_MAX_TRACK_BYTES = 8 * 1024 * 1024


def _aec_track_format(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Read the PCM geometry from the first aligned frame (defaulted)."""
    fmt = dict(_AEC_DEFAULT_FMT)
    if entries:
        data = entries[0].get("data") or {}
        if isinstance(data, dict):
            for key in ("sample_rate", "channels", "sample_width"):
                value = data.get(key)
                if isinstance(value, int) and value > 0:
                    fmt[key] = value
    return fmt


def _aec_interruption_frames(
    records: list[dict[str, Any]],
    turn_id: str,
    post_aec: list[dict[str, Any]],
    *,
    frame_ms: int,
    fmt: dict[str, int],
) -> list[int]:
    """Map this turn's interruption records onto post-AEC frame indices.

    Each ``assistant_interruption_notified`` (or a ``turn_state_changed``
    transition *into* ``user_speaking`` while the bot was speaking) is placed at
    the frame whose monotonic timestamp it most closely follows, so self-echo
    detection can tell a true barge-in from the bot hearing itself.

    ``post_aec`` must be the FULL (untruncated) track: ``total_frames`` and the
    clamp ceiling are derived from it so a barge-in beyond the analyzed prefix
    keeps its true frame index instead of collapsing onto the prefix tail.
    """
    if not post_aec:
        return []
    base_ns = post_aec[0]["mono_ns"]
    frame_span_ns = max(1, frame_ms) * 1_000_000
    bytes_per_sample = max(1, int(fmt.get("sample_width") or 2)) * max(
        1, int(fmt.get("channels") or 1)
    )
    samples_per_frame = max(1, int(int(fmt.get("sample_rate") or 16000) * frame_ms / 1000))
    bytes_per_frame = max(1, bytes_per_sample * samples_per_frame)
    total_frames = 0
    for entry in post_aec:
        total_frames += max(1, (len(entry["pcm"]) + bytes_per_frame - 1) // bytes_per_frame)
    frames: list[int] = []
    for record in records:
        if record.get("turn_id") != turn_id:
            continue
        name = record.get("name")
        if name == "assistant_interruption_notified":
            pass
        elif name == "turn_state_changed":
            data = record.get("data") or {}
            if not (isinstance(data, dict) and data.get("to") == "user_speaking"):
                continue
        else:
            continue
        timing = record.get("timing")
        mono_ns = timing.get("mono_ns") if isinstance(timing, dict) else None
        if not isinstance(mono_ns, int):
            continue
        frame = max(0, (mono_ns - base_ns) // frame_span_ns)
        frames.append(min(int(frame), max(0, total_frames - 1)))
    return frames


def _limit_aec_track(
    entries: list[dict[str, Any]], max_bytes: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """Return track entries whose PCM totals no more than ``max_bytes``.

    Entries are shallow-copied only when their PCM blob must be clipped. This
    avoids unbounded concatenation/decoding while preserving the existing
    diagnostics shape for normal-sized turns.
    """
    total_bytes = sum(len(entry["pcm"]) for entry in entries)
    if total_bytes <= max_bytes:
        return entries, total_bytes, False
    remaining = max(0, max_bytes)
    limited: list[dict[str, Any]] = []
    for entry in entries:
        if remaining <= 0:
            break
        pcm = entry["pcm"]
        if len(pcm) <= remaining:
            limited.append(entry)
            remaining -= len(pcm)
            continue
        clipped = dict(entry)
        clipped["pcm"] = pcm[:remaining]
        limited.append(clipped)
        remaining = 0
    return limited, total_bytes, True


def _aec_diagnostics_for_turn(source: DebuggerSource, turn_id: str) -> dict[str, Any]:
    """Build the AEC diagnostics payload for one turn.

    Aligns mic-in / reference / post-AEC tracks on ``timing.mono_ns`` and
    derives ERLE, double-talk bands, and self-echo hits.  Degrades gracefully:
    a turn with no captured reference returns ``has_reference: False`` and empty
    diagnostics rather than raising.
    """
    records = source.records()
    tracks = _align_aec_tracks(records, source=source, turn_id=turn_id)
    mic_in_all = tracks["mic_in"]
    reference_all = tracks["reference"]
    post_aec_all = tracks["post_aec"]
    has_reference = bool(reference_all)

    fmt = _aec_track_format(post_aec_all or mic_in_all)
    # 8-bit mu-law (sample_width == 1) can't be linearly decoded for the energy
    # math below; surface a clear unsupported result rather than mis-decoded
    # garbage ERLE/self-echo numbers.
    if not _is_supported_width(fmt["sample_width"]):
        return {
            "turn_id": turn_id,
            "has_reference": has_reference,
            "unsupported": True,
            "reason": (
                "unsupported audio format for AEC diagnostics: "
                f"sample_width={fmt['sample_width']} "
                "(8-bit/mu-law telephony audio is not decodable here)"
            ),
            "format": fmt,
            "tracks": {
                "mic_in": {"frame_count": len(mic_in_all)},
                "reference": {"frame_count": len(reference_all)},
                "post_aec": {"frame_count": len(post_aec_all)},
            },
        }
    frame_ms = _AEC_FRAME_MS
    mic_in, mic_total_bytes, mic_truncated = _limit_aec_track(mic_in_all, _AEC_MAX_TRACK_BYTES)
    reference, ref_total_bytes, ref_truncated = _limit_aec_track(
        reference_all, _AEC_MAX_TRACK_BYTES
    )
    post_aec, post_total_bytes, post_truncated = _limit_aec_track(
        post_aec_all, _AEC_MAX_TRACK_BYTES
    )
    mic_pcm = b"".join(entry["pcm"] for entry in mic_in)
    ref_pcm = b"".join(entry["pcm"] for entry in reference)
    post_pcm = b"".join(entry["pcm"] for entry in post_aec)
    diagnostics_truncated = mic_truncated or ref_truncated or post_truncated

    erle = _compute_erle(
        mic_pcm,
        post_pcm,
        sample_width=fmt["sample_width"],
        channels=fmt["channels"],
        sample_rate=fmt["sample_rate"],
        frame_ms=frame_ms,
    )
    mic_rms = _frame_rms_series(
        mic_pcm,
        sample_width=fmt["sample_width"],
        channels=fmt["channels"],
        sample_rate=fmt["sample_rate"],
        frame_ms=frame_ms,
    )
    ref_rms = _frame_rms_series(
        ref_pcm,
        sample_width=fmt["sample_width"],
        channels=fmt["channels"],
        sample_rate=fmt["sample_rate"],
        frame_ms=frame_ms,
    )
    double_talk = _detect_double_talk(ref_rms, mic_rms)
    # Frame interruptions against the FULL (untruncated) post-AEC track. Passing
    # the clipped prefix would shrink ``total_frames`` and clamp a real barge-in
    # that lands after the prefix onto the last analyzed frame, planting a
    # phantom guard window that suppresses genuine self-echo in the prefix tail.
    # Only ``len(pcm)`` is read here (no decode/join), so the memory bound holds.
    interruption_frames = _aec_interruption_frames(
        records, turn_id, post_aec_all, frame_ms=frame_ms, fmt=fmt
    )
    self_echo = _detect_self_echo(
        post_pcm,
        interruption_frames,
        sample_width=fmt["sample_width"],
        channels=fmt["channels"],
        sample_rate=fmt["sample_rate"],
        frame_ms=frame_ms,
    )
    return {
        "turn_id": turn_id,
        "has_reference": has_reference,
        "frame_ms": frame_ms,
        "format": fmt,
        "erle": erle,
        "double_talk": double_talk,
        "self_echo": self_echo,
        "interruption_frames": interruption_frames,
        "diagnostics_truncated": diagnostics_truncated,
        "max_track_bytes": _AEC_MAX_TRACK_BYTES,
        "tracks": {
            "mic_in": {
                "frame_count": len(mic_in_all),
                "analyzed_frame_count": len(mic_in),
                "byte_count": mic_total_bytes,
                "analyzed_byte_count": len(mic_pcm),
                "truncated": mic_truncated,
            },
            "reference": {
                "frame_count": len(reference_all),
                "analyzed_frame_count": len(reference),
                "byte_count": ref_total_bytes,
                "analyzed_byte_count": len(ref_pcm),
                "truncated": ref_truncated,
            },
            "post_aec": {
                "frame_count": len(post_aec_all),
                "analyzed_frame_count": len(post_aec),
                "byte_count": post_total_bytes,
                "analyzed_byte_count": len(post_pcm),
                "truncated": post_truncated,
            },
        },
    }


def _vad_baseline_start_count(records: list[dict[str, Any]], turn_id: str) -> int:
    """Count the ``VADStartSpeaking`` events the live VAD emitted for a turn.

    Reads the recorded VAD ``stage_complete`` event descriptors so the what-if
    delta compares against what actually happened, not a re-run of the live
    threshold.
    """
    count = 0
    for record in records:
        if record.get("turn_id") != turn_id or record.get("name") != "stage_complete":
            continue
        data = record.get("data") or {}
        if not isinstance(data, dict) or data.get("stage") != "vad":
            continue
        for event in data.get("events") or []:
            if isinstance(event, dict) and event.get("type") == "VADStartSpeaking":
                count += 1
    return count


def _vad_whatif_frames(source: DebuggerSource, turn_id: str) -> list[bytes]:
    """Return the turn's raw VAD ``stage_start`` input PCM blobs, in order.

    These are the pre-mono mic frames captured before the VAD provider ran, so
    the what-if re-drives a fresh provider against the same input the live run
    saw.
    """
    from easycat.debugger.server import _record_sequence

    frames: list[tuple[int, bytes]] = []
    for record in source.records():
        if record.get("turn_id") != turn_id or record.get("name") != "stage_start":
            continue
        data = record.get("data") or {}
        if not isinstance(data, dict) or data.get("stage") != "vad":
            continue
        ref = record.get("input_ref")
        if not ref:
            continue
        blob = source.artifact(ref)
        if blob is None:
            continue
        seq = _record_sequence(record)
        if seq is None:
            continue
        frames.append((seq, blob))
    frames.sort(key=lambda item: item[0])
    return [blob for _seq, blob in frames]


__all__ = [
    "_AEC_MAX_TRACK_BYTES",
    "_aec_diagnostics_for_turn",
    "_aec_interruption_frames",
    "_aec_track_format",
    "_limit_aec_track",
    "_vad_baseline_start_count",
    "_vad_whatif_frames",
]
