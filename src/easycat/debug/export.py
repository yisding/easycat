"""Export API for debug bundles.

``export_debug_bundle`` is the primary entry point: given a Session
(or session-like object), it writes a portable ``.zip`` bundle
containing the journal, artifacts, and manifest metadata.
"""

from __future__ import annotations

import base64
import json
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from easycat.debug._bundle_loader import _ArtifactAccumulator
from easycat.debug._bundle_models import _INLINE_ARTIFACT_COUNT_CAP
from easycat.debug._serialize import record_to_dict, safe_config_snapshot_from_session
from easycat.debug.bundle import (
    FORMAT_VERSION,
    ArtifactEntry,
    BundleExists,
    BundleValidationError,
    CommittableCheckpoint,
    DebugCaptureDisabledError,
    Manifest,
    RunBundle,
)
from easycat.errors import EASYCAT_E401, _attach_error_code


@runtime_checkable
class _JournalReader(Protocol):
    def read(self, start: int = 0, limit: int | None = None) -> Iterable[object]: ...


@runtime_checkable
class _MemoryArtifactStore(Protocol):
    @property
    def _store(self) -> Mapping[str, bytes | str]: ...


@runtime_checkable
class _FilesystemArtifactStore(Protocol):
    @property
    def _dir(self) -> Path: ...


@dataclass(frozen=True, slots=True)
class _CapturedSessionBundle:
    manifest: Manifest
    journal_ndjson: bytes
    artifacts: dict[str, bytes]


def export_debug_bundle(
    session: object,
    path: str | Path,
    *,
    inline_artifacts: bool = False,
    overwrite: bool = False,
) -> None:
    """Export a debug bundle from a running or cleanly stopped session."""
    path = Path(path)
    journal = _resolve_journal(session)
    _require_debug_capture(session, journal)

    if path.exists() and not overwrite:
        raise BundleExists(f"Bundle already exists: {path}. Use overwrite=True to replace.")

    try:
        captured = _capture_session_bundle(session, journal)
        _write_bundle_archive(
            path,
            captured,
            inline_artifacts=inline_artifacts,
        )
    except Exception as exc:
        _attach_error_code(
            exc,
            EASYCAT_E401(path=str(path), detail=str(exc)),
        )
        raise


def _resolve_journal(session: object) -> _JournalReader | None:
    private_journal = getattr(session, "_journal", None)
    if isinstance(private_journal, _JournalReader):
        return private_journal
    public_journal = getattr(session, "journal", None)
    return public_journal if isinstance(public_journal, _JournalReader) else None


def _require_debug_capture(
    session: object,
    journal: _JournalReader | None,
) -> None:
    debug_mode = getattr(session, "_debug", None) or getattr(session, "debug", None)
    if debug_mode is None:
        debug_mode = "off" if journal is None else "light"
    if isinstance(debug_mode, str) and debug_mode == "off":
        raise DebugCaptureDisabledError("Debug capture is disabled (debug='off')")


def _capture_session_bundle(
    session: object,
    journal: _JournalReader | None,
) -> _CapturedSessionBundle:
    collected_artifacts = _collect_artifacts(session)
    validated_artifacts = _ArtifactAccumulator()
    for ref, data in collected_artifacts.items():
        validated_artifacts.add(ref, data)
    manifest = Manifest(
        format_version=FORMAT_VERSION,
        provider_versions=_collect_provider_versions(session),
        config_snapshot=safe_config_snapshot_from_session(session),
        sharing_banner=_sharing_banner(),
    )
    return _CapturedSessionBundle(
        manifest=manifest,
        journal_ndjson=_serialize_journal(journal),
        artifacts=validated_artifacts.blobs,
    )


def _serialize_journal(journal: _JournalReader | None) -> bytes:
    if journal is None:
        return b""
    lines = [json.dumps(record_to_dict(record), default=str) for record in journal.read()]
    return "\n".join(lines).encode("utf-8")


def _collect_artifacts(session: object) -> dict[str, bytes]:
    artifact_store = getattr(session, "_artifact_store", None)
    if artifact_store is None:
        return {}
    if isinstance(artifact_store, _MemoryArtifactStore):
        return {
            ref: data if isinstance(data, bytes) else data.encode()
            for ref, data in artifact_store._store.items()
        }
    if not isinstance(artifact_store, _FilesystemArtifactStore):
        return {}

    artifact_dir = artifact_store._dir
    if not artifact_dir.is_dir():
        return {}
    return {
        artifact_file.stem: artifact_file.read_bytes()
        for artifact_file in artifact_dir.iterdir()
        if artifact_file.suffix == ".bin" and artifact_file.is_file()
    }


