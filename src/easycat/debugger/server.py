"""aiohttp-backed debugger server.

Adapts a :class:`DebuggerSource` (a bundle on disk, an in-memory
:class:`RunBundle`, or a live :class:`Session`) into a JSON HTTP API,
WebSocket push channel, and single-page HTML UI rendering the
timeline, per-stage waterfall, pipeline graph, transcript, audio
playback, replay surface, cost rollup, and bundle export.

Routes:

- ``GET  /``                          — static HTML page
- ``GET  /api/manifest``              — bundle/session metadata
- ``GET  /api/records``               — journal records (filterable)
- ``GET  /api/turns``                 — per-turn rollup with stage counts
- ``GET  /api/timeline``              — per-stage span timing per turn
- ``GET  /api/transcript``            — extracted user/agent text per turn
- ``GET  /api/cost``                  — cost rollup (degrades to zero)
- ``GET  /api/artifact/<ref>``        — raw artifact bytes (audio chunks)
- ``GET  /api/audio/concat/<turn>``   — concatenated WAV for one turn
- ``POST /api/replay``                — run replay against the source
- ``POST /api/export``                — export the source as a bundle ZIP
- ``GET  /ws``                        — WebSocket push for live updates
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import struct
import threading
import wave
import webbrowser
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easycat.debug.bundle import RunBundle

logger = logging.getLogger(__name__)


_SHA256_REF = re.compile(r"^[a-f0-9]{64}$")
_TURN_ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Hard cap on frames returned in /api/replay so a 50k-record bundle can't
# blow the response past sane sizes. The cap is generous: typical voice
# bundles run a few thousand records, and a per-frame `data` dict is
# small. UI surfaces `frames_truncated` + `total_frames` when this fires.
_REPLAY_FRAME_LIMIT = 5000


def _safe_ref(ref: str) -> str:
    """Reject anything that isn't a SHA-256 hex digest before any I/O.

    Without this guard, the ``{ref}`` route matcher would happily accept
    URL-encoded path traversal sequences and pass them straight to the
    filesystem artifact store.
    """
    if not _SHA256_REF.match(ref):
        raise ValueError(f"invalid artifact ref: {ref!r}")
    return ref


def _safe_turn_id(turn_id: str) -> str:
    if not _TURN_ID_OK.match(turn_id):
        raise ValueError(f"invalid turn_id: {turn_id!r}")
    return turn_id


# ── Source adaptation ────────────────────────────────────────────


@dataclass
class DebuggerSource:
    """Adapts heterogeneous data sources into one interface for the UI.

    ``records`` returns the latest snapshot of journal records.  Bundle
    sources cache because bundles are immutable; live sources re-snapshot
    every call so WebSocket polling surfaces new events.

    ``artifact`` resolves a content-addressed ref to bytes.  ``manifest``
    returns a small dict the UI shows in the header — the path field is
    stripped to a basename to avoid leaking absolute paths into the
    browser.
    """

    label: str
    _records_fn: Any = field(repr=False)
    _artifact_fn: Any = field(repr=False)
    _manifest_fn: Any = field(repr=False)
    _bundle_fn: Any | None = field(default=None, repr=False)
    _replay_fn: Any | None = field(default=None, repr=False)
    _progress_fn: Any | None = field(default=None, repr=False)
    is_live: bool = False

    def records(self) -> list[dict[str, Any]]:
        return list(self._records_fn())

    def progress(self) -> tuple[int, int]:
        """Cheap ``(latest_sequence, record_count)`` without serializing.

        Used by the live WebSocket loop to detect journal growth in O(1)
        instead of re-reading and re-serializing every record each tick.
        ``latest_sequence`` is the monotonic change-detection key; the
        count is the value shown in the UI header.  Falls back to the
        ``records()`` length when a source has no cheap accessor (so the
        contract holds for every source).
        """
        if self._progress_fn is not None:
            return self._progress_fn()
        n = len(self.records())
        return (n, n)

    def artifact(self, ref: str) -> bytes | None:
        return self._artifact_fn(ref)

    def manifest(self) -> dict[str, Any]:
        return dict(self._manifest_fn())

    def bundle(self) -> RunBundle | None:
        return self._bundle_fn() if self._bundle_fn is not None else None

    def replay(self, **kwargs: Any) -> Any:
        if self._replay_fn is None:
            raise RuntimeError("This source does not support replay.")
        return self._replay_fn(**kwargs)


def _serialize_frame(frame: Any) -> dict[str, Any]:
    """Project a :class:`ReplayFrame` into JSON-safe shape for the wire.

    The raw frame carries ``input_blob`` / ``output_blob`` as ``bytes``,
    which can't go through ``json.dumps``.  We strip the bytes and expose
    the SHA-256 refs instead — the UI fetches blobs on demand from
    ``/api/artifact/{ref}``.  Sizes are surfaced separately so the UI can
    show a badge without paying the round-trip.
    """
    return {
        "sequence": frame.sequence,
        "stage": frame.stage,
        "kind": frame.kind,
        "name": frame.name,
        "turn_id": frame.turn_id,
        "data": frame.data,
        "input_ref": frame.input_ref,
        "output_ref": frame.output_ref,
        "input_blob_size": len(frame.input_blob) if frame.input_blob else 0,
        "output_blob_size": len(frame.output_blob) if frame.output_blob else 0,
        "error": frame.error,
        "side_effecting": frame.side_effecting,
    }


def _validated_replay_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Type-check and normalise the optional windowing/filter keys.

    Raises :class:`ValueError` on bad input so the handler maps it to a
    400 with a structured ``BAD_REQUEST`` error_code.  Unknown stage
    names are rejected here rather than silently ignored — a typo in a
    UI checkbox should surface, not produce surprising frame slices.
    """
    from easycat.runtime.replay import _STAGE_NAMES

    out: dict[str, Any] = {}
    if "from_sequence" in kwargs and kwargs["from_sequence"] is not None:
        value = kwargs["from_sequence"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("from_sequence must be an integer")
        out["from_sequence"] = value
    if "to_sequence" in kwargs and kwargs["to_sequence"] is not None:
        value = kwargs["to_sequence"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("to_sequence must be an integer")
        out["to_sequence"] = value
    if "stage_filter" in kwargs and kwargs["stage_filter"] is not None:
        value = kwargs["stage_filter"]
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ValueError("stage_filter must be a list of strings")
        unknown = [s for s in value if s not in _STAGE_NAMES]
        if unknown:
            raise ValueError(f"unknown stage(s) in stage_filter: {sorted(unknown)}")
        out["stage_filter"] = list(value)
    return out


def _bundle_source(bundle_path: str | Path) -> DebuggerSource:
    """Build an immutable bundle-backed source with cached lookups.

    Bundles never change after load, so we cache the records list and
    artifact-blob view once.  Subsequent ``records()`` calls return the
    same list without re-decoding NDJSON, which matters when the UI
    polls and bundles run into the tens of thousands of records.
    """
    bundle = RunBundle.load(bundle_path)
    cached_records = list(bundle.records())
    basename = Path(str(bundle_path)).name

    def _replay(**kwargs: Any) -> Any:
        from easycat.runtime.replay import (
            ReplayFidelity,
            ReplaySpec,
            ToolReplayPolicy,
        )

        fidelity = ReplayFidelity(kwargs.get("fidelity", "artifact"))
        timing = kwargs.get("timing", "fast")
        force = bool(kwargs.get("force", False))
        tool_policy = ToolReplayPolicy(kwargs.get("tool_policy", "deny"))
        validated = _validated_replay_kwargs(kwargs)
        spec = ReplaySpec(
            fidelity=fidelity,
            timing=timing,
            force=force,
            tool_policy=tool_policy,
            from_sequence=validated.get("from_sequence"),
            to_sequence=validated.get("to_sequence"),
            stage_filter=validated.get("stage_filter"),
        )
        result = bundle.replay(spec)
        total_frames = len(result.frames)
        truncated = total_frames > _REPLAY_FRAME_LIMIT
        kept = result.frames[:_REPLAY_FRAME_LIMIT] if truncated else result.frames
        return {
            "fidelity_label": result.fidelity_label.value,
            "frame_count": len(kept),
            "total_frames": total_frames,
            "frames_truncated": truncated,
            "frames": [_serialize_frame(f) for f in kept],
            "side_effecting": result.side_effecting,
            "blocked_tool_calls": result.blocked_tool_calls,
            "stubbed_tool_calls": result.stubbed_tool_calls,
            "allowed_tool_calls": result.allowed_tool_calls,
        }

    return DebuggerSource(
        label=basename,
        _records_fn=lambda: cached_records,
        # Bundles are immutable, so the count is fixed.  Use it as both the
        # change-detection key and the displayed count — the WS loop emits a
        # single snapshot and stops (bundles are not live).
        _progress_fn=lambda: (len(cached_records), len(cached_records)),
        _artifact_fn=lambda ref: bundle.artifact_blobs.get(ref),
        _manifest_fn=lambda: {
            "source": "bundle",
            "name": basename,
            "format_version": bundle.format_version,
            "provider_versions": bundle.manifest.provider_versions,
            "config_snapshot": bundle.manifest.config_snapshot,
            "sharing_banner": bundle.sharing_banner,
            "record_count": len(cached_records),
            "artifact_count": len(bundle.artifact_blobs),
            "supports_replay": True,
            "supports_export": False,
            "is_live": False,
            "replay_entry_points": [
                {
                    "sequence": cp.sequence,
                    "stage": cp.stage,
                    "unit_id": cp.unit_id,
                    "checkpoint_id": cp.checkpoint_id,
                }
                for cp in bundle.replay_entry_points
            ],
        },
        _bundle_fn=lambda: bundle,
        _replay_fn=_replay,
        is_live=False,
    )


def _session_source(session: Any) -> DebuggerSource:
    """Adapt a live ``Session`` so the UI can poll while it's running.

    Reads from ``session.journal`` (a JournalView) and pulls artifact
    bytes from ``session._artifact_store`` if one is attached.  No
    side-effecting hooks into Session — purely observational.
    """

    def _records() -> Iterable[dict[str, Any]]:
        journal = getattr(session, "journal", None)
        if journal is None:
            return []
        return [_record_to_dict(r) for r in journal.read()]

    def _progress() -> tuple[int, int]:
        # O(1) growth probe: the backend keeps ``latest_sequence`` as an
        # in-memory counter, so this never re-reads or re-serializes the
        # journal.  Sequence is monotonic (the WS change-detection key);
        # we surface it as the displayed count too — it equals the record
        # count on persistent backends, which are the ones that grow
        # unboundedly and the only ones the WS poll needs to track.
        journal = getattr(session, "journal", None)
        if journal is None:
            return (0, 0)
        seq = getattr(journal, "latest_sequence", None)
        if seq is None:
            n = len(list(journal.read()))
            return (n, n)
        return (int(seq), int(seq))

    def _artifact(ref: str) -> bytes | None:
        store = getattr(session, "_artifact_store", None)
        if store is None:
            return None
        return store.get(ref)

    def _manifest() -> dict[str, Any]:
        return {
            "source": "session",
            "session_id": getattr(session, "session_id", ""),
            "is_running": bool(getattr(session, "is_running", False)),
            "turn_state": str(getattr(session, "turn_state", "")),
            "supports_replay": False,
            "supports_export": True,
            "is_live": True,
            "replay_entry_points": [],
        }

    return DebuggerSource(
        label=f"session-{getattr(session, 'session_id', 'unknown')}",
        _records_fn=_records,
        _progress_fn=_progress,
        _artifact_fn=_artifact,
        _manifest_fn=_manifest,
        _bundle_fn=None,
        _replay_fn=None,
        is_live=True,
    )


def _record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a JournalRecord-like object to a JSON-friendly dict."""
    if isinstance(record, dict):
        return record
    out: dict[str, Any] = {}
    for attr in (
        "sequence",
        "session_id",
        "kind",
        "name",
        "turn_id",
        "data",
        "input_ref",
        "output_ref",
    ):
        value = getattr(record, attr, None)
        if hasattr(value, "value"):
            value = value.value
        out[attr] = value
    timing = getattr(record, "timing", None)
    if timing is not None:
        out["timing"] = {
            k: getattr(timing, k, None) for k in ("wall_ns", "mono_ns", "cpu_ns", "queue_ns")
        }
    error = getattr(record, "error", None)
    if error is not None:
        out["error"] = {
            "type": getattr(error, "type", None),
            "message": getattr(error, "message", None),
        }
    return out


# ── Pure helpers (record filtering / rollups) ────────────────────


def _filter_records(
    records: list[dict[str, Any]],
    *,
    stage: str | None,
    turn_id: str | None,
    name: str | Iterable[str] | None,
    from_seq: int | None,
    to_seq: int | None,
    errors_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Filter records.  Slicing happens here for callers that want a
    single combined operation; pagination on the HTTP API goes through
    :func:`_filter_and_paginate` so the response can carry both the
    page slice and the full match count.

    ``name`` may be a single string (exact match) or an iterable of
    strings (membership match).  The HTTP handler surfaces the latter
    via repeated ``name=`` query params so the Live view can fetch only
    the event names it renders without being capped by ``limit``.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")
    name_set: frozenset[str] | None
    if name is None:
        name_set = None
    elif isinstance(name, str):
        name_set = frozenset({name})
    else:
        collected = frozenset(name)
        name_set = collected or None
    out = []
    for r in records:
        seq = r.get("sequence")
        if seq is None:
            continue
        if from_seq is not None and seq < from_seq:
            continue
        if to_seq is not None and seq > to_seq:
            continue
        if turn_id is not None and r.get("turn_id") != turn_id:
            continue
        if name_set is not None and r.get("name") not in name_set:
            continue
        if stage is not None:
            data = r.get("data") or {}
            if not isinstance(data, dict):
                continue
            if data.get("stage") != stage and data.get("observed_stage") != stage:
                continue
        if errors_only and not r.get("error"):
            continue
        out.append(r)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return out


def _filter_and_paginate(
    records: list[dict[str, Any]],
    *,
    stage: str | None,
    turn_id: str | None,
    name: str | Iterable[str] | None,
    from_seq: int | None,
    to_seq: int | None,
    errors_only: bool,
    limit: int | None,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(page, total)`` so the UI can render "X of N".

    The previous endpoint returned ``page_size`` as ``total``, which
    made it impossible to render a real pager and confused tooling.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")
    full = _filter_records(
        records,
        stage=stage,
        turn_id=turn_id,
        name=name,
        from_seq=from_seq,
        to_seq=to_seq,
        errors_only=errors_only,
        limit=None,
        offset=0,
    )
    total = len(full)
    if offset:
        full = full[offset:]
    if limit is not None:
        full = full[:limit]
    return full, total


def _summarise_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        timing = r.get("timing") or {}
        wall = timing.get("wall_ns") if isinstance(timing, dict) else None
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
        # T3.8 fans an InterruptSignal across all 8 stages, so a single
        # barge-in produces 8 ``control_signal`` records (one per stage)
        # plus the legacy ``interruption`` event.  We bookkeep both
        # here and resolve the deduped count in the post-pass below
        # so record order doesn't affect the result.
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
        # ``interruption`` event count for bundles that predate T3.8.
        signal_count = len(bucket["_interrupt_signal_ids"])
        legacy_count = bucket.pop("_legacy_interruptions", 0)
        bucket["interruption_count"] = signal_count if signal_count else legacy_count
        bucket.pop("_interrupt_signal_ids", None)
        first = bucket["first_wall_ns"]
        last = bucket["last_wall_ns"]
        bucket["wall_ms"] = ((last - first) / 1_000_000) if first and last else None
        rolled.append(bucket)
    return rolled


_STAGE_ORDER = ("transport", "audio", "vad", "stt", "agent", "tts", "turn", "telephony")


def _build_timeline(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        timing = r.get("timing") or {}
        wall = timing.get("wall_ns") if isinstance(timing, dict) else None
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
        # T3.8 fans an interrupt across all 8 stages, so each stage
        # gets a ``control_signal`` with ``observed_stage`` set even
        # when that stage had no real pipeline activity.  Counting
        # those here would render a synthetic instant-span for stages
        # the turn never actually touched.  ``stage_counts`` in
        # ``_summarise_turns`` still accounts for the signals.
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
        for stage_name in _STAGE_ORDER:
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


def _build_transcript(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull user transcripts and agent responses out of the journal.

    The UI renders this alongside the waterfall so a developer can read
    the conversation without opening every record.  Sources:
    - User text: ``stt_final`` event records (``data.text``).
    - Agent reply: AgentStage ``stage_complete`` records
      (``data.response``) for the basic path; concatenated agent_delta
      records for the streaming path.
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
                "user": "",
                "agent": "",
                "user_seq": None,
                "agent_seq": None,
                "agent_delta": [],
                "agent_delta_seq": None,
            },
        )
        name = r.get("name") or ""
        data = r.get("data") or {}
        seq = r.get("sequence")
        if not isinstance(data, dict):
            continue
        if name == "stt_final":
            txt = data.get("text") or data.get("transcript")
            if isinstance(txt, str) and txt:
                bucket["user"] = txt
                bucket["user_seq"] = seq
        elif name == "stage_complete" and (
            data.get("stage") == "agent" or data.get("observed_stage") == "agent"
        ):
            resp = data.get("response")
            if isinstance(resp, str) and resp:
                bucket["agent"] = resp
                bucket["agent_seq"] = seq
        elif name == "agent_delta":
            txt = data.get("text")
            if isinstance(txt, str) and txt and data.get("type") == "TEXT_DELTA":
                bucket["agent_delta"].append(txt)
                if bucket["agent_delta_seq"] is None:
                    bucket["agent_delta_seq"] = seq
        elif name == "agent_final":
            txt = data.get("text")
            if isinstance(txt, str) and txt and not bucket["agent"]:
                bucket["agent"] = txt
                bucket["agent_seq"] = seq

    transcripts = []
    for turn_id, bucket in by_turn.items():
        if not bucket["agent"] and bucket["agent_delta"]:
            bucket["agent"] = "".join(bucket["agent_delta"])
            if bucket["agent_seq"] is None:
                bucket["agent_seq"] = bucket["agent_delta_seq"]
        bucket.pop("agent_delta", None)
        bucket.pop("agent_delta_seq", None)
        transcripts.append(bucket)
    return transcripts


def _cost_rollup(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate ``CostRecord``-style entries.  Degrades to zero when absent.

    Cost records are owned by the peripheral observability/cost plan so
    they may not exist in any given bundle.  The endpoint returns a
    well-formed shape with zeroes rather than 404'ing so the UI can
    always render the panel.
    """
    by_turn: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {"usd": 0.0, "stt_seconds": 0.0, "tts_chars": 0, "llm_tokens": 0}
    for r in records:
        if r.get("name") not in ("cost", "cost_record"):
            continue
        data = r.get("data") or {}
        if not isinstance(data, dict):
            continue
        turn_id = r.get("turn_id") or ""
        bucket = by_turn.setdefault(
            turn_id, {"usd": 0.0, "stt_seconds": 0.0, "tts_chars": 0, "llm_tokens": 0}
        )
        for key in ("usd", "stt_seconds", "tts_chars", "llm_tokens"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                bucket[key] += v
                totals[key] += v
    return {"per_turn": by_turn, "totals": totals}


def _collect_tts_frames(
    source: DebuggerSource, turn_id: str
) -> tuple[list[bytes], dict[str, int]]:
    """Return ``(pcm_blobs_in_order, format)`` for one turn's TTS frames.

    Streaming concat reads this and writes the WAV header up-front,
    then pushes each PCM blob to the response without buffering the
    entire stream in memory.

    Raises ``ValueError`` if frames have inconsistent PCM formats —
    never silently splices different sample rates together.
    """
    frames: list[tuple[int, bytes, dict[str, Any]]] = []
    for r in source.records():
        if r.get("name") != "tts_frame":
            continue
        if r.get("turn_id") != turn_id:
            continue
        ref = r.get("output_ref")
        if not ref:
            continue
        blob = source.artifact(ref)
        if blob is None:
            continue
        data = r.get("data") or {}
        if not isinstance(data, dict):
            continue
        frames.append((int(r.get("sequence") or 0), blob, data))

    if not frames:
        return [], {}

    frames.sort(key=lambda item: item[0])
    fmt0 = frames[0][2]
    fmt = {
        "sample_rate": int(fmt0.get("sample_rate") or 16000),
        "channels": int(fmt0.get("channels") or 1),
        "sample_width": int(fmt0.get("sample_width") or 2),
    }
    for _seq, _blob, data in frames[1:]:
        if (
            int(data.get("sample_rate") or 0) != fmt["sample_rate"]
            or int(data.get("channels") or 0) != fmt["channels"]
            or int(data.get("sample_width") or 0) != fmt["sample_width"]
        ):
            raise ValueError(
                f"tts_frame format mismatch in turn {turn_id}: cannot stitch "
                "frames with differing sample_rate/channels/sample_width"
            )
    return [blob for _seq, blob, _data in frames], fmt


def _wav_header(*, sample_rate: int, channels: int, sample_width: int, data_size: int) -> bytes:
    """Build a 44-byte RIFF/WAVE PCM header.

    Used by both the streaming HTTP route and the in-memory helper that
    backs the legacy ``_concatenated_wav_for_turn`` function.
    """
    bits_per_sample = sample_width * 8
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_size),
            b"WAVE",
            b"fmt ",
            struct.pack(
                "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample
            ),
            b"data",
            struct.pack("<I", data_size),
        ]
    )


