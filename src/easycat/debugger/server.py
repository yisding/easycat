"""aiohttp-backed debugger server.

Adapts a :class:`DebuggerSource` (a bundle on disk, an in-memory
:class:`RunBundle`, or a live :class:`Session`) into a JSON HTTP API,
WebSocket push channel, and single-page HTML UI rendering the
timeline, per-stage waterfall, pipeline graph, transcript, audio
playback, replay surface, cost rollup, and bundle export.

Routes:

- ``GET  /``                          — static HTML page
- ``GET  /api/manifest``              — bundle/session metadata
- ``GET  /api/records``               — journal records (filterable; ``?q=`` text, ``&regex=1``)
- ``GET  /api/turns``                 — per-turn rollup with stage counts
- ``GET  /api/timeline``              — per-stage span timing per turn
- ``GET  /api/transcript``            — extracted user/agent text per turn
- ``GET  /api/cost``                  — cost rollup and budget status
- ``GET  /api/issues``                — severity-ranked issue rollup
- ``GET  /api/artifact/<ref>``        — raw artifact bytes (audio chunks)
- ``GET  /api/audio/concat/<turn>``   — concatenated WAV for one turn (``?track=tts|mic``)
- ``GET  /api/audio/waveform/<turn>`` — greyscale waveform PNG (``?track=tts|mic&w=&h=``)
- ``GET  /api/aec/<turn>``            — AEC diagnostics (ERLE / double-talk / self-echo / tracks)
- ``POST /api/aec/<turn>/vad-whatif`` — re-run VAD at an alternate threshold (bundle only)
- ``POST /api/replay``                — run replay against the source
- ``POST /api/export``                — export the source as a bundle ZIP (``?turn=`` slices one)
- ``POST /api/annotate``              — persist a per-turn verdict sidecar (bundle only)
- ``GET  /api/annotations``           — read the per-turn verdict sidecar map (bundle only)
- ``GET  /ws``                        — WebSocket push for live updates
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import socket
import struct
import threading
import wave
import webbrowser
from collections.abc import Iterable
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from easycat.debug._issues import build_issues as _build_issues
from easycat.debug._pcm import full_scale as _full_scale
from easycat.debug._pcm import is_supported_width as _is_supported_width
from easycat.debug._turn_timeline import build_timeline as _build_timeline  # noqa: F401
from easycat.debug._turn_timeline import extract_turn_transcripts as _extract_turn_transcripts
from easycat.debug._turn_timeline import summarise_turns as _summarise_turns
from easycat.debug._turn_timeline import turn_cost_rollup as _turn_cost_rollup
from easycat.debug._turn_timeline import turn_waterfall as _turn_waterfall
from easycat.debug.annotations import (
    Annotation,
    AnnotationError,
    load_annotations,
    save_annotation,
)
from easycat.debug.bundle import RunBundle
from easycat.debugger._aec import (
    align_tracks as _align_aec_tracks,
)
from easycat.debugger._aec import (
    compute_erle as _compute_erle,
)
from easycat.debugger._aec import (
    detect_double_talk as _detect_double_talk,
)
from easycat.debugger._aec import (
    detect_self_echo as _detect_self_echo,
)
from easycat.debugger._aec import (
    frame_rms_series as _frame_rms_series,
)
from easycat.debugger._install_hint import DEBUGGER_INSTALL_HINT
from easycat.debugger._waveform import decode_pcm_peaks, encode_peaks_png
from easycat.runtime.costs import (
    cost_budget_status,
    max_session_cost_usd_from_snapshot,
)
from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME

# ``audioop`` was removed from the stdlib in Python 3.13.  Fall back to numpy
# (optional extra) for mic-track resample/downmix; skip mismatched frames only
# when neither helper is available.
try:
    import audioop as _audioop
except ImportError:  # pragma: no cover - exercised on 3.13+
    _audioop = None  # type: ignore[assignment]

try:
    import numpy as _np
except (ImportError, RecursionError):  # pragma: no cover
    _np = None  # type: ignore[assignment]


def _np_tomono(data: bytes, width: int) -> bytes:
    """Average stereo channels into mono using numpy (int8/16/32 PCM)."""
    _dtypes = {1: _np.int8, 2: _np.int16, 4: _np.int32}  # type: ignore[union-attr]
    dt = _dtypes.get(width)
    if dt is None:
        raise ValueError(f"unsupported sample width {width}")
    arr = _np.frombuffer(data, dtype=dt)  # type: ignore[union-attr]
    stereo = arr.reshape(-1, 2).astype(_np.int32)  # type: ignore[union-attr]
    mono = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(dt)
    return mono.tobytes()


def _np_ratecv(data: bytes, width: int, nchannels: int, inrate: int, outrate: int) -> bytes:
    """Linearly interpolate PCM from *inrate* to *outrate* using numpy."""
    _dtypes = {1: _np.int8, 2: _np.int16, 4: _np.int32}  # type: ignore[union-attr]
    dt = _dtypes.get(width)
    if dt is None:
        raise ValueError(f"unsupported sample width {width}")
    arr = _np.frombuffer(data, dtype=dt).astype(_np.float64)  # type: ignore[union-attr]
    n_frames = len(arr) // nchannels
    if n_frames == 0:
        return b""
    n_out = max(1, round(n_frames * outrate / inrate))
    x_in = _np.arange(n_frames)  # type: ignore[union-attr]
    x_out = _np.linspace(0, n_frames - 1, n_out)  # type: ignore[union-attr]
    if nchannels == 1:
        out = _np.interp(x_out, x_in, arr)  # type: ignore[union-attr]
    else:
        frames = arr.reshape(n_frames, nchannels)
        out = _np.column_stack(  # type: ignore[union-attr]
            [_np.interp(x_out, x_in, frames[:, ch]) for ch in range(nchannels)]  # type: ignore[union-attr]
        ).ravel()
    info = _np.iinfo(dt)  # type: ignore[union-attr]
    return _np.clip(out, info.min, info.max).astype(dt).tobytes()  # type: ignore[union-attr]

logger = logging.getLogger(__name__)


_SHA256_REF = re.compile(r"^[a-f0-9]{64}$")
_TURN_ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Hard cap on frames returned in /api/replay so a 50k-record bundle can't
# blow the response past sane sizes. The cap is generous: typical voice
# bundles run a few thousand records, and a per-frame `data` dict is
# small. UI surfaces `frames_truncated` + `total_frames` when this fires.
_REPLAY_FRAME_LIMIT = 5000

# Hard cap on records scanned by full-text search (``/api/records?q=`` and
# ``easycat journal grep``) so a pathological journal can't pin the event
# loop / CLI on a single request. Past this many records the scan stops and
# callers see ``scan_truncated`` so the cap is visible rather than silent.
_SEARCH_SCAN_LIMIT = 50000

# Upper bound on the search query string. The debugger binds loopback-only and
# the query comes from the developer searching their own journal (no privilege
# boundary), but bounding the length is cheap defense-in-depth that keeps a
# pathological user-supplied regex from compiling into a huge automaton.
_SEARCH_MAX_QUERY_LEN = 500

# Per-tick cap on records pushed in a live ``{"type": "records"}`` WebSocket
# batch.  A burst can advance the sequence by thousands in one poll; capping
# the slice keeps each frame small and lets the cursor catch up over a few
# polls rather than serializing a megabyte at once.
_WS_RECORD_BATCH_CAP = 200

# Audio-track selectors for ``/api/audio/concat`` and ``/api/audio/waveform``.
# ``tts`` stitches the bot's synthesized output (``tts_frame``/``output_ref``);
# ``mic`` stitches the caller's captured input (the STT stage's
# ``stage_start``/``input_ref``); ``reference`` stitches the AEC far-end
# reference (the bot playback fed to the echo canceller, journaled as
# ``aec_reference_frame``/``output_ref``) so the AEC view can draw it as the
# third aligned waveform strip alongside mic-in and post-AEC.
_AUDIO_TRACK_TTS = "tts"
_AUDIO_TRACK_MIC = "mic"
_AUDIO_TRACK_REFERENCE = "reference"
_VALID_AUDIO_TRACKS = frozenset({_AUDIO_TRACK_TTS, _AUDIO_TRACK_MIC, _AUDIO_TRACK_REFERENCE})


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
    # Bounded tail fetch used by the live WS loop: returns up to ``cap`` records
    # with ``sequence > after_seq`` *without* materializing the whole journal.
    # Live sources push this filter down to the journal's bounded
    # ``read(start=..., limit=...)``; when absent, ``records_since`` falls back to
    # slicing the (immutable, in-memory) ``records()`` list — correct for bundle
    # and static sources where the full list is already cached.
    _records_since_fn: Any | None = field(default=None, repr=False)
    # On-disk bundle path used to read/write the annotation sidecar.  Set only
    # for bundle sources and never surfaced in ``manifest()`` — the browser
    # learns it can annotate via the ``supports_annotate`` flag, never the path.
    _annotate_path: Path | None = field(default=None, repr=False)
    is_live: bool = False

    def records(self) -> list[dict[str, Any]]:
        return list(self._records_fn())

    def records_since(self, after_seq: int, cap: int) -> list[dict[str, Any]]:
        """Return up to *cap* records with ``sequence > after_seq`` (ascending).

        Live sources push the bound down to the journal's
        ``read(start=after_seq + 1, limit=cap)`` so an idle/caught-up WS tick
        never re-reads or re-serializes the whole journal.  Sources without a
        bounded fetch (bundles, static in-memory lists) fall back to slicing the
        already-cached ``records()`` list — cheap because that list is immutable
        and never re-decoded.
        """
        if cap <= 0:
            return []
        if self._records_since_fn is not None:
            return list(self._records_since_fn(after_seq, cap))
        out: list[dict[str, Any]] = []
        for r in self.records():
            seq = r.get("sequence")
            if isinstance(seq, int) and seq > after_seq:
                out.append(r)
                if len(out) >= cap:
                    break
        return out

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


def _run_bundle_source(
    bundle: RunBundle, *, label: str, annotate_path: Path | None = None
) -> DebuggerSource:
    """Build an immutable bundle-backed source from an already loaded bundle.

    ``annotate_path`` is the real on-disk bundle path used for the reviewer
    annotation sidecar; pass ``None`` for path-less bundles (e.g. a journal
    loaded in-memory), which disables the annotation controls.
    """
    cached_records = list(bundle.records())

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
        label=label,
        _records_fn=lambda: cached_records,
        # Bundles are immutable, so the count is fixed.  Use it as both the
        # change-detection key and the displayed count — the WS loop emits a
        # single snapshot and stops (bundles are not live).
        _progress_fn=lambda: (len(cached_records), len(cached_records)),
        _artifact_fn=lambda ref: bundle.artifact_blobs.get(ref),
        _manifest_fn=lambda: {
            "source": "bundle",
            "name": label,
            "format_version": bundle.format_version,
            "provider_versions": bundle.manifest.provider_versions,
            "config_snapshot": bundle.manifest.config_snapshot,
            "sharing_banner": bundle.sharing_banner,
            "record_count": len(cached_records),
            "artifact_count": len(bundle.artifact_blobs),
            "supports_replay": True,
            "supports_export": False,
            # Bundles are read-only, so reviewer verdicts land in a sidecar
            # next to the bundle rather than the journal.  The SPA shows the
            # per-turn annotation controls only when we have a real on-disk
            # path to write that sidecar to.
            "supports_annotate": annotate_path is not None,
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
        # Real on-disk path for the annotation sidecar; kept off the manifest
        # so it never leaks into the browser.
        _annotate_path=annotate_path,
        is_live=False,
    )


def _bundle_source(bundle_path: str | Path) -> DebuggerSource:
    """Build an immutable bundle-backed source with cached lookups.

    Bundles never change after load, so we cache the records list and
    artifact-blob view once.  Subsequent ``records()`` calls return the
    same list without re-decoding NDJSON, which matters when the UI
    polls and bundles run into the tens of thousands of records.
    """
    return _run_bundle_source(
        RunBundle.load(bundle_path),
        label=Path(str(bundle_path)).name,
        annotate_path=Path(str(bundle_path)),
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

    def _records_since(after_seq: int, cap: int) -> list[dict[str, Any]]:
        # Push the tail bound down to the journal: ``read(start=after_seq + 1,
        # limit=cap)`` returns only records with ``sequence > after_seq`` (the
        # backend filters/limits in SQL or on the ring buffer), so a live WS
        # tick serializes at most ``cap`` records instead of the whole journal.
        journal = getattr(session, "journal", None)
        if journal is None:
            return []
        return [_record_to_dict(r) for r in journal.read(start=after_seq + 1, limit=cap)]

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
            "config_snapshot": _safe_session_config_snapshot(session),
            "is_running": bool(getattr(session, "is_running", False)),
            "turn_state": str(getattr(session, "turn_state", "")),
            "supports_replay": False,
            "supports_export": True,
            # Live sessions don't carry a stable on-disk bundle to sidecar
            # against; verdicts are recorded after capture, on a bundle.
            "supports_annotate": False,
            "is_live": True,
            "replay_entry_points": [],
        }

    return DebuggerSource(
        label=f"session-{getattr(session, 'session_id', 'unknown')}",
        _records_fn=_records,
        _progress_fn=_progress,
        _records_since_fn=_records_since,
        _artifact_fn=_artifact,
        _manifest_fn=_manifest,
        _bundle_fn=None,
        _replay_fn=None,
        is_live=True,
    )


def _safe_session_config_snapshot(session: Any) -> dict[str, Any]:
    """Return the allowlisted config snapshot for live debugger sessions."""
    try:
        from easycat.runtime.safe_defaults import safe_config_snapshot

        config = getattr(session, "_easycat_config", None) or getattr(session, "_config", None)
        if config is None:
            return {}
        return safe_config_snapshot(config)
    except ImportError:
        return {}


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
        out["timing"] = {k: getattr(timing, k, None) for k in ("wall_ns", "mono_ns", "cpu_ns")}
    error = getattr(record, "error", None)
    if error is not None:
        out["error"] = _error_to_dict(error)
    return out


def _error_to_dict(error: Any) -> dict[str, Any]:
    return {
        "type": getattr(error, "type", None),
        "message": getattr(error, "message", None),
        "traceback": getattr(error, "traceback", None),
        "notes": getattr(error, "notes", None),
        "children": [_error_to_dict(child) for child in getattr(error, "children", ())],
    }


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


def _record_searchable_text(record: dict[str, Any]) -> str:
    """Build the haystack a full-text query is matched against.

    Combines the serialized ``data`` payload, the error type/message/notes,
    and the indexed ``name``/``turn_id`` so a query like ``timeout`` or a
    phone number embedded in a tool argument is found regardless of where it
    lives in the record.
    """
    parts: list[str] = []
    name = record.get("name")
    if name:
        parts.append(str(name))
    turn_id = record.get("turn_id")
    if turn_id:
        parts.append(str(turn_id))
    data = record.get("data")
    if data is not None:
        try:
            parts.append(json.dumps(data, default=str))
        except (TypeError, ValueError):
            parts.append(str(data))
    error = record.get("error")
    if isinstance(error, dict):
        for key in ("type", "message", "traceback", "notes"):
            value = error.get(key)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def _record_match_fields(record: dict[str, Any], needle: Any, *, is_regex: bool) -> list[str]:
    """Return the named fields of *record* whose text matches *needle*.

    *needle* is a lowercased substring (when ``is_regex`` is false) or a
    compiled :class:`re.Pattern` (when true).  Used to render match badges in
    the SPA and to scope redaction in the CLI grep output.
    """

    def _hit(text: str) -> bool:
        if not text:
            return False
        if is_regex:
            return needle.search(text) is not None
        return needle in text.lower()

    fields: list[str] = []
    if _hit(str(record.get("name") or "")):
        fields.append("name")
    if _hit(str(record.get("turn_id") or "")):
        fields.append("turn_id")
    data = record.get("data")
    if data is not None:
        try:
            data_text = json.dumps(data, default=str)
        except (TypeError, ValueError):
            data_text = str(data)
        if _hit(data_text):
            fields.append("data")
    error = record.get("error")
    if isinstance(error, dict) and any(
        _hit(str(error.get(key) or "")) for key in ("type", "message", "traceback", "notes")
    ):
        fields.append("error")
    return fields


def _search_records(
    records: list[dict[str, Any]],
    *,
    query: str,
    use_regex: bool = False,
    errors_only: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Full-text filter *records* against *query*, returning ``(matches, truncated)``.

    The haystack per record is :func:`_record_searchable_text` (serialized
    ``data`` + error fields + ``name``/``turn_id``).  Matching is a
    case-insensitive substring by default; when *use_regex* is true *query* is
    compiled with :data:`re.IGNORECASE` and a bad pattern raises
    :class:`ValueError` (mapped to a 400 / CLI error by callers).

    Matched records are returned as **shallow copies** carrying a
    ``_match_fields`` list — the cached ``source.records()`` dicts are never
    mutated.  The scan stops after :data:`_SEARCH_SCAN_LIMIT` records and the
    second tuple element reports whether that cap was hit.
    """
    if len(query) > _SEARCH_MAX_QUERY_LEN:
        raise ValueError("search query too long")
    needle: Any
    if use_regex:
        try:
            needle = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("invalid regex") from exc
    else:
        needle = query.lower()
        if not needle:
            # An empty query matches nothing rather than everything — an empty
            # search box should not silently return the entire journal.
            return [], False

    matches: list[dict[str, Any]] = []
    truncated = False
    for index, record in enumerate(records):
        if index >= _SEARCH_SCAN_LIMIT:
            truncated = True
            break
        if errors_only and not record.get("error"):
            continue
        haystack = _record_searchable_text(record)
        if use_regex:
            if needle.search(haystack) is None:
                continue
        elif needle not in haystack.lower():
            continue
        fields = _record_match_fields(record, needle, is_regex=use_regex)
        # Copy before annotating so the cached source records stay pristine.
        copied = dict(record)
        copied["_match_fields"] = fields
        matches.append(copied)
    return matches, truncated