def _sharing_banner() -> str:
    try:
        from easycat.runtime.safe_defaults import DEV_BUNDLE_BANNER

        return DEV_BUNDLE_BANNER
    except ImportError:
        return "This debug bundle is for development use only."


def _write_bundle_archive(
    path: Path,
    captured: _CapturedSessionBundle,
    *,
    inline_artifacts: bool,
) -> None:
    manifest_dict = _manifest_to_dict(captured.manifest)
    if inline_artifacts and captured.artifacts:
        if len(captured.artifacts) > _INLINE_ARTIFACT_COUNT_CAP:
            raise BundleValidationError(
                f"Bundle has more than {_INLINE_ARTIFACT_COUNT_CAP} inline artifacts",
                reason_code="SIZE_EXCEEDED",
            )
        manifest_dict["inline_artifacts"] = {
            ref: base64.b64encode(data).decode("ascii") for ref, data in captured.artifacts.items()
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False)
        tmp_name = tmp.name
        tmp.close()  # Release the fd; ZipFile will open the path itself.
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest_dict, indent=2))
            zf.writestr("journal.ndjson", captured.journal_ndjson)
            if not inline_artifacts:
                for ref, data in captured.artifacts.items():
                    zf.writestr(f"artifacts/{ref}.bin", data)
        Path(tmp_name).replace(path)
    except Exception:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()
        raise


def slice_bundle_by_turn(bundle: RunBundle, turn_id: str) -> RunBundle:
    """Return a new, self-contained :class:`RunBundle` for a single turn.

    Keeps only the records whose ``turn_id`` equals *turn_id*, collects the
    artifact blobs those records reference via ``input_ref`` / ``output_ref``
    (so the slice replays at ARTIFACT fidelity without the rest of the
    session), and filters :attr:`RunBundle.replay_entry_points` down to the
    sliced sequence set.  The manifest is reused verbatim — provider
    versions and config snapshot describe the whole run, which is exactly
    what a regression replay wants to assert against.

    Raises :class:`ValueError` when no record carries the target turn so
    callers can map an empty slice onto an explicit error.
    """
    sliced_records = bundle.filter_by_turn(turn_id)
    if not sliced_records:
        raise ValueError(f"No journal records found for turn {turn_id!r}")

    referenced_refs: set[str] = set()
    sliced_sequences: set[int] = set()
    for record in sliced_records:
        seq = record.get("sequence")
        if isinstance(seq, int):
            sliced_sequences.add(seq)
        for ref_key in ("input_ref", "output_ref"):
            ref = record.get(ref_key)
            if isinstance(ref, str) and ref:
                referenced_refs.add(ref)

    artifact_index: dict[str, ArtifactEntry] = {}
    artifact_blobs: dict[str, bytes] = {}
    for ref in referenced_refs:
        blob = bundle.artifact_blobs.get(ref)
        if blob is None:
            continue
        artifact_blobs[ref] = blob
        artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=len(blob))

    journal_ndjson = "\n".join(
        json.dumps(record, default=str) for record in sliced_records
    ).encode("utf-8")

    entry_points: list[CommittableCheckpoint] = [
        cp for cp in bundle.replay_entry_points if cp.sequence in sliced_sequences
    ]

    return RunBundle(
        format_version=bundle.format_version,
        manifest=bundle.manifest,
        journal_ndjson=journal_ndjson,
        artifact_index=artifact_index,
        artifact_blobs=artifact_blobs,
        replay_entry_points=entry_points,
        sharing_banner=bundle.manifest.sharing_banner or bundle.sharing_banner,
    )


def export_turn_bundle(source: RunBundle, turn_id: str, path: str | Path) -> None:
    """Write a single-turn, self-contained bundle ZIP to *path*.

    Shared by the debugger's ``POST /api/export?turn=`` route and the
    ``easycat journal promote`` CLI: both need a portable slice of one turn
    that replays on its own.  ``source`` is the full :class:`RunBundle`;
    the slice is built by :func:`slice_bundle_by_turn` and written via
    :meth:`RunBundle.save`, so the output round-trips through
    :meth:`RunBundle.load`.
    """
    sliced = slice_bundle_by_turn(source, turn_id)
    sliced.save(path)


def _manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in manifest.__dict__.items():
        if not k.startswith("_"):
            d[k] = v
    return d


def _collect_provider_versions(session: object) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for attr in ("stt", "tts", "transport", "vad", "noise_reducer", "echo_canceller"):
        provider = getattr(session, attr, None)
        if provider is not None and hasattr(provider, "version_info"):
            try:
                versions[attr] = provider.version_info()
            except Exception:
                pass
    return versions
