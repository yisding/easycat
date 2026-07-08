"""aiohttp-backed debugger server.

Adapts a :class:`DebuggerSource` (a bundle on disk, an in-memory
:class:`RunBundle`, or a live :class:`Session`) into a JSON HTTP API,
WebSocket push channel, and single-page HTML UI rendering the
timeline, per-stage waterfall, pipeline graph, transcript, audio
playback, replay surface, and bundle export.

Routes:

- ``GET  /``                          — static HTML page
- ``GET  /api/manifest``              — bundle/session metadata
- ``GET  /api/records``               — journal records (filterable; ``?q=`` text, ``&regex=1``)
- ``GET  /api/turns``                 — per-turn rollup with stage counts
- ``GET  /api/timeline``              — per-stage span timing per turn
- ``GET  /api/transcript``            — extracted user/agent text per turn
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
import json
import logging
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from easycat._net import is_loopback_host
from easycat.debug._issues import build_issues as _build_issues
from easycat.debug._pcm import full_scale as _full_scale
from easycat.debug._pcm import is_supported_width as _is_supported_width
from easycat.debug._turn_timeline import build_timeline as _build_timeline  # noqa: F401
from easycat.debug._turn_timeline import summarise_turns as _summarise_turns
from easycat.debug._turn_timeline import turn_waterfall as _turn_waterfall
from easycat.debug.annotations import (
    Annotation,
    AnnotationError,
    load_annotations,
    save_annotation,
)
from easycat.debug.bundle import RunBundle

# AEC diagnostics + VAD what-if payload builders were split into
# ``_aec_routes.py`` (QS3). Re-exported here so the historical
# ``from easycat.debugger.server import _helper`` sites keep resolving.
# ``_AEC_MAX_TRACK_BYTES`` is monkeypatched by tests, so it lives physically in
# ``_aec_routes`` (patch its real home, not this module).
from easycat.debugger._aec_routes import (
    _aec_diagnostics_for_turn,
    _aec_interruption_frames,
    _aec_track_format,
    _limit_aec_track,
    _vad_baseline_start_count,
    _vad_whatif_frames,
)

# PCM/WAV/frame coercion helpers were split into ``_audio.py`` (QS3). Re-exported
# here so the historical ``from easycat.debugger.server import _helper`` sites
# keep resolving. ``_AUDIO_MAX_CONVERTED_FRAME_BYTES`` is monkeypatched by tests,
# so it lives physically in ``_audio`` (patch its real home, not this alias).
from easycat.debugger._audio import (
    _AUDIO_DEFAULT_FMT,
    _AUDIO_MAX_CONVERTED_FRAME_BYTES,
    _AUDIO_MAX_RESAMPLE_RATIO,
    _AUDIO_MAX_SAMPLE_RATE,
    _AUDIO_MIN_SAMPLE_RATE,
    _AUDIO_VALID_CHANNELS,
    _audio_metadata_int,
    _coerce_frames_to_format,
    _is_safe_audio_format,
    _np_pcm_dtype,
    _np_ratecv,
    _np_tomono,
    _project_converted_pcm_bytes,
    _safe_audio_format_from_metadata,
    _serialize_frame,
    _wav_header,
)
from easycat.debugger._install_hint import DEBUGGER_INSTALL_HINT

# Record filtering / full-text search / transcript / record coercion helpers
# were split into ``_records.py`` (QS3). Re-exported here so the historical
# ``from easycat.debugger.server import _helper`` import sites keep resolving.
from easycat.debugger._records import (
    _SEARCH_MAX_QUERY_LEN,
    _SEARCH_SCAN_LIMIT,
    _UNSAFE_REGEX_MESSAGE,
    _build_transcript,
    _compile_search_regex,
    _filter_and_paginate,
    _filter_records,
    _record_match_fields,
    _record_searchable_text,
    _record_to_dict,
    _regex_tree_has_unsafe_backtracking,
    _search_records,
)

# Source adaptation (DebuggerSource + bundle/session sources and ref/turn-id
# validators) was split into ``_sources.py`` (QS3). Re-exported here so the
# historical ``from easycat.debugger.server import _helper`` sites keep
# resolving. ``_REPLAY_FRAME_LIMIT`` is monkeypatched by tests, so it lives
# physically in ``_sources`` (patch its real home, not this module).
from easycat.debugger._sources import (
    DebuggerSource,
    _bundle_source,
    _run_bundle_source,
    _safe_ref,
    _safe_turn_id,
    _session_source,
    _validated_replay_kwargs,
)
from easycat.debugger._waveform import decode_pcm_peaks, encode_peaks_png
from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME

logger = logging.getLogger(__name__)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

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


# ── Pure helpers (record filtering / rollups) ────────────────────
# The record filtering, full-text search, transcript, and record/error
# coercion helpers now live in ``easycat.debugger._records`` and are
# re-exported at the top of this module (QS3 split).


def _record_sequence(record: dict[str, Any]) -> int | None:
    seq = record.get("sequence")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


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
        seq = _record_sequence(r)
        if seq is None:
            continue
        frames.append((seq, blob, data))

    if not frames:
        return [], {}

    frames.sort(key=lambda item: item[0])
    fmt0 = frames[0][2]
    fmt = _safe_audio_format_from_metadata(fmt0)
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


def _should_reset_live_follow(prev_epoch: int | None, cur_epoch: int) -> bool:
    """Whether the WS live-follow cursor should reset (the selection changed).

    Pure so it is unit-testable in isolation: resets iff a prior epoch was seen
    and the current epoch differs (a dev-registry session switch). The first
    tick (``prev_epoch is None``) never resets — it is the initial snapshot.
    """
    return prev_epoch is not None and cur_epoch != prev_epoch


def _session_overview_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce one session's journal records to cheap, journal-only triage stats.

    No audio decode and no issue analysis — just the per-turn rollups
    (:func:`summarise_turns`) summed: turn count, total errors/interruptions,
    and the wall time of the most recent turn. Powers the cross-session overview
    strip so a developer can see which of N concurrent calls is hot or erroring.
    """
    turns = _summarise_turns(records)
    error_count = sum(int(t.get("error_count", 0) or 0) for t in turns)
    interruption_count = sum(int(t.get("interruption_count", 0) or 0) for t in turns)
    last_turn_wall_ms = 0.0
    if turns:
        first_ns = turns[-1].get("first_wall_ns")
        last_ns = turns[-1].get("last_wall_ns")
        if isinstance(first_ns, (int, float)) and isinstance(last_ns, (int, float)):
            last_turn_wall_ms = max(0.0, (last_ns - first_ns) / 1e6)
    return {
        "turn_count": len(turns),
        "error_count": error_count,
        "interruption_count": interruption_count,
        "last_turn_wall_ms": round(last_turn_wall_ms, 1),
    }