def _concatenated_wav_for_turn(
    source: DebuggerSource, turn_id: str
) -> tuple[bytes, dict[str, Any]] | None:
    """Backwards-compat helper that returns the entire WAV in memory.

    Tests still use this directly; the HTTP route now streams via
    :func:`_collect_tts_frames` + :func:`_wav_header` for memory safety.
    """
    frames, fmt = _collect_tts_frames(source, turn_id)
    if not frames:
        return None
    pcm = b"".join(frames)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(fmt["channels"])
        wf.setsampwidth(fmt["sample_width"])
        wf.setframerate(fmt["sample_rate"])
        wf.writeframes(pcm)
    return buf.getvalue(), {**fmt, "frame_count": len(frames), "byte_count": len(pcm)}


def _safe_unlink(path: Any) -> None:
    """Best-effort delete; never raises so the event-loop callback is safe."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - filesystem race
        logger.debug("Failed to clean up debugger temp file %s", path, exc_info=True)


def _bundle_zip_from_session(session: Any) -> Path | None:
    """Build a bundle-shaped ZIP for a live session and return its path.

    The HTTP export route uses :class:`aiohttp.web.FileResponse` to
    stream the file, then schedules a delayed unlink so we don't have
    to hold the bundle bytes in memory.  Returns ``None`` when the
    session has no journal (debug='off').
    """
    journal = getattr(session, "journal", None) or getattr(session, "_journal", None)
    if journal is None:
        return None
    import tempfile

    from easycat.debug.export import export_debug_bundle

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        export_debug_bundle(session, tmp_path, overwrite=True)
    except Exception:
        # Clean up before propagating so callers don't see a half-written
        # tempfile linger in /tmp.
        _safe_unlink(tmp_path)
        raise
    return tmp_path


# ── HTTP API ─────────────────────────────────────────────────────


def _make_app(source: DebuggerSource, *, allow_remote: bool = False) -> Any:
    """Build the aiohttp Application with all routes wired up."""
    try:
        from aiohttp import WSMsgType, web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "easycat[debugger] not installed. Install with "
            "`pip install easycat[debugger]` to use the debugger UI."
        ) from exc

    static_dir = Path(__file__).parent / "static"

    _SAFE_ORIGIN_PREFIXES = (
        "http://127.0.0.1",
        "http://localhost",
        "http://[::1]",
        "https://127.0.0.1",
        "https://localhost",
        "https://[::1]",
    )
    _STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def _origin_is_safe(origin: str) -> bool:
        return bool(origin) and origin.startswith(_SAFE_ORIGIN_PREFIXES)

    @web.middleware
    async def _origin_guard(request: Any, handler: Any) -> Any:
        """Refuse cross-origin requests on the loopback default.

        Three checks layered for defense-in-depth:

        1. ``Origin`` header, when present, must point at a loopback
           address.  Browsers always send Origin on cross-origin
           fetches, ws upgrades, and POST.
        2. ``Sec-Fetch-Site`` (set by all modern browsers) must be
           ``same-origin``, ``same-site``, or ``none`` (top-level nav).
           Any cross-site value is refused regardless of Origin.
        3. State-changing methods (POST/PUT/PATCH/DELETE) require an
           ``application/json`` content type and a present, safe
           Origin — kills the simple-form-POST CSRF vector that
           browsers wave through without preflight.

        ``allow_remote=True`` disables all three: callers who want
        network exposure are on their own.
        """
        if allow_remote:
            return await handler(request)
        origin = request.headers.get("Origin", "")
        site = request.headers.get("Sec-Fetch-Site", "")
        if site and site not in ("same-origin", "same-site", "none"):
            return web.Response(status=403, text="cross-site requests refused")
        if origin and not _origin_is_safe(origin):
            return web.Response(status=403, text="cross-origin requests refused")
        if request.method in _STATE_CHANGING_METHODS:
            ctype = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and ctype != "application/json":
                return web.Response(
                    status=415, text="state-changing requests must use application/json"
                )
            # On state-changing requests, a missing Origin from a
            # browser is suspicious — refuse rather than trust the
            # caller blindly.  Server-to-server clients can pass an
            # explicit ``Origin: http://localhost`` or use
            # ``allow_remote``.
            if not origin:
                return web.Response(
                    status=403, text="state-changing requests require an Origin header"
                )
        return await handler(request)

    async def index(_request: Any) -> Any:
        return web.FileResponse(static_dir / "index.html")

    async def manifest(_request: Any) -> Any:
        return web.json_response(source.manifest())

    async def records(request: Any) -> Any:
        params = request.query
        try:
            from_seq = int(params["from"]) if "from" in params else None
            to_seq = int(params["to"]) if "to" in params else None
            limit = int(params["limit"]) if "limit" in params else None
            offset = int(params["offset"]) if "offset" in params else 0
        except ValueError:
            return web.Response(status=400, text="from/to/limit/offset must be integers")
        # aiohttp's ``getall`` returns every repeated ``name=`` value so
        # the Live view can request only the handful of event names it
        # actually renders (e.g. ``name=vad_start_speaking&name=stt_partial``)
        # without being capped by ``limit``.
        names = [n for n in params.getall("name", ()) if n]
        try:
            page, total = _filter_and_paginate(
                source.records(),
                stage=params.get("stage") or None,
                turn_id=params.get("turn") or None,
                name=names or None,
                from_seq=from_seq,
                to_seq=to_seq,
                errors_only=params.get("errors") == "1",
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            logger.warning("Invalid records query: %s", exc)
            return web.Response(status=400, text="invalid query parameters")
        return web.json_response(
            {
                "records": page,
                "page_size": len(page),
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )

    async def turns(_request: Any) -> Any:
        return web.json_response({"turns": _summarise_turns(source.records())})

    async def timeline(_request: Any) -> Any:
        return web.json_response({"timeline": _build_timeline(source.records())})

    async def transcript(_request: Any) -> Any:
        return web.json_response({"transcripts": _build_transcript(source.records())})

    async def cost(_request: Any) -> Any:
        return web.json_response(_cost_rollup(source.records()))

    async def artifact(request: Any) -> Any:
        try:
            ref = _safe_ref(request.match_info["ref"])
        except ValueError:
            return web.Response(status=400, text="invalid artifact ref")
        blob = source.artifact(ref)
        if blob is None:
            return web.Response(status=404, text=f"artifact {ref} not found")
        return web.Response(body=blob, content_type="application/octet-stream")

    async def audio_concat(request: Any) -> Any:
        try:
            turn_id = _safe_turn_id(request.match_info["turn"])
        except ValueError:
            return web.Response(status=400, text="invalid turn_id")
        try:
            frames, fmt = _collect_tts_frames(source, turn_id)
        except ValueError as exc:
            logger.warning("Cannot assemble TTS audio for %s: %s", turn_id, exc)
            return web.Response(status=409, text="cannot assemble audio for this turn")
        if not frames:
            return web.Response(status=404, text="no tts frames for turn")
        # Stream the WAV out incrementally.  Whole-file response would
        # buffer tens of MB for long turns; StreamResponse lets aiohttp
        # backpressure the client and avoids the heap spike.
        pcm_total = sum(len(blob) for blob in frames)
        header = _wav_header(
            sample_rate=fmt["sample_rate"],
            channels=fmt["channels"],
            sample_width=fmt["sample_width"],
            data_size=pcm_total,
        )
        response = web.StreamResponse(
            headers={
                "Content-Type": "audio/wav",
                "Content-Length": str(len(header) + pcm_total),
            }
        )
        await response.prepare(request)
        await response.write(header)
        for blob in frames:
            await response.write(blob)
        await response.write_eof()
        return response

    _DESTRUCTIVE_FIDELITIES = frozenset({"live"})
    _DESTRUCTIVE_TOOL_POLICIES = frozenset({"allow"})
    _ALLOWED_REPLAY_KEYS = frozenset(
        {
            "fidelity",
            "timing",
            "force",
            "tool_policy",
            "confirm",
            "from_sequence",
            "to_sequence",
            "stage_filter",
        }
    )

    async def replay(request: Any) -> Any:
        if not source.manifest().get("supports_replay"):
            return web.Response(status=405, text="this source does not support replay")
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="body must be JSON")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="body must be a JSON object")
        unknown = set(payload) - _ALLOWED_REPLAY_KEYS
        if unknown:
            return web.json_response({"error": f"unknown keys: {sorted(unknown)}"}, status=400)
        fidelity = payload.get("fidelity", "artifact")
        tool_policy = payload.get("tool_policy", "deny")
        force = bool(payload.get("force", False))
        confirm = bool(payload.pop("confirm", False))
        # ARTIFACT/SIMULATED with DENY/STUB are always safe; LIVE
        # fidelity, ALLOW tool policy, or force=True can re-execute
        # against live providers and need explicit confirmation so a
        # CSRF / drive-by from another tab can't fire them silently.
        destructive = (
            fidelity in _DESTRUCTIVE_FIDELITIES
            or tool_policy in _DESTRUCTIVE_TOOL_POLICIES
            or force
        )
        if destructive and not confirm:
            return web.json_response(
                {
                    "error": (
                        "destructive replay requested (live fidelity, allow tool "
                        "policy, or force) — set 'confirm': true to acknowledge"
                    ),
                    "destructive": True,
                },
                status=409,
            )
        from easycat.runtime.replay import (
            ProviderVersionMismatchError,
            ReplayError,
            ReplaySideEffectBlocked,
        )

        try:
            result = source.replay(**payload)
        except ProviderVersionMismatchError as exc:
            return web.json_response(
                {
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "details": {
                        "mismatches": [
                            {
                                "provider": m.provider,
                                "bundle_version": m.bundle_version,
                                "installed_version": m.installed_version,
                                "code": m.code,
                            }
                            for m in exc.mismatches
                        ],
                    },
                },
                status=409,
            )
        except ReplayError as exc:
            return web.json_response(
                {
                    "error_code": "REPLAY_NON_COMMITTABLE",
                    "message": str(exc),
                    "details": {
                        "requested_sequence": exc.requested_sequence,
                        "nearest_committable_before": exc.nearest_committable_before,
                        "nearest_committable_after": exc.nearest_committable_after,
                        "stage": exc.stage,
                    },
                },
                status=409,
            )
        except ReplaySideEffectBlocked as exc:
            return web.json_response(
                {
                    "error_code": "REPLAY_SIDE_EFFECT_BLOCKED",
                    "message": str(exc),
                    "details": {},
                },
                status=409,
            )
        except (ValueError, TypeError) as exc:
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": str(exc)}, status=400
            )
        except RuntimeError as exc:
            return web.json_response(
                {"error_code": "REPLAY_FAILED", "message": str(exc)}, status=500
            )
        result["destructive"] = destructive
        return web.json_response(result)

    async def export(request: Any) -> Any:
        if not source.manifest().get("supports_export"):
            return web.Response(status=405, text="export only supported for live sessions")
        export_fn = getattr(source, "_export_fn", None)
        if export_fn is None:
            return web.Response(status=503, text="no export function bound")
        try:
            tmp_path = export_fn()
        except Exception:  # noqa: BLE001 - never hide export errors
            # Detail is logged server-side; don't leak exception text to the
            # client (CodeQL py/stack-trace-exposure).
            logger.exception("Export failed")
            return web.Response(status=500, text="export failed")
        if tmp_path is None:
            return web.Response(status=409, text="session has no journal to export")
        # FileResponse streams the bundle without loading it into memory.
        # The temp file is cleaned up by a delayed callback below.
        response = web.FileResponse(
            tmp_path,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename=session.zip",
            },
        )
        # Schedule cleanup once aiohttp has finished sending.
        loop = asyncio.get_running_loop()
        loop.call_later(60.0, _safe_unlink, tmp_path)
        return response

    async def refresh(_request: Any) -> Any:
        return web.json_response({"snapshot_size": len(source.records())})

    async def healthcheck(_request: Any) -> Any:
        return web.json_response({"ok": True, "is_live": source.is_live})

    async def websocket(request: Any) -> Any:
        """Push live updates to the UI.

        Sends a snapshot every poll interval (live sources) or once
        (bundle sources).  Clients can send ``{"action": "ping"}`` to
        keep the connection alive; we respond with ``pong``.
        """
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        last_seq = -1
        try:
            while not ws.closed:
                # Cheap O(1) growth probe — never re-reads or re-serializes
                # the journal just to compare counts.  Only emit a snapshot
                # when the monotonic sequence advances; the actual records
                # are fetched separately via /api/records.
                latest_seq, record_count = source.progress()
                if latest_seq != last_seq:
                    last_seq = latest_seq
                    await ws.send_json(
                        {
                            "type": "snapshot",
                            "record_count": record_count,
                            "manifest": source.manifest(),
                        }
                    )
                if not source.is_live:
                    break
                # Poll every 500ms for new records.  WS clients also
                # listen for messages, so a manual refresh works too.
                with contextlib.suppress(asyncio.TimeoutError):
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                    if msg.type == WSMsgType.TEXT:
                        try:
                            req = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if req.get("action") == "ping":
                            await ws.send_json({"type": "pong"})
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
        finally:
            await ws.close()
        return ws

    app = web.Application(middlewares=[_origin_guard])
    app.router.add_get("/", index)
    app.router.add_get("/api/manifest", manifest)
    app.router.add_get("/api/records", records)
    app.router.add_get("/api/turns", turns)
    app.router.add_get("/api/timeline", timeline)
    app.router.add_get("/api/transcript", transcript)
    app.router.add_get("/api/cost", cost)
    app.router.add_get("/api/artifact/{ref}", artifact)
    app.router.add_get("/api/audio/concat/{turn}", audio_concat)
    app.router.add_post("/api/replay", replay)
    app.router.add_post("/api/export", export)
    app.router.add_get("/api/refresh", refresh)
    app.router.add_get("/api/health", healthcheck)
    app.router.add_get("/ws", websocket)
    # Static assets directory if we ever add JS / CSS files.
    if static_dir.is_dir():
        app.router.add_static("/static/", path=static_dir, show_index=False)
    return app


# ── Public entry points ──────────────────────────────────────────


def serve_bundle(
    bundle_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_remote: bool = False,
) -> None:
    """Serve the debugger UI for a bundle on disk.  Blocks the caller.

    ``allow_remote=True`` is required to bind a non-loopback ``host``;
    otherwise the server refuses non-loopback addresses with a clear
    error.  Bundles can contain transcripts, audio, and provider
    versions, so default to loopback-only.
    """
    _check_host(host, allow_remote)
    source = _bundle_source(bundle_path)
    _serve(source, host=host, port=port, open_browser=open_browser, allow_remote=allow_remote)


def serve_session(
    session: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    in_thread: bool = False,
    allow_remote: bool = False,
) -> threading.Thread | None:
    """Serve the debugger UI for a live :class:`Session`.

    Blocks the caller unless ``in_thread`` is set, in which case the
    server runs on a background daemon thread and the started
    :class:`threading.Thread` is returned so the caller can join later.
    """
    _check_host(host, allow_remote)
    source = _session_source(session)
    # Wire up the export-bytes function so /api/export can stream a zip.
    source._export_fn = lambda: _bundle_zip_from_session(session)  # type: ignore[attr-defined]
    if not in_thread:
        _serve(
            source,
            host=host,
            port=port,
            open_browser=open_browser,
            allow_remote=allow_remote,
        )
        return None

    thread = threading.Thread(
        target=_serve,
        args=(source,),
        kwargs={
            "host": host,
            "port": port,
            "open_browser": open_browser,
            "allow_remote": allow_remote,
            # aiohttp's default signal handling uses ``signal.set_wakeup_fd``,
            # which only works on the main thread — installing it from a
            # daemon thread raises ``RuntimeError`` and kills the server
            # before it answers a single request.
            "handle_signals": False,
        },
        daemon=True,
        name="easycat-debugger",
    )
    thread.start()
    return thread


def _check_host(host: str, allow_remote: bool) -> None:
    """Refuse non-loopback hosts unless the caller explicitly opts in.

    The debugger surfaces journals (which can contain transcripts and
    audio) and the artifact endpoint serves bytes by ref — exposing
    that to the local network without auth is dangerous by default.
    """
    if host in _LOOPBACK_HOSTS:
        return
    if not allow_remote:
        raise RuntimeError(
            f"Refusing to bind debugger to non-loopback host {host!r} without "
            "allow_remote=True. The debugger has no auth — see docstring."
        )
    logger.warning(
        "Debugger UI bound to non-loopback host %s with allow_remote=True. "
        "Anyone who can reach this address can read your journals.",
        host,
    )


def _serve(
    source: DebuggerSource,
    *,
    host: str,
    port: int,
    open_browser: bool,
    allow_remote: bool,
    handle_signals: bool = True,
) -> None:
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "easycat[debugger] not installed. Install with "
            "`pip install easycat[debugger]` to use the debugger UI."
        ) from exc

    app = _make_app(source, allow_remote=allow_remote)
    url = f"http://{host}:{port}/"
    logger.info("EasyCat debugger UI serving on %s (source=%s)", url, source.label)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - depends on env
            logger.debug("Could not open browser automatically", exc_info=True)
    web.run_app(app, host=host, port=port, print=None, handle_signals=handle_signals)


# ── Async-friendly variant for callers already inside an event loop ─


async def run_app_async(
    source: DebuggerSource,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_remote: bool = False,
) -> Any:
    """Start the debugger app inside an existing asyncio loop.

    Returns the ``aiohttp`` ``AppRunner`` so the caller can ``cleanup``
    it during shutdown.  Useful for unit tests that need to drive the
    server from inside a pytest-asyncio test.
    """
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "easycat[debugger] not installed. Install with "
            "`pip install easycat[debugger]` to use the debugger UI."
        ) from exc

    _check_host(host, allow_remote)
    app = _make_app(source, allow_remote=allow_remote)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner


def _ensure_aiohttp() -> None:
    """Internal helper used by tests to skip cleanly when aiohttp is absent."""
    try:
        import aiohttp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("aiohttp not installed; install easycat[debugger].") from exc
