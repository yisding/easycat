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
  deltas (VAD endpoint → STT final → agent request → agent first token
  → TTS first byte) the CLI surfaces as the ``turns`` array in
  ``bundles show --json`` / ``inspect --json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from easycat.runtime.records import (
    AGENT_DELTA_RECORD_NAME,
    AGENT_FINAL_RECORD_NAME,
    AGENT_REQUEST_STARTED_RECORD_NAME,
    BOT_STARTED_SPEAKING_RECORD_NAME,
    BOT_STOPPED_SPEAKING_RECORD_NAME,
    CONTROL_SIGNAL_RECORD_NAME,
    INTERRUPTION_RECORD_NAME,
    PLAYBACK_MARK_ACK_RECORD_NAME,
    STAGE_COMPLETE_RECORD_NAME,
    STAGE_START_RECORD_NAME,
    STT_FINAL_RECORD_NAME,
    TTS_FRAME_RECORD_NAME,
    VAD_START_SPEAKING_RECORD_NAME,
)

STAGE_ORDER = ("transport", "audio", "vad", "stt", "agent", "tts", "turn", "telephony")

# Milestone journal-record names.  ``vad_stop_speaking`` marks the VAD
# endpoint, ``stt_final`` the committed transcript, ``agent_request_started``
# the moment the agent run is dispatched (request queueing/setup), ``agent_delta``
# (or ``agent_final`` for non-streaming agents) the first agent token, and
# ``tts_frame`` / ``tts_audio`` the first synthesized audio bytes.  Splitting at
# the agent request lets us separate dispatch overhead from raw LLM TTFT.
_VAD_ENDPOINT = "vad_stop_speaking"
_STT_FINAL = STT_FINAL_RECORD_NAME
_AGENT_REQUEST = AGENT_REQUEST_STARTED_RECORD_NAME
_AGENT_FIRST = (AGENT_DELTA_RECORD_NAME, AGENT_FINAL_RECORD_NAME)
_TTS_FIRST = (TTS_FRAME_RECORD_NAME, "tts_audio")

# Barge-in milestone record names.  ``bot_started_speaking`` opens a playback
# window the user can interrupt; the FIRST ``vad_start_speaking`` at/after that
# is the user starting to barge in, and the FIRST ``bot_stopped_speaking`` /
# ``playback_mark_ack`` after the barge-in is the bot actually going quiet.
_BOT_STARTED = BOT_STARTED_SPEAKING_RECORD_NAME
_USER_SPEECH_START = VAD_START_SPEAKING_RECORD_NAME
_BOT_STOPPED = (BOT_STOPPED_SPEAKING_RECORD_NAME, PLAYBACK_MARK_ACK_RECORD_NAME)


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


def safe_turn_id(value: Any) -> str | None:
    """Return a valid journal turn id or ``None`` for malformed input.

    Debug bundles can come from untrusted sources and are decoded from raw
    JSON.  JSON arrays and objects are unhashable in Python, so never use a
    raw ``turn_id`` value as a dictionary key before validating its shape.
    Runtime-generated turn ids are non-empty strings; malformed or missing
    values are ignored by timeline rollups.
    """
    if isinstance(value, str) and value:
        return value
    return None


