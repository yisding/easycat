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
from pathlib import Path
from typing import Any

from easycat.debug._serialize import record_to_dict, safe_config_snapshot_from_session
from easycat.debug.bundle import (
    FORMAT_VERSION,
    ArtifactEntry,
    BundleExists,
    CommittableCheckpoint,
    DebugCaptureDisabledError,
    Manifest,
    RunBundle,
)


def export_debug_bundle(
    session: Any,
    path: str | Path,
    *,
    inline_artifacts: bool = False,
    overwrite: bool = False,
) -> None:
    """Export a debug bundle from a running or cleanly stopped session."""
    path = Path(path)

    journal = getattr(session, "_journal", None) or getattr(session, "journal", None)

    # Infer debug mode: check explicit attributes first, then fall back to
    # whether a journal is present (real Session objects created by
    # create_session / create_text_session don't store a _debug attribute,
    # but they do store _journal when debug != "off").
    debug_mode = getattr(session, "_debug", None) or getattr(session, "debug", None)
    if debug_mode is None:
        debug_mode = "off" if journal is None else "light"
    if isinstance(debug_mode, str) and debug_mode == "off":
        raise DebugCaptureDisabledError("Debug capture is disabled (debug='off')")

    if path.exists() and not overwrite:
        raise BundleExists(f"Bundle already exists: {path}. Use overwrite=True to replace.")

    # Build journal NDJSON
    journal_lines: list[str] = []
    if journal is not None:
        records = journal.read() if hasattr(journal, "read") else []
        for record in records:
            journal_lines.append(json.dumps(record_to_dict(record), default=str))
    journal_ndjson = "\n".join(journal_lines).encode("utf-8")

    # Collect artifacts. The refs are already content-addressed
    # SHA-256 hex digests produced by ``ArtifactStore.put`` — we just
    # copy the bytes; the bundle does not carry separate checksums.
    artifact_data: dict[str, bytes] = {}
    artifact_store = getattr(session, "_artifact_store", None)
    if artifact_store is not None:
        if hasattr(artifact_store, "_store"):
            # InMemoryArtifactStore — iterate the in-memory dict.
            for ref, data in artifact_store._store.items():
                raw = data if isinstance(data, bytes) else data.encode()
                artifact_data[ref] = raw
        elif hasattr(artifact_store, "_dir"):
            # FilesystemArtifactStore — read .bin files from disk.
            artifact_dir = artifact_store._dir
            if artifact_dir.is_dir():
                for f in artifact_dir.iterdir():
                    if f.suffix == ".bin" and f.is_file():
                        ref = f.stem
                        artifact_data[ref] = f.read_bytes()

    # Provider versions
    provider_versions = _collect_provider_versions(session)

    # Safe config snapshot (use safe_defaults allowlist)
    config_snapshot = safe_config_snapshot_from_session(session)

    # Sharing banner
    try:
        from easycat.runtime.safe_defaults import DEV_BUNDLE_BANNER

        banner = DEV_BUNDLE_BANNER
    except ImportError:
        banner = "This debug bundle is for development use only."

    manifest = Manifest(
        format_version=FORMAT_VERSION,
        provider_versions=provider_versions,
        config_snapshot=config_snapshot,
        sharing_banner=banner,
    )

    manifest_dict = _manifest_to_dict(manifest)
    if inline_artifacts and artifact_data:
        manifest_dict["inline_artifacts"] = {
            ref: base64.b64encode(data).decode("ascii") for ref, data in artifact_data.items()
        }

    # Write zip atomically
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False)
        tmp_name = tmp.name
        tmp.close()  # Release the fd; ZipFile will open the path itself.
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest_dict, indent=2))
            zf.writestr("journal.ndjson", journal_ndjson)
            if not inline_artifacts:
                for ref, data in artifact_data.items():
                    zf.writestr(f"artifacts/{ref}.bin", data)
        Path(tmp_name).rename(path)
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


def _collect_provider_versions(session: Any) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for attr in ("stt", "tts", "transport", "vad", "noise_reducer", "echo_canceller"):
        provider = getattr(session, attr, None)
        if provider is not None and hasattr(provider, "version_info"):
            try:
                versions[attr] = provider.version_info()
            except Exception:
                pass
    return versions