def _build_transcript(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull user transcripts and agent responses out of the journal.

    The UI renders this alongside the waterfall so a developer can read
    the conversation without opening every record.  The pure projection
    lives in :func:`easycat.debug._turn_timeline.extract_turn_transcripts`
    so the two-source ``easycat diff`` shares one implementation; this thin
    wrapper keeps the historical name the SPA routes call.
    """
    return _extract_turn_transcripts(records)


def _cost_rollup(
    records: list[dict[str, Any]],
    *,
    config_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate ``CostRecord``-style entries.  Degrades to zero when absent.

    Cost records are owned by the peripheral observability/cost plan so
    they may not exist in any given bundle.  The endpoint returns a
    well-formed shape with zeroes rather than 404'ing so the UI can
    always render the panel.  The per-turn / total aggregation is the shared
    :func:`easycat.debug._turn_timeline.turn_cost_rollup`; only the budget
    evaluation (which needs the config snapshot) stays here.
    """
    by_turn, totals = _turn_cost_rollup(records)
    budget = cost_budget_status(
        totals["usd"],
        max_session_cost_usd_from_snapshot(config_snapshot),
    )
    return {"per_turn": by_turn, "totals": totals, "budget": budget}


def _coerce_frames_to_format(
    frames: list[tuple[int, bytes, dict[str, Any]]],
    fmt: dict[str, int],
    *,
    strict: bool,
) -> tuple[list[bytes], int]:
    """Reconcile *frames* to a single PCM *format*, returning ``(blobs, dropped)``.

    Every frame whose ``sample_rate``/``channels``/``sample_width`` already
    matches *fmt* passes through untouched.  For a mismatch:

    - ``strict=True`` (TTS) raises :class:`ValueError` — the bot's own output
      should never splice across formats, and the route maps this to a 409.
    - ``strict=False`` (mic) makes a best-effort conversion with
      :mod:`audioop` (stdlib, removed in 3.13) or :mod:`numpy` (optional
      extra) when ``sample_width`` matches, and otherwise *skips* the blob
      (incrementing the dropped counter) so a noisy caller capture never
      aborts the whole turn.  When neither helper is available any mismatch
      is skipped.
    """
    blobs: list[bytes] = []
    dropped = 0
    target_rate = fmt["sample_rate"]
    target_channels = fmt["channels"]
    target_width = fmt["sample_width"]
    for _seq, blob, data in frames:
        rate = int(data.get("sample_rate") or 0)
        channels = int(data.get("channels") or 0)
        width = int(data.get("sample_width") or 0)
        if rate == target_rate and channels == target_channels and width == target_width:
            blobs.append(blob)
            continue
        if strict:
            raise ValueError(
                "tts_frame format mismatch: cannot stitch frames with differing "
                "sample_rate/channels/sample_width"
            )
        # Non-strict (mic): convert when sample widths match and at least one
        # audio helper is present; otherwise drop rather than corrupt the stream.
        if (_audioop is None and _np is None) or width != target_width or width <= 0:
            dropped += 1
            continue
        try:
            converted = blob
            if channels == 2 and target_channels == 1:
                if _audioop is not None:
                    converted = _audioop.tomono(converted, width, 0.5, 0.5)
                else:
                    converted = _np_tomono(converted, width)
            elif channels != target_channels:
                dropped += 1
                continue
            if rate > 0 and rate != target_rate:
                if _audioop is not None:
                    converted, _ = _audioop.ratecv(
                        converted, width, target_channels, rate, target_rate, None
                    )
                else:
                    converted = _np_ratecv(converted, width, target_channels, rate, target_rate)
        except Exception:
            # audio helpers reject malformed PCM lengths; never abort the turn.
            dropped += 1
            continue
        blobs.append(converted)
    return blobs, dropped


def _collect_audio_frames(
    source: DebuggerSource, turn_id: str, *, track: str
) -> tuple[list[bytes], dict[str, int]]:
    """Return ``(pcm_blobs_in_order, format)`` for one turn's audio frames.

    ``track == "tts"`` stitches the bot's synthesized output: ``tts_frame``
    records carrying an ``output_ref`` artifact, ordered by sequence, and the
    format is reconciled *strictly* (a mismatch raises :class:`ValueError`).

    ``track == "mic"`` stitches the caller's captured input: the STT stage's
    ``stage_start`` records (``data.stage == "stt"``) carrying an
    ``input_ref`` artifact, ordered by sequence.  The format is reconciled
    *leniently* — mismatched blobs are best-effort converted or skipped so a
    ragged caller capture never aborts the response.

    ``track == "reference"`` stitches the AEC far-end reference: the bot
    playback fed to the echo canceller, journaled as ``aec_reference_frame``
    records carrying an ``output_ref`` artifact, ordered by sequence.  It is
    reconciled *leniently* (like ``mic``) so a ragged reference capture never
    aborts the AEC waveform strip.

    Streaming concat reads this and writes the WAV header up-front, then
    pushes each PCM blob to the response without buffering the whole stream in
    memory.
    """
    is_tts = track == _AUDIO_TRACK_TTS
    is_reference = track == _AUDIO_TRACK_REFERENCE
    frames: list[tuple[int, bytes, dict[str, Any]]] = []
    for r in source.records():
        if r.get("turn_id") != turn_id:
            continue
        data = r.get("data") or {}
        if not isinstance(data, dict):
            continue
        if is_tts:
            if r.get("name") != "tts_frame":
                continue
            ref = r.get("output_ref")
        elif is_reference:
            if r.get("name") != AEC_REFERENCE_FRAME_NAME:
                continue
            ref = r.get("output_ref")
        else:
            if r.get("name") != "stage_start" or data.get("stage") != "stt":
                continue
            ref = r.get("input_ref")
        if not ref:
            continue
        blob = source.artifact(ref)
        if blob is None:
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
    blobs, _dropped = _coerce_frames_to_format(frames, fmt, strict=is_tts)
    return blobs, fmt


def _collect_tts_frames(
    source: DebuggerSource, turn_id: str
) -> tuple[list[bytes], dict[str, int]]:
    """Return ``(pcm_blobs_in_order, format)`` for one turn's TTS frames.

    Thin back-compat wrapper over :func:`_collect_audio_frames` with the
    strict TTS track; raises ``ValueError`` on inconsistent PCM formats.
    """
    return _collect_audio_frames(source, turn_id, track=_AUDIO_TRACK_TTS)


def _collect_concat_pcm(
    source: DebuggerSource, turn_id: str, *, track: str
) -> tuple[bytes, dict[str, int]]:
    """Return one turn's audio as a single ``(raw_pcm, format)`` blob.

    Reuses :func:`_collect_audio_frames` and joins the per-frame blobs so
    the waveform endpoint can decode peaks without rebuilding a WAV.  The
    TTS track raises ``ValueError`` on inconsistent PCM formats; the mic and
    reference tracks are lenient (mismatched frames are dropped upstream).
    """
    frames, fmt = _collect_audio_frames(source, turn_id, track=track)
    return b"".join(frames), fmt


# ── AEC diagnostics ──────────────────────────────────────────────

# Default decode geometry used when the journal frames carry no explicit
# PCM format fields (debugger-internal fixtures, malformed captures).
_AEC_DEFAULT_FMT = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
_AEC_FRAME_MS = 20


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
) -> list[int]:
    """Map this turn's interruption records onto post-AEC frame indices.

    Each ``assistant_interruption_notified`` (or a ``turn_state_changed``
    transition *into* ``user_speaking`` while the bot was speaking) is placed at
    the frame whose monotonic timestamp it most closely follows, so self-echo
    detection can tell a true barge-in from the bot hearing itself.
    """
    if not post_aec:
        return []
    base_ns = post_aec[0]["mono_ns"]
    frame_span_ns = max(1, frame_ms) * 1_000_000
    total_frames = 0
    for entry in post_aec:
        total_frames += max(
            1,
            len(
                _frame_rms_series(
                    entry["pcm"],
                    frame_ms=frame_ms,
                )
            ),
        )
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


