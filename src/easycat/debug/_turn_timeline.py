"""Shared per-turn timing rollups for the debugger UI and the journal CLI.

Extracted from ``debugger/server.py`` so the debugger waterfall and the
``easycat bundles show`` / ``easycat inspect`` commands compute per-turn
stage spans from one implementation.  Everything here operates on plain
journal-record dicts (as yielded by ``RunBundle.records()`` or a live
``JournalView``), so it works for ZIP bundles, crash-dump SQLite
journals, and live sessions alike.

Three public entry points:

- :func:`summarise_turns` — per-turn record/stage/error counts
  (the debugger ``/api/turns`` rollup).
- :func:`build_timeline` — per-turn, per-stage span timing
  (the debugger ``/api/timeline`` waterfall).
- :func:`turn_waterfall` — :func:`build_timeline` plus the milestone
  deltas (VAD endpoint → STT final → agent first token → TTS first
  byte) the CLI surfaces as the ``turns`` array in
  ``bundles show --json`` / ``inspect --json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STAGE_ORDER = ("transport", "audio", "vad", "stt", "agent", "tts", "turn", "telephony")

# Milestone journal-record names.  ``vad_stop_speaking`` marks the VAD
# endpoint, ``stt_final`` the committed transcript, ``agent_delta`` (or
# ``agent_final`` for non-streaming agents) the first agent token, and
# ``tts_frame`` / ``tts_audio`` the first synthesized audio bytes.
_VAD_ENDPOINT = "vad_stop_speaking"
_STT_FINAL = "stt_final"
_AGENT_FIRST = ("agent_delta", "agent_final")
_TTS_FIRST = ("tts_frame", "tts_audio")


def record_wall_ns(record: Mapping[str, Any]) -> int | None:
    """Read a record's wall-clock timestamp in nanoseconds.

    Exported ZIP bundles keep the ``JournalRecord`` shape with the
    timestamp nested under ``timing.wall_ns``; crash-dump SQLite
    journals flatten it to a top-level ``wall_ns``.  Read both.
    """
    timing = record.get("timing")
    if isinstance(timing, dict):
        wall = timing.get("wall_ns")
        if isinstance(wall, int):
            return wall
    wall = record.get("wall_ns")
    return wall if isinstance(wall, int) else None


def summarise_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up per-turn timing for the waterfall view."""
    by_turn: dict[str | None, dict[str, Any]] = {}
    order: list[str | None] = []
    for r in records:
        turn_id = r.get("turn_id")
        if turn_id is None:
            continue
        bucket = by_turn.get(turn_id)
        if bucket is None:
            bucket = {
                "turn_id": turn_id,
                "first_sequence": r.get("sequence"),
                "last_sequence": r.get("sequence"),
                "first_wall_ns": None,
                "last_wall_ns": None,
                "stage_counts": {},
                "tts_audio_bytes": 0,
                "stt_audio_bytes": 0,
                "interruption_count": 0,
                "error_count": 0,
                "_interrupt_signal_ids": set(),
            }
            by_turn[turn_id] = bucket
            order.append(turn_id)
        seq = r.get("sequence")
        if seq is not None:
            if bucket["first_sequence"] is None or seq < bucket["first_sequence"]:
                bucket["first_sequence"] = seq
            if bucket["last_sequence"] is None or seq > bucket["last_sequence"]:
                bucket["last_sequence"] = seq
        wall = record_wall_ns(r)
        if wall is not None:
            if bucket["first_wall_ns"] is None or wall < bucket["first_wall_ns"]:
                bucket["first_wall_ns"] = wall
            if bucket["last_wall_ns"] is None or wall > bucket["last_wall_ns"]:
                bucket["last_wall_ns"] = wall
        data = r.get("data") or {}
        if isinstance(data, dict):
            stage = data.get("stage")
            if isinstance(stage, str):
                bucket["stage_counts"][stage] = bucket["stage_counts"].get(stage, 0) + 1
            audio_bytes = data.get("audio_bytes")
            if r.get("name") == "tts_frame" and isinstance(audio_bytes, int):
                bucket["tts_audio_bytes"] += audio_bytes
            if r.get("name") in ("stage_start", "stt_audio_in"):
                if isinstance(audio_bytes, int) and stage == "stt":
                    bucket["stt_audio_bytes"] += audio_bytes
        # A single barge-in fans an InterruptSignal across all stages, so it
        # produces one ``control_signal`` record per stage plus the legacy
        # ``interruption`` event. We bookkeep both here and resolve the
        # deduped count in the post-pass below so record order doesn't affect
        # the result.
        if r.get("name") == "control_signal":
            data = r.get("data") or {}
            if isinstance(data, dict) and data.get("signal_kind") == "interrupt":
                bucket["_interrupt_signal_ids"].add(data.get("signal_id") or "")
        elif r.get("name") == "interruption":
            bucket["_legacy_interruptions"] = bucket.get("_legacy_interruptions", 0) + 1
        if r.get("error"):
            bucket["error_count"] += 1
    rolled: list[dict[str, Any]] = []
    for turn_id in order:
        bucket = by_turn[turn_id]
        # Prefer the deduped signal-id count; fall back to legacy
        # ``interruption`` event count for older bundles.
        signal_count = len(bucket["_interrupt_signal_ids"])
        legacy_count = bucket.pop("_legacy_interruptions", 0)
        bucket["interruption_count"] = signal_count if signal_count else legacy_count
        bucket.pop("_interrupt_signal_ids", None)
        first = bucket["first_wall_ns"]
        last = bucket["last_wall_ns"]
        bucket["wall_ms"] = ((last - first) / 1_000_000) if first and last else None
        rolled.append(bucket)
    return rolled