def _safe_sequence(value: Any) -> int | None:
    """Return a comparable journal sequence, ignoring malformed values."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def summarise_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up per-turn timing for the waterfall view."""
    by_turn: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in records:
        turn_id = safe_turn_id(r.get("turn_id"))
        if turn_id is None:
            continue
        seq = _safe_sequence(r.get("sequence"))
        bucket = by_turn.get(turn_id)
        if bucket is None:
            bucket = {
                "turn_id": turn_id,
                "first_sequence": seq,
                "last_sequence": seq,
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
            if r.get("name") == TTS_FRAME_RECORD_NAME and isinstance(audio_bytes, int):
                bucket["tts_audio_bytes"] += audio_bytes
            if r.get("name") in (STAGE_START_RECORD_NAME, "stt_audio_in"):  # noqa: SIM102 nested branches preserve decision context
                if isinstance(audio_bytes, int) and stage == "stt":
                    bucket["stt_audio_bytes"] += audio_bytes
        # A single barge-in fans an InterruptSignal across all stages, so it
        # produces one ``control_signal`` record per stage plus the legacy
        # ``interruption`` event. We bookkeep both here and resolve the
        # deduped count in the post-pass below so record order doesn't affect
        # the result.
        if r.get("name") == CONTROL_SIGNAL_RECORD_NAME:
            data = r.get("data") or {}
            if isinstance(data, dict) and data.get("signal_kind") == "interrupt":
                signal_id = data.get("signal_id")
                bucket["_interrupt_signal_ids"].add(
                    signal_id if isinstance(signal_id, str) else ""
                )
        elif r.get("name") == INTERRUPTION_RECORD_NAME:
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
        turn_id = safe_turn_id(r.get("turn_id"))
        if turn_id is None:
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
        if name == CONTROL_SIGNAL_RECORD_NAME:
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
        if name == STAGE_START_RECORD_NAME and (
            slot.get("started_wall_ns") is None or wall < slot["started_wall_ns"]
        ):
            slot["started_wall_ns"] = wall
        if name == STAGE_COMPLETE_RECORD_NAME and (
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
    """Milestone delta in ms, or ``None`` when it is not a measurable latency.

    A backward wall-clock step between two records of the same turn (an NTP
    correction, a VM or laptop suspend/resume) makes ``end_ns < start_ns``.
    A negative latency is meaningless, and every consumer already handles
    ``None`` — ``LatencyPercentileStats.from_values`` drops it, the per-turn
    table renders it blank — whereas an unclamped negative reached
    ``from_values`` and aborted the whole ``easycat latency`` summary with a
    raw ``ValueError`` traceback (gh 1106).

    ``None`` rather than ``build_timeline``'s ``max(0.0, ...)`` clamp on
    purpose: a visual span needs a number to draw, but a reported measurement
    should say "not measurable" instead of inventing a zero.
    """
    if start_ns is None or end_ns is None:
        return None
    if end_ns < start_ns:
        return None
    return (end_ns - start_ns) / 1_000_000


def turn_milestones(records: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Compute per-turn milestone deltas from existing journal records.

    The chain is VAD endpoint → STT final → agent request → agent first
    token → TTS first byte.  The ``stt_final`` → ``agent_request_started``
    delta is dispatch/queueing overhead; ``agent_request_started`` → first
    agent token is the raw LLM time-to-first-token (TTFT).  Each delta is
    ``None`` when either endpoint is missing (text-only turns have no VAD
    endpoint; failed turns may never reach TTS).  The VAD endpoint is the
    *last* ``vad_stop_speaking`` before the turn's first ``stt_final``,
    since brief pauses can emit several VAD stops within one turn.
    """
    state: dict[str, dict[str, int | None]] = {}
    # Per-turn ``(wall_ns, name)`` pairs for the barge-in scan, which depends on
    # wall-clock ordering rather than the single-pass FSM the response chain
    # uses.  Records can arrive out of wall order across backends, so we sort
    # each turn's barge-in markers before walking them.
    barge_records: dict[str, list[tuple[int, str]]] = {}
    for r in records:
        turn_id = safe_turn_id(r.get("turn_id"))
        if turn_id is None:
            continue
        wall = record_wall_ns(r)
        if wall is None:
            continue
        name = r.get("name")
        slot = state.setdefault(
            turn_id,
            {
                "vad_endpoint": None,
                "stt_final": None,
                "agent_request": None,
                "agent_first": None,
                "tts_first": None,
                "user_speech_start": None,
                "bot_stopped": None,
            },
        )
        if name == _VAD_ENDPOINT and slot["stt_final"] is None:
            slot["vad_endpoint"] = wall
        elif name == _STT_FINAL and slot["stt_final"] is None:
            slot["stt_final"] = wall
        elif name == _AGENT_REQUEST and slot["agent_request"] is None:
            slot["agent_request"] = wall
        elif name in _AGENT_FIRST and slot["agent_first"] is None:
            slot["agent_first"] = wall
        elif name in _TTS_FIRST and slot["tts_first"] is None:
            slot["tts_first"] = wall
        if name == _BOT_STARTED or name == _USER_SPEECH_START or name in _BOT_STOPPED:
            barge_records.setdefault(turn_id, []).append((wall, name if name else ""))

    for turn_id, pairs in barge_records.items():
        slot = state[turn_id]
        user_speech_start, bot_stopped = _barge_in_walls(pairs)
        slot["user_speech_start"] = user_speech_start
        slot["bot_stopped"] = bot_stopped

    milestones: dict[str, dict[str, float | None]] = {}
    for turn_id, slot in state.items():
        milestones[turn_id] = {
            "vad_endpoint_to_stt_final_ms": _delta_ms(slot["vad_endpoint"], slot["stt_final"]),
            "stt_final_to_agent_request_ms": _delta_ms(slot["stt_final"], slot["agent_request"]),
            "agent_request_to_first_token_ms": _delta_ms(
                slot["agent_request"], slot["agent_first"]
            ),
            "agent_first_token_to_tts_first_byte_ms": _delta_ms(
                slot["agent_first"], slot["tts_first"]
            ),
            "vad_endpoint_to_tts_first_byte_ms": _delta_ms(
                slot["vad_endpoint"], slot["tts_first"]
            ),
            "user_speech_start_to_bot_stopped_ms": _delta_ms(
                slot["user_speech_start"], slot["bot_stopped"]
            ),
        }
    return milestones


def _barge_in_walls(pairs: list[tuple[int, str]]) -> tuple[int | None, int | None]:
    """Find the barge-in user-speech-start and bot-stopped walls for one turn.

    Pure wall-clock ordering: the FIRST ``vad_start_speaking`` at/after a
    ``bot_started_speaking`` is the user starting to barge in, and the FIRST
    ``bot_stopped_speaking`` / ``playback_mark_ack`` strictly after that is the
    bot going quiet.  Returns ``(None, None)`` when the turn never opened a
    playback window or the user never spoke into it.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0])
    bot_speaking = False
    user_speech_start: int | None = None
    for wall, name in ordered:
        if name == _BOT_STARTED:
            bot_speaking = True
        elif name == _USER_SPEECH_START and bot_speaking and user_speech_start is None:
            user_speech_start = wall
        elif name in _BOT_STOPPED and user_speech_start is not None:
            return user_speech_start, wall
    return user_speech_start, None


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
        "stt_final_to_agent_request_ms": None,
        "agent_request_to_first_token_ms": None,
        "agent_first_token_to_tts_first_byte_ms": None,
        "vad_endpoint_to_tts_first_byte_ms": None,
        "user_speech_start_to_bot_stopped_ms": None,
    }
    # Per-turn deduped interruption counts ride alongside the milestones as a
    # TOP-LEVEL turn key (never under ``milestones``): the milestone-key-set
    # guard inspects only ``turn['milestones']`` and would trip on an extra key.
    interruptions = {
        turn["turn_id"]: turn.get("interruption_count", 0) for turn in summarise_turns(records)
    }
    turns = build_timeline(records)
    for turn in turns:
        turn["milestones"] = milestones.get(turn["turn_id"], dict(empty))
        turn["interruption_count"] = interruptions.get(turn["turn_id"], 0)
    return turns