def _aec_diagnostics_for_turn(source: DebuggerSource, turn_id: str) -> dict[str, Any]:
    """Build the AEC diagnostics payload for one turn.

    Aligns mic-in / reference / post-AEC tracks on ``timing.mono_ns`` and
    derives ERLE, double-talk bands, and self-echo hits.  Degrades gracefully:
    a turn with no captured reference returns ``has_reference: False`` and empty
    diagnostics rather than raising.
    """
    records = source.records()
    tracks = _align_aec_tracks(records, source=source, turn_id=turn_id)
    mic_in = tracks["mic_in"]
    reference = tracks["reference"]
    post_aec = tracks["post_aec"]
    has_reference = bool(reference)

    fmt = _aec_track_format(post_aec or mic_in)
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
                "mic_in": {"frame_count": len(mic_in)},
                "reference": {"frame_count": len(reference)},
                "post_aec": {"frame_count": len(post_aec)},
            },
        }
    frame_ms = _AEC_FRAME_MS
    mic_pcm = b"".join(entry["pcm"] for entry in mic_in)
    ref_pcm = b"".join(entry["pcm"] for entry in reference)
    post_pcm = b"".join(entry["pcm"] for entry in post_aec)

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
    interruption_frames = _aec_interruption_frames(records, turn_id, post_aec, frame_ms=frame_ms)
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
        "tracks": {
            "mic_in": {"frame_count": len(mic_in), "byte_count": len(mic_pcm)},
            "reference": {"frame_count": len(reference), "byte_count": len(ref_pcm)},
            "post_aec": {"frame_count": len(post_aec), "byte_count": len(post_pcm)},
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
        frames.append((int(record.get("sequence") or 0), blob))
    frames.sort(key=lambda item: item[0])
    return [blob for _seq, blob in frames]


async def _vad_whatif_for_turn(
    source: DebuggerSource, turn_id: str, *, threshold: float
) -> dict[str, Any]:
    """Re-run VAD over a turn's captured input at an alternate sensitivity.

    ``threshold`` is the alternate VAD *sensitivity* (0..1, higher = more
    sensitive).  Returns ``{"threshold", "baseline_starts", "whatif_starts",
    "false_trigger_delta"}``.  Raises :class:`RuntimeError` when the VAD
    provider cannot be imported (the handler maps it to a 422 degrade).
    """
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.events import VADStartSpeaking
    from easycat.vad.factory import VADConfig, create_vad

    records = source.records()
    baseline = _vad_baseline_start_count(records, turn_id)
    blobs = _vad_whatif_frames(source, turn_id)

    try:
        provider = create_vad(VADConfig(sensitivity=threshold))
    except Exception as exc:  # noqa: BLE001 - import/availability degrade → 422
        raise RuntimeError(f"VAD provider unavailable: {exc}") from exc

    whatif_starts = 0
    for blob in blobs:
        chunk = AudioChunk(data=blob, format=PCM16_MONO_16K)
        async for event in provider.process(chunk):
            if isinstance(event, VADStartSpeaking):
                whatif_starts += 1
    return {
        "threshold": threshold,
        "baseline_starts": baseline,
        "whatif_starts": whatif_starts,
        "false_trigger_delta": whatif_starts - baseline,
    }


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


def _turn_bundle_zip_from_session(session: Any, turn_id: str) -> Path | None:
    """Build a single-turn slice ZIP for a live session and return its path.

    Exports the full session bundle to a temp file, reloads it, and writes a
    self-contained slice for *turn_id* via
    :func:`easycat.debug.export.export_turn_bundle`.  Loopback-only, so no
    redaction is applied — the slice carries raw transcripts/audio exactly
    like the full export.  Returns ``None`` when the session has no journal
    (debug='off'); raises :class:`ValueError` when the turn is absent so the
    handler can map it to a 404.
    """
    import tempfile

    from easycat.debug.export import export_turn_bundle

    full_path = _bundle_zip_from_session(session)
    if full_path is None:
        return None
    try:
        bundle = RunBundle.load(full_path)
    finally:
        _safe_unlink(full_path)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        export_turn_bundle(bundle, turn_id, tmp_path)
    except Exception:
        _safe_unlink(tmp_path)
        raise
    return tmp_path


def _records_since(
    source: DebuggerSource, after_seq: int, cap: int
) -> tuple[list[dict[str, Any]], int]:
    """Return up to *cap* records with ``sequence > after_seq``.

    Used by the live WebSocket loop to push only the records that arrived
    since the last batch.  Delegates to :meth:`DebuggerSource.records_since`,
    which pushes the bound down to the journal for live sources (so an
    idle/caught-up tick never re-reads or re-serializes the whole journal) and
    slices the cached list for bundle/static sources.  Records arrive ascending
    by sequence, so no re-sort is needed; the second return value is the new
    high-water cursor (the last sequence actually pushed) so the caller advances
    correctly even when the batch is capped mid-burst.
    """
    batch = source.records_since(after_seq, cap)
    if not batch:
        return [], after_seq
    return batch, int(batch[-1]["sequence"])


# ── HTTP API ─────────────────────────────────────────────────────


def _make_app(source: DebuggerSource, *, allow_remote: bool = False) -> Any:
    """Build the aiohttp Application with all routes wired up."""
    try:
        from aiohttp import WSMsgType, web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(DEBUGGER_INSTALL_HINT) from exc

    static_dir = Path(__file__).parent / "static"

    _STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def _hostname_is_loopback(hostname: str | None) -> bool:
        if not hostname:
            return False
        normalized = hostname.strip().lower()
        if normalized == "localhost":
            return True
        try:
            return ip_address(normalized).is_loopback
        except ValueError:
            return False

    def _origin_is_safe(origin: str) -> bool:
        if not origin:
            return False
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"}:
            return False
        return _hostname_is_loopback(parsed.hostname)

    def _host_is_safe(host: str) -> bool:
        if not host:
            return False
        parsed = urlsplit(f"//{host}")
        return _hostname_is_loopback(parsed.hostname)

    @web.middleware
    async def _origin_guard(request: Any, handler: Any) -> Any:
        """Refuse cross-origin requests on the loopback default.

        Three checks layered for defense-in-depth:

        1. ``Host`` must be an exact loopback hostname/address, blocking
           DNS-rebinding hostnames such as ``localhost.attacker.example``.
        2. ``Origin`` header, when present, must parse to an exact loopback
           hostname/address. Browsers always send Origin on cross-origin
           fetches, ws upgrades, and POST.
        3. ``Sec-Fetch-Site`` (set by all modern browsers) must be
           ``same-origin``, ``same-site``, or ``none`` (top-level nav).
           Any cross-site value is refused regardless of Origin.
        4. State-changing methods (POST/PUT/PATCH/DELETE) require an
           ``application/json`` content type and a present, safe
           Origin — kills the simple-form-POST CSRF vector that
           browsers wave through without preflight.

        ``allow_remote=True`` disables all three: callers who want
        network exposure are on their own.
        """
        if allow_remote:
            return await handler(request)
        host = request.headers.get("Host", "")
        origin = request.headers.get("Origin", "")
        site = request.headers.get("Sec-Fetch-Site", "")
        if not _host_is_safe(host):
            return web.Response(status=403, text="non-loopback host refused")
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
        query = params.get("q") or None
        use_regex = params.get("regex") == "1"
        errors_only = params.get("errors") == "1"
        scan_truncated = False
        try:
            if query is None:
                page, total = _filter_and_paginate(
                    source.records(),
                    stage=params.get("stage") or None,
                    turn_id=params.get("turn") or None,
                    name=names or None,
                    from_seq=from_seq,
                    to_seq=to_seq,
                    errors_only=errors_only,
                    limit=limit,
                    offset=offset,
                )
            else:
                # Filter first (no pagination), full-text search the subset, then
                # paginate the matches so "X of N" reflects the search result set.
                subset = _filter_records(
                    source.records(),
                    stage=params.get("stage") or None,
                    turn_id=params.get("turn") or None,
                    name=names or None,
                    from_seq=from_seq,
                    to_seq=to_seq,
                    errors_only=errors_only,
                    limit=None,
                    offset=0,
                )
                # Offload the full-text scan to a worker thread: a regex
                # search compiles a user-supplied pattern (q=...&regex=1) and
                # runs re.search over up to _SEARCH_SCAN_LIMIT records, so a
                # catastrophic-backtracking pattern must not block the event
                # loop. The substring path is offloaded too for uniformity.
                matched, scan_truncated = await asyncio.to_thread(
                    _search_records, subset, query=query, use_regex=use_regex
                )
                total = len(matched)
                page = matched[offset:]
                if limit is not None:
                    page = page[:limit]
        except ValueError as exc:
            logger.warning("Invalid records query: %s", exc)
            text = "invalid regex" if str(exc) == "invalid regex" else "invalid query parameters"
            return web.Response(status=400, text=text)
        return web.json_response(
            {
                "records": page,
                "page_size": len(page),
                "total": total,
                "offset": offset,
                "limit": limit,
                "scan_truncated": scan_truncated,
            }
        )

    async def turns(_request: Any) -> Any:
        return web.json_response({"turns": _summarise_turns(source.records())})

    async def timeline(_request: Any) -> Any:
        # ``turn_waterfall`` carries the same stage spans as ``build_timeline``
        # (so the existing SPA span rendering is unaffected) plus the per-turn
        # ``milestones`` the critical-path panel needs.
        return web.json_response({"timeline": _turn_waterfall(source.records())})

    async def transcript(_request: Any) -> Any:
        return web.json_response({"transcripts": _build_transcript(source.records())})

    async def cost(_request: Any) -> Any:
        manifest = source.manifest()
        config_snapshot = manifest.get("config_snapshot") if isinstance(manifest, dict) else None
        return web.json_response(_cost_rollup(source.records(), config_snapshot=config_snapshot))

    async def issues(_request: Any) -> Any:
        return web.json_response(
            _build_issues(source.records(), artifact_resolver=source.artifact)
        )

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
        track = request.query.get("track", _AUDIO_TRACK_TTS)
        if track not in _VALID_AUDIO_TRACKS:
            return web.Response(
                status=400,
                text=f"invalid track; expected one of {sorted(_VALID_AUDIO_TRACKS)}",
            )
        try:
            frames, fmt = _collect_audio_frames(source, turn_id, track=track)
        except ValueError as exc:
            # Only the strict TTS track raises; the mic track skips mismatched
            # blobs instead, so a 409 here always means a bot-output format
            # clash that we refuse to silently splice.
            logger.warning("Cannot assemble %s audio for %s: %s", track, turn_id, exc)
            return web.Response(status=409, text="cannot assemble audio for this turn")
        if not frames:
            return web.Response(status=404, text=f"no {track} frames for turn")
        # 8-bit mu-law telephony (sample_width == 1) can't be losslessly
        # wrapped as linear PCM WAV here — decoding it as int8 would emit
        # garbage.  Surface a clear unsupported result instead.
        if not _is_supported_width(fmt.get("sample_width", 2)):
            return web.json_response(
                {
                    "unsupported": True,
                    "reason": (
                        "unsupported audio format for concat: "
                        f"sample_width={fmt.get('sample_width')} "
                        "(8-bit/mu-law telephony audio is not decodable here)"
                    ),
                    "format": fmt,
                },
                status=415,
            )
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

    async def audio_waveform(request: Any) -> Any:
        """Render one turn's audio as a greyscale waveform PNG.

        Cheap ``<img>`` source for the Live waveform strip — the SPA Canvas
        path decodes the WAV itself, but the Live view shares one strip per
        turn and an ``<img>`` is far lighter than per-pixel JS.
        """
        try:
            turn_id = _safe_turn_id(request.match_info["turn"])
        except ValueError:
            return web.Response(status=400, text="invalid turn_id")
        track = request.query.get("track", _AUDIO_TRACK_TTS)
        if track not in _VALID_AUDIO_TRACKS:
            return web.Response(
                status=400,
                text=f"invalid track; expected one of {sorted(_VALID_AUDIO_TRACKS)}",
            )

        def _bounded(name: str, default: int, hi: int) -> int:
            try:
                value = int(request.query.get(name, default))
            except (TypeError, ValueError):
                return default
            return max(1, min(hi, value))

        width = _bounded("w", 600, 2000)
        height = _bounded("h", 80, 400)
        try:
            pcm, fmt = _collect_concat_pcm(source, turn_id, track=track)
        except ValueError as exc:
            logger.warning("Cannot render %s waveform for %s: %s", track, turn_id, exc)
            return web.Response(status=409, text="cannot assemble audio for this turn")
        if not pcm:
            return web.Response(status=404, text=f"no {track} frames for turn")
        sample_width = int(fmt.get("sample_width", 2) or 2)
        # 8-bit mu-law (sample_width == 1) decodes to nothing in the shared PCM
        # decoder; rather than paint a misleading flat/garbage strip, return a
        # clear unsupported result so the SPA can show a placeholder.
        if not _is_supported_width(sample_width):
            return web.json_response(
                {
                    "unsupported": True,
                    "reason": (
                        "unsupported audio format for waveform: "
                        f"sample_width={sample_width} "
                        "(8-bit/mu-law telephony audio is not decodable here)"
                    ),
                    "format": fmt,
                },
                status=415,
            )
        peaks = decode_pcm_peaks(
            pcm,
            sample_width=sample_width,
            channels=fmt.get("channels", 1),
            buckets=width,
        )
        png = encode_peaks_png(
            peaks,
            width=width,
            height=height,
            full_scale_value=_full_scale(sample_width),
        )
        return web.Response(
            body=png,
            content_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    async def aec_diagnostics(request: Any) -> Any:
        """AEC diagnostics for one turn: ERLE, double-talk, self-echo, tracks.

        Reads the aligned mic-in / reference / post-AEC tracks from the journal
        and derives the echo-cancellation health metrics.  ``has_reference`` is
        ``False`` (with empty diagnostics) when AEC was disabled or no reference
        frames were captured, rather than 404'ing — the SPA always gets a
        well-formed shape.
        """
        try:
            turn_id = _safe_turn_id(request.match_info["turn"])
        except ValueError:
            return web.Response(status=400, text="invalid turn_id")
        return web.json_response(_aec_diagnostics_for_turn(source, turn_id))

    async def aec_vad_whatif(request: Any) -> Any:
        """Re-run VAD at an alternate sensitivity over a turn's captured input.

        Bundle-only (mirrors replay's ``supports_replay`` 405 for live sources):
        the captured ``stage_start`` input refs only exist on a settled bundle.
        ``_origin_guard`` already enforces JSON content-type + a present Origin
        on this POST.  A missing VAD provider degrades to 422 rather than 500.
        """
        if source.is_live:
            return web.Response(status=405, text="vad-whatif is only supported for bundle sources")
        try:
            turn_id = _safe_turn_id(request.match_info["turn"])
        except ValueError:
            return web.Response(status=400, text="invalid turn_id")
        raw = request.query.get("threshold")
        try:
            threshold = float(raw) if raw is not None else 0.5
        except (TypeError, ValueError):
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "threshold must be a number"},
                status=400,
            )
        if not (0.0 <= threshold <= 1.0):
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "threshold must be between 0 and 1"},
                status=400,
            )
        try:
            result = await _vad_whatif_for_turn(source, turn_id, threshold=threshold)
        except RuntimeError as exc:
            return web.json_response(
                {"error_code": "VAD_UNAVAILABLE", "message": str(exc)}, status=422
            )
        return web.json_response(result)

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
        # ``?turn=<id>`` writes a single-turn replayable slice (the SPA
        # "Save as test case" button); no turn writes the whole session.
        raw_turn = request.query.get("turn")
        download_name = "session.zip"
        if raw_turn is not None:
            try:
                turn_id = _safe_turn_id(raw_turn)
            except ValueError as exc:
                return web.json_response(
                    {"error_code": "BAD_REQUEST", "message": str(exc)}, status=400
                )
            export_turn_fn = getattr(source, "_export_turn_fn", None)
            if export_turn_fn is None:
                return web.Response(status=503, text="no turn-export function bound")
            try:
                tmp_path = export_turn_fn(turn_id)
            except ValueError:
                return web.Response(status=404, text="no records for that turn")
            except Exception:  # noqa: BLE001 - never hide export errors
                logger.exception("Turn export failed")
                return web.Response(status=500, text="export failed")
            download_name = f"turn-{turn_id}.zip"
        else:
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
                "Content-Disposition": f"attachment; filename={download_name}",
            },
        )
        # Schedule cleanup once aiohttp has finished sending.
        loop = asyncio.get_running_loop()
        loop.call_later(60.0, _safe_unlink, tmp_path)
        return response

    async def annotations(_request: Any) -> Any:
        """Return the per-turn verdict sidecar map for a bundle source.

        Bundle-only — live sessions carry no on-disk sidecar.  A missing or
        corrupt sidecar yields an empty map (``load_annotations`` tolerates
        both) so the SPA always gets a well-formed response.
        """
        annotate_path = source._annotate_path
        if annotate_path is None:
            return web.Response(status=405, text="annotations only supported for bundle sources")
        return web.json_response({"annotations": load_annotations(annotate_path)})

    async def annotate(request: Any) -> Any:
        """Persist a per-turn pass/fail verdict into the bundle's sidecar.

        Bundle-only; the journal and the bundle ZIP on disk are never
        touched.  ``_origin_guard`` already enforces JSON content-type and a
        present, safe Origin on this POST, so we only validate the payload.
        """
        annotate_path = source._annotate_path
        if annotate_path is None:
            return web.Response(status=405, text="annotate only supported for bundle sources")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed body → 400, never 500
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "request body must be JSON"},
                status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "request body must be a JSON object"},
                status=400,
            )
        try:
            turn_id = _safe_turn_id(str(body.get("turn_id", "")))
        except ValueError as exc:
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": str(exc)}, status=400
            )
        try:
            annotation = Annotation(
                turn_id=turn_id,
                passed=body.get("passed"),
                failure_type=body.get("failure_type"),
                score=body.get("score"),
                notes=str(body.get("notes") or ""),
            )
        except AnnotationError as exc:
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": str(exc)}, status=400
            )
        try:
            record = save_annotation(annotate_path, annotation)
        except OSError:
            logger.exception("Annotation write failed")
            return web.Response(status=500, text="annotation write failed")
        return web.json_response({"turn_id": turn_id, "annotation": record})

    async def refresh(_request: Any) -> Any:
        return web.json_response({"snapshot_size": len(source.records())})

    async def healthcheck(_request: Any) -> Any:
        return web.json_response({"ok": True, "is_live": source.is_live})

    async def websocket(request: Any) -> Any:
        """Push live updates to the UI.

        Sends a snapshot every poll interval (live sources) or once
        (bundle sources).  On a sequence advance it also pushes a capped,
        only-new ``{"type": "records"}`` batch so the live-follow playhead
        can append records without re-fetching the whole journal.  Clients
        can send ``{"action": "ping"}`` to keep the connection alive; we
        respond with ``pong``.
        """
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        last_seq = -1
        last_pushed_seq = 0
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
                # Drain new records independently of the snapshot guard: slice
                # only the records beyond the last pushed sequence, bounded by a
                # per-tick cap so a burst can't push a multi-megabyte frame.  A
                # burst larger than the cap is delivered in capped slices across
                # successive ticks — keep draining while last_pushed_seq lags
                # latest_seq, even when latest_seq itself stops advancing, so the
                # follow-now playhead never permanently loses the tail of a burst.
                if latest_seq > last_pushed_seq:
                    new_records, last_pushed_seq = _records_since(
                        source, last_pushed_seq, _WS_RECORD_BATCH_CAP
                    )
                    if new_records:
                        await ws.send_json(
                            {
                                "type": "records",
                                "records": new_records,
                                "from_seq": new_records[0].get("sequence"),
                                "to_seq": last_pushed_seq,
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
    app.router.add_get("/api/issues", issues)
    app.router.add_get("/api/artifact/{ref}", artifact)
    app.router.add_get("/api/audio/concat/{turn}", audio_concat)
    app.router.add_get("/api/audio/waveform/{turn}", audio_waveform)
    app.router.add_get("/api/aec/{turn}", aec_diagnostics)
    app.router.add_post("/api/aec/{turn}/vad-whatif", aec_vad_whatif)
    app.router.add_post("/api/replay", replay)
    app.router.add_post("/api/export", export)
    app.router.add_post("/api/annotate", annotate)
    app.router.add_get("/api/annotations", annotations)
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


def serve_run_bundle(
    bundle: RunBundle,
    *,
    label: str = "bundle",
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_remote: bool = False,
) -> None:
    """Serve the debugger UI for an already loaded :class:`RunBundle`.

    This is useful for SQLite journals loaded through
    :meth:`RunBundle.from_partial_journal`, where there is no ZIP bundle path
    for :func:`serve_bundle` to reopen.
    """
    _check_host(host, allow_remote)
    source = _run_bundle_source(bundle, label=label)
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
    # ``/api/export?turn=<id>`` slices a single replayable turn out of the
    # live session; loopback-only, so the slice carries raw audio unredacted.
    source._export_turn_fn = (  # type: ignore[attr-defined]
        lambda turn_id: _turn_bundle_zip_from_session(session, turn_id)
    )
    if not in_thread:
        # Synchronous serve: bind happens inside ``_serve`` → ``run_app`` and a
        # collision raises here on the calling thread already. The browser opens
        # only after that bind succeeds (handled inside ``_serve``).
        _serve(
            source,
            host=host,
            port=port,
            open_browser=open_browser,
            allow_remote=allow_remote,
        )
        return None

    # Probe-bind on the calling thread *before* starting the daemon so a
    # port-in-use collision surfaces synchronously to the caller instead of
    # exploding inside the thread after we've returned. Only after the probe
    # succeeds do we open the browser — a session whose bind fails must open
    # no tab.
    _probe_bind(host, port)

    thread = threading.Thread(
        target=_serve,
        args=(source,),
        kwargs={
            "host": host,
            "port": port,
            # The probe above already confirmed the port is free and we open the
            # browser here; the threaded serve must not open it a second time.
            "open_browser": False,
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
    if open_browser:
        _open_browser(f"http://{host}:{port}/")
    return thread


def _probe_bind(host: str, port: int) -> None:
    """Bind ``(host, port)`` once and release it so collisions surface here.

    ``serve_session(..., in_thread=True)`` runs ``web.run_app`` on a daemon
    thread, so a port-already-in-use ``OSError`` would otherwise fire *after*
    ``serve_session`` has already returned — an unhandled exception in a
    background thread that the autolaunch try/except can never catch, and a
    browser tab that was already opened. Probing the bind synchronously before
    the server thread starts (and before any ``webbrowser.open``) lets the
    failure propagate to the caller. There is an inherent TOCTOU window between
    this probe and the server's own bind, but for the loopback dev-debugger the
    common "second concurrent session, same port" case is caught cleanly.

    The probe only runs for loopback hosts and binds an explicit loopback IP —
    never all-interfaces (``0.0.0.0``/``""``). A non-loopback bind is an
    explicit ``allow_remote`` opt-in (already vetted by ``_check_host``); we
    skip the pre-probe there and let ``run_app`` surface any bind error.
    """
    if host not in _LOOPBACK_HOSTS:
        return
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    # Bind a concrete loopback address (not the caller's string, which static
    # analysis cannot prove is loopback) so the probe can never bind to all
    # interfaces; this still detects a same-port collision with another local
    # server.
    bind_host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: we want this probe to fail loudly when the port is
        # already taken by another live server, mirroring what run_app sees.
        probe.bind((bind_host, port))
    finally:
        probe.close()


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


def _open_browser(url: str) -> None:
    """Best-effort ``webbrowser.open`` that never raises into the caller."""
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - depends on env
        logger.debug("Could not open browser automatically", exc_info=True)


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
        raise RuntimeError(DEBUGGER_INSTALL_HINT) from exc

    # Probe-bind before building the app or opening any browser tab so a port
    # collision in the synchronous-serve path raises here (before
    # ``webbrowser.open``) instead of popping a tab that points at a server
    # that never came up.
    _probe_bind(host, port)
    app = _make_app(source, allow_remote=allow_remote)
    url = f"http://{host}:{port}/"
    logger.info("EasyCat debugger UI serving on %s (source=%s)", url, source.label)
    if open_browser:
        _open_browser(url)
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
        raise RuntimeError(DEBUGGER_INSTALL_HINT) from exc

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
        raise RuntimeError(DEBUGGER_INSTALL_HINT) from exc
