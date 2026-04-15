"""aiohttp-backed debugger server.

The server adapts a :class:`DebuggerSource` (a bundle on disk, an
in-memory :class:`RunBundle`, or a live :class:`Session`) into a
small JSON HTTP API plus a single-page HTML UI that renders the
timeline, per-turn waterfall, and record inspector.

Routes:

- ``GET /``                — the static HTML page
- ``GET /api/manifest``    — bundle metadata
- ``GET /api/records``     — journal records (filterable by stage, turn,
  sequence range)
- ``GET /api/turns``       — per-turn rollup with waterfall timings
- ``GET /api/artifact/<ref>`` — raw artifact bytes for audio playback
- ``GET /api/refresh``     — re-snapshot a live source (no-op for bundles)
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easycat.debug.bundle import RunBundle

logger = logging.getLogger(__name__)


@dataclass
class DebuggerSource:
    """Adapts heterogeneous data sources into one interface for the UI.

    ``records`` returns the latest snapshot of journal records (a fresh
    snapshot each call so live sessions surface new events without the
    server having to subscribe).  ``artifact`` resolves an artifact ref
    to bytes from whatever store the source has access to.
    ``manifest`` returns a small dict the UI shows in the header.
    """

    label: str
    _records_fn: Any = field(repr=False)
    _artifact_fn: Any = field(repr=False)
    _manifest_fn: Any = field(repr=False)

    def records(self) -> list[dict[str, Any]]:
        return list(self._records_fn())

    def artifact(self, ref: str) -> bytes | None:
        return self._artifact_fn(ref)

    def manifest(self) -> dict[str, Any]:
        return dict(self._manifest_fn())


def _bundle_source(bundle_path: str | Path) -> DebuggerSource:
    bundle = RunBundle.load(bundle_path)
    return DebuggerSource(
        label=str(bundle_path),
        _records_fn=lambda: list(bundle.records()),
        _artifact_fn=lambda ref: bundle.artifact_blobs.get(ref),
        _manifest_fn=lambda: {
            "source": "bundle",
            "path": str(bundle_path),
            "format_version": bundle.format_version,
            "provider_versions": bundle.manifest.provider_versions,
            "config_snapshot": bundle.manifest.config_snapshot,
            "sharing_banner": bundle.sharing_banner,
            "record_count": sum(1 for _ in bundle.records()),
            "artifact_count": len(bundle.artifact_blobs),
        },
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

    def _artifact(ref: str) -> bytes | None:
        store = getattr(session, "_artifact_store", None)
        if store is None:
            return None
        return store.get(ref)

    return DebuggerSource(
        label=f"session-{getattr(session, 'session_id', 'unknown')}",
        _records_fn=_records,
        _artifact_fn=_artifact,
        _manifest_fn=lambda: {
            "source": "session",
            "session_id": getattr(session, "session_id", ""),
            "is_running": bool(getattr(session, "is_running", False)),
            "turn_state": str(getattr(session, "turn_state", "")),
        },
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


# ── HTTP API ─────────────────────────────────────────────────────


def _filter_records(
    records: list[dict[str, Any]],
    *,
    stage: str | None,
    turn_id: str | None,
    name: str | None,
    from_seq: int | None,
    to_seq: int | None,
) -> list[dict[str, Any]]:
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
        if name is not None and r.get("name") != name:
            continue
        if stage is not None:
            data = r.get("data") or {}
            if not isinstance(data, dict):
                continue
            if data.get("stage") != stage and data.get("observed_stage") != stage:
                continue
        out.append(r)
    return out


def _summarise_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up per-turn timing for the waterfall view.

    Each turn gets one entry containing first/last sequence, the wall-
    clock span, total bytes of TTS audio captured, count of stage
    records by stage, and any error or interruption observed.  Computed
    from journal records alone — no extra capture required.
    """
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
        if r.get("name") in ("interruption", "control_signal"):
            sig = (r.get("data") or {}).get("signal_kind")
            if r.get("name") == "interruption" or sig == "interrupt":
                bucket["interruption_count"] += 1
        if r.get("error"):
            bucket["error_count"] += 1
    rolled: list[dict[str, Any]] = []
    for turn_id in order:
        bucket = by_turn[turn_id]
        first = bucket["first_wall_ns"]
        last = bucket["last_wall_ns"]
        bucket["wall_ms"] = ((last - first) / 1_000_000) if first and last else None
        rolled.append(bucket)
    return rolled