def build_timeline(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute per-stage spans per turn from stage_start / stage_complete pairs.

    For each stage in each turn, find the first ``stage_start`` and the
    last ``stage_complete`` and report wall-clock + offset relative to
    the turn's earliest record.  This is what the waterfall renders —
    real timing, not just record counts.
    """
    by_turn: dict[str, dict[str, Any]] = {}
    for r in records:
        turn_id = r.get("turn_id")
        if not turn_id:
            continue
        bucket = by_turn.setdefault(
            turn_id,
            {
                "turn_id": turn_id,
                "turn_started_wall_ns": None,
                "turn_ended_wall_ns": None,
                "stages": {},
            },
        )
        wall = record_wall_ns(r)
        if wall is None:
            continue
        if bucket["turn_started_wall_ns"] is None or wall < bucket["turn_started_wall_ns"]:
            bucket["turn_started_wall_ns"] = wall
        if bucket["turn_ended_wall_ns"] is None or wall > bucket["turn_ended_wall_ns"]:
            bucket["turn_ended_wall_ns"] = wall
        data = r.get("data") or {}
        if not isinstance(data, dict):
            continue
        stage = data.get("stage") or data.get("observed_stage")
        if not isinstance(stage, str):
            continue
        name = r.get("name")
        # Skip ``control_signal`` records when computing stage spans.
        # Interrupts fan out across all stages, so each stage gets a
        # ``control_signal`` with ``observed_stage`` set even when that stage
        # had no real pipeline activity. Counting those here would render a
        # synthetic instant-span for stages the turn never actually touched.
        # ``stage_counts`` in ``summarise_turns`` still accounts for the
        # signals.
        if name == "control_signal":
            continue
        slot = bucket["stages"].setdefault(
            stage,
            {"stage": stage, "first_wall_ns": None, "last_wall_ns": None, "record_count": 0},
        )
        slot["record_count"] += 1
        if slot["first_wall_ns"] is None or wall < slot["first_wall_ns"]:
            slot["first_wall_ns"] = wall
        if slot["last_wall_ns"] is None or wall > slot["last_wall_ns"]:
            slot["last_wall_ns"] = wall
        if name == "stage_start" and (
            slot.get("started_wall_ns") is None or wall < slot["started_wall_ns"]
        ):
            slot["started_wall_ns"] = wall
        if name == "stage_complete" and (
            slot.get("completed_wall_ns") is None or wall > slot["completed_wall_ns"]
        ):
            slot["completed_wall_ns"] = wall

    timeline: list[dict[str, Any]] = []
    for turn_id, bucket in by_turn.items():
        turn_start = bucket["turn_started_wall_ns"] or 0
        turn_wall_ms = (
            (bucket["turn_ended_wall_ns"] - turn_start) / 1_000_000
            if bucket["turn_ended_wall_ns"]
            else 0
        )
        spans: list[dict[str, Any]] = []
        for stage_name in STAGE_ORDER:
            slot = bucket["stages"].get(stage_name)
            if slot is None:
                continue
            start_ns = slot.get("started_wall_ns") or slot["first_wall_ns"]
            end_ns = slot.get("completed_wall_ns") or slot["last_wall_ns"]
            if start_ns is None or end_ns is None:
                continue
            span_ms = max(0.0, (end_ns - start_ns) / 1_000_000)
            offset_ms = max(0.0, (start_ns - turn_start) / 1_000_000)
            spans.append(
                {
                    "stage": stage_name,
                    "offset_ms": offset_ms,
                    "duration_ms": span_ms,
                    "record_count": slot["record_count"],
                }
            )
        timeline.append(
            {
                "turn_id": turn_id,
                "wall_ms": turn_wall_ms,
                "spans": spans,
            }
        )
    return timeline


def _delta_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    if start_ns is None or end_ns is None:
        return None
    return (end_ns - start_ns) / 1_000_000


def turn_milestones(records: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Compute per-turn milestone deltas from existing journal records.

    The chain is VAD endpoint → STT final → agent first token → TTS
    first byte.  Each delta is ``None`` when either endpoint is missing
    (text-only turns have no VAD endpoint; failed turns may never reach
    TTS).  The VAD endpoint is the *last* ``vad_stop_speaking`` before
    the turn's first ``stt_final``, since brief pauses can emit several
    VAD stops within one turn.
    """
    state: dict[str, dict[str, int | None]] = {}
    for r in records:
        turn_id = r.get("turn_id")
        if not turn_id:
            continue
        wall = record_wall_ns(r)
        if wall is None:
            continue
        name = r.get("name")
        slot = state.setdefault(
            turn_id,
            {"vad_endpoint": None, "stt_final": None, "agent_first": None, "tts_first": None},
        )
        if name == _VAD_ENDPOINT and slot["stt_final"] is None:
            slot["vad_endpoint"] = wall
        elif name == _STT_FINAL and slot["stt_final"] is None:
            slot["stt_final"] = wall
        elif name in _AGENT_FIRST and slot["agent_first"] is None:
            slot["agent_first"] = wall
        elif name in _TTS_FIRST and slot["tts_first"] is None:
            slot["tts_first"] = wall

    milestones: dict[str, dict[str, float | None]] = {}
    for turn_id, slot in state.items():
        milestones[turn_id] = {
            "vad_endpoint_to_stt_final_ms": _delta_ms(slot["vad_endpoint"], slot["stt_final"]),
            "stt_final_to_agent_first_token_ms": _delta_ms(slot["stt_final"], slot["agent_first"]),
            "agent_first_token_to_tts_first_byte_ms": _delta_ms(
                slot["agent_first"], slot["tts_first"]
            ),
            "vad_endpoint_to_tts_first_byte_ms": _delta_ms(
                slot["vad_endpoint"], slot["tts_first"]
            ),
        }
    return milestones


def turn_waterfall(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-turn waterfall: stage spans plus milestone deltas.

    This is the ``turns`` array surfaced by ``easycat bundles show
    --json`` / ``easycat inspect --json``.  Each entry carries the
    turn's wall-clock duration, its per-stage spans (offset + duration
    relative to the turn start), and the milestone deltas answering
    "where did the time go?" without opening the debugger UI.
    """
    milestones = turn_milestones(records)
    empty: dict[str, float | None] = {
        "vad_endpoint_to_stt_final_ms": None,
        "stt_final_to_agent_first_token_ms": None,
        "agent_first_token_to_tts_first_byte_ms": None,
        "vad_endpoint_to_tts_first_byte_ms": None,
    }
    turns = build_timeline(records)
    for turn in turns:
        turn["milestones"] = milestones.get(turn["turn_id"], dict(empty))
    return turns


__all__ = [
    "STAGE_ORDER",
    "build_timeline",
    "record_wall_ns",
    "summarise_turns",
    "turn_milestones",
    "turn_waterfall",
]
