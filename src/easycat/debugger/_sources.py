"""Source adaptation for the debugger UI.

Pure, aiohttp-free adapters split out of :mod:`easycat.debugger.server`
(QS3): the :class:`DebuggerSource` façade over a bundle-on-disk, an in-memory
:class:`RunBundle`, or a live ``Session``, plus the artifact-ref / turn-id
validators and the replay-kwargs normaliser the routes lean on.

``server.py`` re-exports every name here so the historical
``from easycat.debugger.server import _helper`` import sites keep resolving.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easycat.debug._audio_health import AUDIO_ANALYSIS_BYTE_CAP
from easycat.debug.bundle import RunBundle
from easycat.debugger._audio import _serialize_frame
from easycat.debugger._records import _record_to_dict

_SHA256_REF = re.compile(r"^[a-f0-9]{64}$")
_TURN_ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

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
    _artifact_analysis_fn: Any | None = field(default=None, repr=False)
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
    # Monotonic "which session is selected" counter for dev-registry sources.
    # The live WS loop reads it each tick and, when it advances, resets its
    # follow-cursor and tells the UI to clear — so switching to a session whose
    # journal sequence is *lower* than the prior one re-snapshots cleanly
    # instead of stalling. ``None`` (every non-dev source) reads as a constant 0.
    _selection_epoch_fn: Any | None = field(default=None, repr=False)
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

    def selection_epoch(self) -> int:
        """Return the dev-registry selection counter (0 for non-dev sources)."""
        if self._selection_epoch_fn is not None:
            return int(self._selection_epoch_fn())
        return 0

    def artifact(self, ref: str) -> bytes | None:
        return self._artifact_fn(ref)

    def artifact_for_analysis(self, ref: str) -> bytes | None:
        if self._artifact_analysis_fn is not None:
            return self._artifact_analysis_fn(ref)
        return self.artifact(ref)

    def manifest(self) -> dict[str, Any]:
        return dict(self._manifest_fn())

    def bundle(self) -> RunBundle | None:
        return self._bundle_fn() if self._bundle_fn is not None else None

    def replay(self, **kwargs: Any) -> Any:
        if self._replay_fn is None:
            raise RuntimeError("This source does not support replay.")
        return self._replay_fn(**kwargs)


def _validated_replay_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Type-check and normalise the optional windowing/filter keys.

    Raises :class:`ValueError` on bad input so the handler maps it to a
    400 with a structured ``BAD_REQUEST`` error_code.  Unknown stage
    names are rejected here rather than silently ignored — a typo in a
    UI checkbox should surface, not produce surprising frame slices.
    """
    from easycat.runtime.replay import _STAGE_NAMES

    out: dict[str, Any] = {}
    if "force" in kwargs:
        value = kwargs["force"]
        if not isinstance(value, bool):
            raise ValueError("force must be a boolean")
        out["force"] = value
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
        tool_policy = ToolReplayPolicy(kwargs.get("tool_policy", "deny"))
        validated = _validated_replay_kwargs(kwargs)
        force = validated.get("force", False)
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

    def _artifact_for_analysis(ref: str) -> bytes | None:
        store = getattr(session, "_artifact_store", None)
        if store is None:
            return None
        bounded = getattr(store, "get_head_tail", None)
        if callable(bounded):
            return bounded(ref, byte_cap=AUDIO_ANALYSIS_BYTE_CAP)
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
        _artifact_analysis_fn=_artifact_for_analysis,
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


__all__ = [
    "DebuggerSource",
    "_bundle_source",
    "_run_bundle_source",
    "_safe_ref",
    "_safe_session_config_snapshot",
    "_safe_turn_id",
    "_session_source",
    "_validated_replay_kwargs",
]