def _make_app(source: DebuggerSource) -> Any:
    """Build the aiohttp Application with all routes wired up."""
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "easycat[debugger] not installed. Install with "
            "`pip install easycat[debugger]` to use the debugger UI."
        ) from exc

    static_dir = Path(__file__).parent / "static"

    async def index(_request: Any) -> Any:
        return web.FileResponse(static_dir / "index.html")

    async def manifest(_request: Any) -> Any:
        return web.json_response(source.manifest())

    async def records(request: Any) -> Any:
        params = request.query
        from_seq = int(params["from"]) if "from" in params else None
        to_seq = int(params["to"]) if "to" in params else None
        filtered = _filter_records(
            source.records(),
            stage=params.get("stage") or None,
            turn_id=params.get("turn") or None,
            name=params.get("name") or None,
            from_seq=from_seq,
            to_seq=to_seq,
        )
        return web.json_response({"records": filtered, "total": len(filtered)})

    async def turns(_request: Any) -> Any:
        return web.json_response({"turns": _summarise_turns(source.records())})

    async def artifact(request: Any) -> Any:
        ref = request.match_info["ref"]
        blob = source.artifact(ref)
        if blob is None:
            return web.Response(status=404, text=f"artifact {ref} not found")
        # PCM16 audio → raw octet-stream; the frontend wraps it for
        # playback (browsers don't natively play raw PCM, so the UI
        # converts to WAV client-side using AudioContext).
        return web.Response(body=blob, content_type="application/octet-stream")

    async def refresh(_request: Any) -> Any:
        return web.json_response({"snapshot_size": len(source.records())})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/manifest", manifest)
    app.router.add_get("/api/records", records)
    app.router.add_get("/api/turns", turns)
    app.router.add_get("/api/artifact/{ref}", artifact)
    app.router.add_get("/api/refresh", refresh)
    return app


# ── Public entry points ──────────────────────────────────────────


def serve_bundle(
    bundle_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Serve the debugger UI for a bundle on disk.  Blocks the caller."""
    source = _bundle_source(bundle_path)
    _serve(source, host=host, port=port, open_browser=open_browser)


def serve_session(
    session: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    in_thread: bool = False,
) -> threading.Thread | None:
    """Serve the debugger UI for a live :class:`Session`.

    Blocks the caller unless ``in_thread`` is set, in which case the
    server runs on a background daemon thread and the started
    :class:`threading.Thread` is returned so the caller can join later.
    """
    source = _session_source(session)
    if not in_thread:
        _serve(source, host=host, port=port, open_browser=open_browser)
        return None

    thread = threading.Thread(
        target=_serve,
        args=(source,),
        kwargs={"host": host, "port": port, "open_browser": open_browser},
        daemon=True,
        name="easycat-debugger",
    )
    thread.start()
    return thread


def _serve(
    source: DebuggerSource,
    *,
    host: str,
    port: int,
    open_browser: bool,
) -> None:
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "easycat[debugger] not installed. Install with "
            "`pip install easycat[debugger]` to use the debugger UI."
        ) from exc

    app = _make_app(source)
    url = f"http://{host}:{port}/"
    logger.info("EasyCat debugger UI serving on %s (source=%s)", url, source.label)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - depends on env
            logger.debug("Could not open browser automatically", exc_info=True)
    web.run_app(app, host=host, port=port, print=None)


# ── Async-friendly variant for callers already inside an event loop ─


async def run_app_async(
    source: DebuggerSource,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
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

    app = _make_app(source)
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