# ── Dev-mode registry adaptation ─────────────────────────────────


_EMPTY_DEV_SOURCE_LABEL = "no-session"


def _empty_dev_source() -> DebuggerSource:
    """A well-formed empty source for when no live session is selected.

    Every panel renders against zero records rather than 500'ing before the
    developer has picked a session from the selector.
    """
    return DebuggerSource(
        label=_EMPTY_DEV_SOURCE_LABEL,
        _records_fn=lambda: [],
        _progress_fn=lambda: (0, 0),
        _artifact_fn=lambda _ref: None,
        _manifest_fn=lambda: {
            "source": "dev",
            "session_id": "",
            "is_live": False,
            "supports_replay": False,
            "supports_export": False,
            "supports_annotate": False,
            "active_session": None,
            "replay_entry_points": [],
        },
        _bundle_fn=None,
        _replay_fn=None,
        is_live=False,
    )


class _DevDebuggerState:
    """Per-app dev state: the selected session and the live proxy source.

    Holds a process-local :class:`SessionIndex` and the currently selected
    registry id. ``proxy_source()`` returns a single :class:`DebuggerSource`
    whose accessors resolve, on every call, against the selected session's
    source — so the standard routes (records/timeline/cost/…) follow the
    selector without being rebuilt.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._active_id: str | None = None
        # Bumps whenever the active session changes (explicit select OR the
        # single-session auto-select), so the WS loop knows to reset live-follow.
        self._selection_epoch = 0

    @property
    def registry(self) -> Any:
        return self._registry

    def selection_epoch(self) -> int:
        return self._selection_epoch

    def _set_active(self, registry_id: str | None) -> None:
        """Point at *registry_id*, bumping the selection epoch on a real change."""
        if registry_id != self._active_id:
            self._active_id = registry_id
            self._selection_epoch += 1

    def select(self, registry_id: str | None) -> bool:
        """Set the active registry id. Returns ``True`` when it resolves."""
        if registry_id is None:
            self._set_active(None)
            return True
        if self._registry.get(registry_id) is None:
            return False
        self._set_active(registry_id)
        return True

    def active_id(self) -> str | None:
        # Auto-select the only running session so a single-session dev run shows
        # data immediately without a manual selector click.
        if self._active_id is not None and self._registry.get(self._active_id) is not None:
            return self._active_id
        sessions = self._registry.list()
        if len(sessions) == 1:
            self._set_active(sessions[0].registry_id)
            return self._active_id
        return None

    def active_session(self) -> Any | None:
        active = self.active_id()
        return self._registry.get(active) if active is not None else None

    def _active_source(self) -> DebuggerSource:
        session = self.active_session()
        if session is None:
            return _empty_dev_source()
        source = _session_source(session)
        # Wire the same live-export hooks ``serve_session`` installs so the
        # export paths work for the selected session.
        source._export_fn = lambda s=session: _bundle_zip_from_session(s)  # type: ignore[attr-defined]
        source._export_turn_fn = (  # type: ignore[attr-defined]
            lambda turn_id, s=session: _turn_bundle_zip_from_session(s, turn_id)
        )
        return source

    def proxy_source(self) -> DebuggerSource:
        """Return a DebuggerSource that delegates to the active session source."""
        state = self

        def _manifest() -> dict[str, Any]:
            payload = state._active_source().manifest()
            payload["source"] = payload.get("source", "dev")
            payload["dev_mode"] = True
            payload["active_session"] = state.active_id()
            return payload

        proxy = DebuggerSource(
            label="dev-registry",
            _records_fn=lambda: state._active_source().records(),
            _progress_fn=lambda: state._active_source().progress(),
            _records_since_fn=lambda after, cap: state._active_source().records_since(after, cap),
            _artifact_fn=lambda ref: state._active_source().artifact(ref),
            _manifest_fn=_manifest,
            _bundle_fn=lambda: state._active_source().bundle(),
            _replay_fn=None,
            _selection_epoch_fn=state.selection_epoch,
            is_live=True,
        )
        # The export route reads these hooks off the bound source (the proxy);
        # delegate them to the live active session via its export-aware source.
        proxy._export_fn = lambda: getattr(  # type: ignore[attr-defined]
            state._active_source(), "_export_fn", lambda: None
        )()
        proxy._export_turn_fn = lambda turn_id: getattr(  # type: ignore[attr-defined]
            state._active_source(), "_export_turn_fn", lambda _t: None
        )(turn_id)
        return proxy


# ── HTTP API ─────────────────────────────────────────────────────


def _make_app(
    source: DebuggerSource,
    *,
    allow_remote: bool = False,
    registry: Any | None = None,
) -> Any:
    """Build the aiohttp Application with all routes wired up.

    When ``registry`` is a
    :class:`~easycat.debugger.session_registry.SessionIndex`, the dev-mode
    routes (``/api/dev/sessions``, ``/api/dev/select``) are mounted and *source*
    is replaced by a live proxy that follows the registry session the developer
    selects. All other routes are unchanged — they read through the proxy, so
    switching the active session re-points the whole UI.
    """
    try:
        from aiohttp import WSMsgType, web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(DEBUGGER_INSTALL_HINT) from exc

    static_dir = Path(__file__).parent / "static"

    dev = _DevDebuggerState(registry) if registry is not None else None
    if dev is not None:
        # All existing routes read through ``source``; swap in the live proxy so
        # selecting a different session re-points every panel at once.
        source = dev.proxy_source()

    _STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def _origin_is_safe(origin: str) -> bool:
        if not origin:
            return False
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"}:
            return False
        return is_loopback_host(parsed.hostname)

    def _host_is_safe(host: str) -> bool:
        if not host:
            return False
        parsed = urlsplit(f"//{host}")
        return is_loopback_host(parsed.hostname)

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
            if offset < 0:
                raise ValueError("offset must be >= 0")
            if limit is not None and limit <= 0:
                raise ValueError("limit must be > 0")
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
            if str(exc) in {"invalid regex", _UNSAFE_REGEX_MESSAGE}:
                text = str(exc)
            else:
                text = "invalid query parameters"
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

    async def issues(_request: Any) -> Any:
        return web.json_response(
            _build_issues(source.records(), artifact_resolver=source.artifact_for_analysis)
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
        try:
            payload = _aec_diagnostics_for_turn(source, turn_id)
        except MemoryError:
            logger.exception("AEC diagnostics exceeded available memory")
            return web.json_response(
                {
                    "error_code": "AEC_DIAGNOSTICS_TOO_LARGE",
                    "message": "AEC diagnostics too large",
                },
                status=413,
            )
        return web.json_response(payload)

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
        try:
            validated = _validated_replay_kwargs(payload)
        except ValueError as exc:
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": str(exc)}, status=400
            )
        force = validated.get("force", False)
        confirm = payload.pop("confirm", False) is True
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
        allowed_turn_ids = {
            turn["turn_id"]
            for turn in _summarise_turns(source.records())
            if isinstance(turn.get("turn_id"), str)
        }
        if turn_id not in allowed_turn_ids:
            return web.json_response(
                {
                    "error_code": "BAD_REQUEST",
                    "message": f"turn_id does not exist in bundle: {turn_id!r}",
                },
                status=400,
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
            record = save_annotation(annotate_path, annotation, allowed_turn_ids=allowed_turn_ids)
        except AnnotationError as exc:
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": str(exc)}, status=400
            )
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
        last_epoch: int | None = None
        last_sessions_version: int | None = None
        try:
            while not ws.closed:
                # Dev-registry session switch: when the developer re-points the
                # selector, the active session's journal sequence can be LOWER
                # than the one we were following, which would otherwise stall the
                # follow-cursor forever.  Reset the cursor and tell the UI to
                # clear so the newly selected session re-snapshots from scratch.
                cur_epoch = source.selection_epoch()
                if _should_reset_live_follow(last_epoch, cur_epoch):
                    last_seq = -1
                    last_pushed_seq = 0
                    await ws.send_json({"type": "reset", "selection_epoch": cur_epoch})
                last_epoch = cur_epoch
                # Dev-registry live selector: push the session list only when it
                # actually changed (the registry's O(1) version counter bumps on
                # register/unregister/prune), so the UI selector updates as calls
                # come and go without polling /api/dev/sessions.
                if dev is not None:
                    sessions_version = dev.registry.version()
                    if sessions_version != last_sessions_version:
                        last_sessions_version = sessions_version
                        await ws.send_json(
                            {
                                "type": "sessions",
                                "sessions": [s.to_dict() for s in dev.registry.list()],
                                "active_session": dev.active_id(),
                                "selection_epoch": cur_epoch,
                            }
                        )
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

    async def dev_sessions(_request: Any) -> Any:
        """List the live sessions the dev registry is tracking.

        Powers the UI session selector. Returns the active registry id so the
        selector can highlight the session every other panel is showing.
        """
        if dev is None:
            return web.Response(status=404, text="dev session registry not enabled")
        return web.json_response(
            {
                "sessions": [summary.to_dict() for summary in dev.registry.list()],
                "active_session": dev.active_id(),
            }
        )

    async def dev_select(request: Any) -> Any:
        """Switch the active session every panel renders against.

        ``_origin_guard`` already enforces JSON content-type + a present Origin
        on this POST. A ``null``/absent ``registry_id`` clears the selection.
        """
        if dev is None:
            return web.Response(status=404, text="dev session registry not enabled")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed body → 400, never 500
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "request body must be JSON"}, status=400
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "body must be a JSON object"}, status=400
            )
        registry_id = body.get("registry_id")
        if registry_id is not None and not isinstance(registry_id, str):
            return web.json_response(
                {"error_code": "BAD_REQUEST", "message": "registry_id must be a string or null"},
                status=400,
            )
        if not dev.select(registry_id):
            return web.json_response(
                {"error_code": "NOT_FOUND", "message": f"unknown session {registry_id!r}"},
                status=404,
            )
        return web.json_response({"active_session": dev.active_id()})

    async def dev_overview(_request: Any) -> Any:
        """Cross-session triage: per-session journal stats + an aggregate strip.

        At a glance: which of N concurrent live sessions is hot, idle, or
        erroring. Journal-derived only (no audio decode); a flaky session is
        skipped rather than 500'ing the whole strip, and zero sessions yields an
        empty-but-200 report.
        """
        if dev is None:
            return web.Response(status=404, text="dev session registry not enabled")
        sessions_out: list[dict[str, Any]] = []
        sessions_running = 0
        active_turns = 0
        errors_total = 0
        for summary in dev.registry.list():
            session = dev.registry.get(summary.registry_id)
            stats = {
                "turn_count": 0,
                "error_count": 0,
                "interruption_count": 0,
                "last_turn_wall_ms": 0.0,
            }
            if session is not None:
                try:
                    stats = _session_overview_stats(_session_source(session).records())
                except Exception:  # noqa: BLE001 - one flaky session must not 500 the strip
                    logger.debug(
                        "overview stats failed for %s", summary.registry_id, exc_info=True
                    )
            if summary.is_running:
                sessions_running += 1
            if summary.activity == "active":
                active_turns += 1
            errors_total += int(stats["error_count"])
            sessions_out.append({**summary.to_dict(), **stats})
        return web.json_response(
            {
                "summary": {
                    "sessions_total": len(sessions_out),
                    "sessions_running": sessions_running,
                    "active_turns": active_turns,
                    "errors_total": errors_total,
                },
                "sessions": sessions_out,
                "active_session": dev.active_id(),
            }
        )

    app = web.Application(middlewares=[_origin_guard])
    app.router.add_get("/", index)
    app.router.add_get("/api/manifest", manifest)
    app.router.add_get("/api/records", records)
    app.router.add_get("/api/turns", turns)
    app.router.add_get("/api/timeline", timeline)
    app.router.add_get("/api/transcript", transcript)
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
    # The dev-only registry routes are mounted only when a registry is attached
    # (EASYCAT_DEV / VoiceApp(dev=True)); a plain bundle/session app 404s them.
    if dev is not None:
        app.router.add_get("/api/dev/sessions", dev_sessions)
        app.router.add_post("/api/dev/select", dev_select)
        app.router.add_get("/api/dev/overview", dev_overview)
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


def serve_dev_registry(
    registry: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    in_thread: bool = False,
    allow_remote: bool = False,
) -> threading.Thread | None:
    """Serve the dev debugger UI backed by a live :class:`SessionIndex`.

    Unlike :func:`serve_session` (one fixed session), this serves a session
    *selector* over every session the process registers, so a browser/websocket
    server fanning out many concurrent sessions exposes one UI for all of them.
    Loopback-only by default (the journal can carry transcripts/audio); a
    non-loopback bind requires ``allow_remote=True``.

    Blocks the caller unless ``in_thread`` is set, in which case the server runs
    on a background daemon thread and the started thread is returned.
    """
    _check_host(host, allow_remote)
    # The app builds its own live proxy source from the registry; pass an empty
    # placeholder source so the (registry-driven) proxy takes over.
    source = _empty_dev_source()

    if not in_thread:
        _serve(
            source,
            host=host,
            port=port,
            open_browser=open_browser,
            allow_remote=allow_remote,
            registry=registry,
        )
        return None

    _probe_bind(host, port)
    thread = threading.Thread(
        target=_serve,
        args=(source,),
        kwargs={
            "host": host,
            "port": port,
            "open_browser": False,
            "allow_remote": allow_remote,
            "handle_signals": False,
            "registry": registry,
        },
        daemon=True,
        name="easycat-dev-debugger",
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
    registry: Any | None = None,
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
    app = _make_app(source, allow_remote=allow_remote, registry=registry)
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


# ``server.py`` stays a facade over the QS3 split modules; every private moved
# into ``_records.py`` / ``_audio.py`` / ``_sources.py`` / ``_aec_routes.py`` is
# re-exported here so the historical ``from easycat.debugger.server import
# _helper`` import sites (tests, ``cli/debug/grep.py``) keep resolving.
__all__ = [
    # Public entry points.
    "DebuggerSource",
    "run_app_async",
    "serve_bundle",
    "serve_dev_registry",
    "serve_run_bundle",
    "serve_session",
    # Re-exported from ``_records`` (record filtering / search / transcript).
    "_SEARCH_MAX_QUERY_LEN",
    "_SEARCH_SCAN_LIMIT",
    "_UNSAFE_REGEX_MESSAGE",
    "_build_transcript",
    "_compile_search_regex",
    "_filter_and_paginate",
    "_filter_records",
    "_record_match_fields",
    "_record_searchable_text",
    "_record_to_dict",
    "_regex_tree_has_unsafe_backtracking",
    "_search_records",
    # Re-exported from ``_audio`` (PCM/WAV/frame coercion).
    "_AUDIO_DEFAULT_FMT",
    "_AUDIO_MAX_CONVERTED_FRAME_BYTES",
    "_AUDIO_MAX_RESAMPLE_RATIO",
    "_AUDIO_MAX_SAMPLE_RATE",
    "_AUDIO_MIN_SAMPLE_RATE",
    "_AUDIO_VALID_CHANNELS",
    "_audio_metadata_int",
    "_coerce_frames_to_format",
    "_is_safe_audio_format",
    "_np_pcm_dtype",
    "_np_ratecv",
    "_np_tomono",
    "_project_converted_pcm_bytes",
    "_safe_audio_format_from_metadata",
    "_serialize_frame",
    "_wav_header",
    # Re-exported from ``_sources`` (source adaptation + validators).
    "_bundle_source",
    "_run_bundle_source",
    "_safe_ref",
    "_safe_turn_id",
    "_session_source",
    "_validated_replay_kwargs",
    # Re-exported from ``_aec_routes`` (AEC diagnostics + VAD what-if).
    "_aec_diagnostics_for_turn",
    "_aec_interruption_frames",
    "_aec_track_format",
    "_limit_aec_track",
    "_vad_baseline_start_count",
    "_vad_whatif_frames",
]