def extract_turn_transcripts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull per-turn user transcripts and agent responses out of the journal.

    The debugger transcript panel and the two-source ``easycat diff`` both
    render this, so the projection lives here (dependency-free) rather than in
    the aiohttp-optional ``debugger/server.py``.  Sources:

    - User text: ``stt_final`` event records (``data.text`` / ``data.transcript``).
    - Agent reply: AgentStage ``stage_complete`` records (``data.response``) for
      the basic path; replayed ``agent_delta`` ``TEXT_DELTA`` / ``TEXT_REPLACE``
      records for the streaming path; ``agent_final`` ``data.text`` as the
      non-streaming fallback.

    Each entry keeps the first sequence each side was observed at so callers can
    deep-link to the originating record.
    """
    by_turn: dict[str, dict[str, Any]] = {}
    for r in records:
        turn_id = safe_turn_id(r.get("turn_id"))
        if turn_id is None:
            continue
        bucket = by_turn.setdefault(
            turn_id,
            {
                "turn_id": turn_id,
                "user": "",
                "agent": "",
                "user_seq": None,
                "agent_seq": None,
                "agent_delta_flat": [],
                "agent_delta_parts": {},
                "agent_delta_seq": None,
            },
        )
        name = r.get("name") or ""
        data = r.get("data") or {}
        seq = r.get("sequence")
        if not isinstance(data, dict):
            continue
        if name == STT_FINAL_RECORD_NAME:
            txt = data.get("text") or data.get("transcript")
            if isinstance(txt, str) and txt:
                bucket["user"] = txt
                bucket["user_seq"] = seq
        elif name == STAGE_COMPLETE_RECORD_NAME and (
            data.get("stage") == "agent" or data.get("observed_stage") == "agent"
        ):
            resp = data.get("response")
            if isinstance(resp, str) and resp:
                bucket["agent"] = resp
                bucket["agent_seq"] = seq
        elif name == AGENT_DELTA_RECORD_NAME:
            _fold_agent_delta_record(bucket, data, seq)
        elif name == "agent_final":
            txt = data.get("text")
            if isinstance(txt, str) and txt and not bucket["agent"]:
                bucket["agent"] = txt
                bucket["agent_seq"] = seq

    transcripts = []
    for turn_id, bucket in by_turn.items():
        if not bucket["agent"]:
            delta_text = _joined_agent_delta_text(bucket)
            if delta_text:
                bucket["agent"] = delta_text
                if bucket["agent_seq"] is None:
                    bucket["agent_seq"] = bucket["agent_delta_seq"]
        bucket.pop("agent_delta_flat", None)
        bucket.pop("agent_delta_parts", None)
        bucket.pop("agent_delta_seq", None)
        transcripts.append(bucket)
    return transcripts


def _fold_agent_delta_record(bucket: dict[str, Any], data: dict[str, Any], seq: Any) -> None:
    """Fold one streamed ``agent_delta`` record into the fallback transcript.

    Mirrors ``AgentTextStream``: an indexed ``TEXT_REPLACE`` overwrites its
    part, an indexed ``TEXT_DELTA`` appends to its part, and records without
    ``part_index`` stay flat appends.
    """
    record_type = data.get("type")
    txt = data.get("text")
    if record_type not in ("TEXT_DELTA", "TEXT_REPLACE") or not isinstance(txt, str):
        return
    part_index = data.get("part_index")
    if isinstance(part_index, int) and not isinstance(part_index, bool):
        parts: dict[int, str] = bucket["agent_delta_parts"]
        if record_type == "TEXT_REPLACE":
            parts[part_index] = txt
        elif txt:
            parts[part_index] = parts.get(part_index, "") + txt
        else:
            return
    elif record_type == "TEXT_DELTA" and txt:
        bucket["agent_delta_flat"].append(txt)
    else:
        return
    if bucket["agent_delta_seq"] is None:
        bucket["agent_delta_seq"] = seq


def _joined_agent_delta_text(bucket: dict[str, Any]) -> str:
    parts: dict[int, str] = bucket["agent_delta_parts"]
    indexed = "".join(parts[index] for index in sorted(parts))
    return "".join(bucket["agent_delta_flat"]) + indexed


__all__ = [
    "STAGE_ORDER",
    "build_timeline",
    "extract_turn_transcripts",
    "record_wall_ns",
    "safe_turn_id",
    "summarise_turns",
    "turn_milestones",
    "turn_waterfall",
]
