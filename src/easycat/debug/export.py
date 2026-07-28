"""Export API for debug bundles.

``export_debug_bundle`` is the primary entry point: given a Session
(or session-like object), it writes a portable ``.zip`` bundle
containing the journal, artifacts, and manifest metadata.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from easycat.debug._bundle_models import (
    _ARTIFACT_SIZE_CAP,
    _INLINE_ARTIFACT_COUNT_CAP,
    _SHA256_REF,
)
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


_JOURNAL_PAGE_SIZE = 1_000
_STREAM_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ArtifactSource:
    ref: str
    size_bytes: int
    data: bytes | None = None
    path: Path | None = None


@dataclass(slots=True)
class _ArtifactBudget:
    total_size: int = 0


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
        manifest = _capture_manifest(session)
        _write_bundle_archive(
            path,
            manifest=manifest,
            journal=journal,
            artifacts=_iter_artifacts(session),
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


def _capture_manifest(session: object) -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION,
        provider_versions=_collect_provider_versions(session),
        config_snapshot=safe_config_snapshot_from_session(session),
        sharing_banner=_sharing_banner(),
    )


def _iter_serialized_journal(journal: _JournalReader | None) -> Iterator[bytes]:
    """Yield journal records as NDJSON lines from bounded read pages."""
    if journal is None:
        return

    snapshot_end = _journal_snapshot_end(journal)
    supports_paging = _journal_supports_paging(journal)
    cursor = 0
    while True:
        page = _read_journal_page(journal, cursor, supports_paging=supports_paging)
        if not page:
            return

        last_sequence: int | None = None
        for record in page:
            serialized = record_to_dict(record)
            sequence = _serialized_sequence(serialized)
            if sequence is not None:
                if snapshot_end is not None and sequence > snapshot_end:
                    return
                last_sequence = sequence
            yield json.dumps(serialized, default=str).encode("utf-8")

        if not supports_paging or len(page) != _JOURNAL_PAGE_SIZE:
            return
        if last_sequence is None or last_sequence < cursor:
            return
        cursor = last_sequence + 1
        if snapshot_end is not None and cursor > snapshot_end:
            return


def _journal_snapshot_end(journal: _JournalReader) -> int | None:
    sequence = getattr(journal, "latest_sequence", None)
    if sequence is None:
        inner = getattr(journal, "_journal", None)
        sequence = getattr(inner, "latest_sequence", None)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return sequence


def _journal_supports_paging(journal: _JournalReader) -> bool:
    try:
        parameters = inspect.signature(journal.read).parameters
    except (TypeError, ValueError):
        return True
    return (
        "start" in parameters
        and "limit" in parameters
        or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
    )


def _read_journal_page(
    journal: _JournalReader,
    cursor: int,
    *,
    supports_paging: bool,
) -> list[object]:
    if not supports_paging:
        # Compatibility for small session-like stubs that predate the paged
        # protocol. Production journals all support start+limit.
        return list(journal.read())
    return list(journal.read(start=cursor, limit=_JOURNAL_PAGE_SIZE))


def _serialized_sequence(record: dict[str, Any]) -> int | None:
    sequence = record.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return sequence


def _iter_artifacts(session: object) -> Iterator[_ArtifactSource]:
    """Yield artifact descriptors without reading filesystem blobs."""
    artifact_store = getattr(session, "_artifact_store", None)
    if artifact_store is None:
        return
    if isinstance(artifact_store, _MemoryArtifactStore):
        yield from _iter_memory_artifacts(artifact_store._store)
        return
    if isinstance(artifact_store, _FilesystemArtifactStore):
        yield from _iter_filesystem_artifacts(artifact_store._dir)


def _iter_memory_artifacts(store: Mapping[str, bytes | str]) -> Iterator[_ArtifactSource]:
    for ref, value in store.items():
        data = value if isinstance(value, bytes) else value.encode()
        yield _ArtifactSource(ref=ref, size_bytes=len(data), data=data)


def _iter_filesystem_artifacts(artifact_dir: Path) -> Iterator[_ArtifactSource]:
    if not artifact_dir.is_dir():
        return

    # New stores are sharded one directory deep. Read those first, then
    # legacy flat files that do not have a sharded counterpart.
    for artifact_file in artifact_dir.glob("*/*.bin"):
        if artifact_file.parent.name != artifact_file.stem[:2]:
            continue
        source = _filesystem_artifact_source(artifact_file)
        if source is not None:
            yield source
    for artifact_file in artifact_dir.glob("*.bin"):
        sharded = artifact_dir / artifact_file.stem[:2] / artifact_file.name
        if sharded.is_file():
            continue
        source = _filesystem_artifact_source(artifact_file)
        if source is not None:
            yield source


def _filesystem_artifact_source(path: Path) -> _ArtifactSource | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return _ArtifactSource(ref=path.stem, size_bytes=size, path=path)


def _sharing_banner() -> str:
    try:
        from easycat.runtime.safe_defaults import DEV_BUNDLE_BANNER

        return DEV_BUNDLE_BANNER
    except ImportError:
        return "This debug bundle is for development use only."


def _write_bundle_archive(
    path: Path,
    *,
    manifest: Manifest,
    journal: _JournalReader | None,
    artifacts: Iterable[_ArtifactSource],
    inline_artifacts: bool,
) -> None:
    manifest_dict = _manifest_to_dict(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False)
        tmp_name = tmp.name
        tmp.close()  # Release the fd; ZipFile will open the path itself.
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            artifact_iter = iter(artifacts)
            first_artifact = next(artifact_iter, None)
            all_artifacts = (
                chain((first_artifact,), artifact_iter) if first_artifact is not None else iter(())
            )
            if inline_artifacts and first_artifact is not None:
                _write_inline_manifest(zf, manifest_dict, all_artifacts)
            else:
                zf.writestr("manifest.json", json.dumps(manifest_dict, indent=2))
            _write_journal_member(zf, journal)
            if not inline_artifacts and first_artifact is not None:
                _write_artifact_members(zf, all_artifacts)
        Path(tmp_name).replace(path)
    except Exception:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()
        raise


def _write_journal_member(
    archive: zipfile.ZipFile,
    journal: _JournalReader | None,
) -> None:
    written = 0
    first = True
    with archive.open("journal.ndjson", "w", force_zip64=True) as member:
        for line in _iter_serialized_journal(journal):
            prefix_size = 0 if first else 1
            if written + prefix_size + len(line) > _ARTIFACT_SIZE_CAP:
                raise BundleValidationError(
                    "Journal exceeds 500MB cap",
                    reason_code="SIZE_EXCEEDED",
                )
            if not first:
                member.write(b"\n")
                written += 1
            member.write(line)
            written += len(line)
            first = False


def _write_artifact_members(
    archive: zipfile.ZipFile,
    artifacts: Iterable[_ArtifactSource],
) -> None:
    budget = _ArtifactBudget()
    for source in artifacts:
        _validate_artifact_ref(source.ref)
        with archive.open(
            f"artifacts/{source.ref}.bin",
            "w",
            force_zip64=True,
        ) as member:
            _copy_artifact(source, member.write, budget)


def _write_inline_manifest(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    artifacts: Iterable[_ArtifactSource],
) -> None:
    budget = _ArtifactBudget()
    with archive.open("manifest.json", "w", force_zip64=True) as member:
        member.write(b"{")
        first_field = True
        for key, value in manifest.items():
            if not first_field:
                member.write(b",")
            member.write(json.dumps(str(key)).encode("utf-8"))
            member.write(b":")
            member.write(json.dumps(value).encode("utf-8"))
            first_field = False
        if not first_field:
            member.write(b",")
        member.write(b'"inline_artifacts":{')

        count = 0
        for source in artifacts:
            count += 1
            if count > _INLINE_ARTIFACT_COUNT_CAP:
                raise BundleValidationError(
                    f"Bundle has more than {_INLINE_ARTIFACT_COUNT_CAP} inline artifacts",
                    reason_code="SIZE_EXCEEDED",
                )
            _validate_artifact_ref(source.ref)
            if count > 1:
                member.write(b",")
            member.write(json.dumps(source.ref).encode("ascii"))
            member.write(b':"')
            _copy_artifact_base64(source, member.write, budget)
            member.write(b'"')
        member.write(b"}}")


def _validate_artifact_ref(ref: str) -> None:
    if not _SHA256_REF.fullmatch(ref):
        raise BundleValidationError(
            f"Invalid artifact ref: {ref!r}",
            reason_code="INVALID_REF",
        )


def _copy_artifact(
    source: _ArtifactSource,
    write: Callable[[bytes], Any],
    budget: _ArtifactBudget,
) -> None:
    if source.size_bytes < 0 or budget.total_size + source.size_bytes > _ARTIFACT_SIZE_CAP:
        raise BundleValidationError(
            "Total artifact size exceeds 500MB cap",
            reason_code="SIZE_EXCEEDED",
        )

    hasher = hashlib.sha256()
    copied = 0
    for chunk in _artifact_chunks(source):
        copied += len(chunk)
        if budget.total_size + copied > _ARTIFACT_SIZE_CAP:
            raise BundleValidationError(
                "Total artifact size exceeds 500MB cap",
                reason_code="SIZE_EXCEEDED",
            )
        hasher.update(chunk)
        write(chunk)
    if hasher.hexdigest() != source.ref:
        raise BundleValidationError(
            f"Artifact checksum does not match ref {source.ref!r}",
            reason_code="CHECKSUM_MISMATCH",
        )
    budget.total_size += copied


def _copy_artifact_base64(
    source: _ArtifactSource,
    write: Callable[[bytes], Any],
    budget: _ArtifactBudget,
) -> None:
    carry = b""

    def write_encoded(chunk: bytes) -> None:
        nonlocal carry
        combined = carry + chunk
        encodable_size = len(combined) - (len(combined) % 3)
        if encodable_size:
            write(base64.b64encode(combined[:encodable_size]))
        carry = combined[encodable_size:]

    _copy_artifact(source, write_encoded, budget)
    if carry:
        write(base64.b64encode(carry))


def _artifact_chunks(source: _ArtifactSource) -> Iterator[bytes]:
    if source.data is not None:
        for offset in range(0, len(source.data), _STREAM_CHUNK_SIZE):
            yield source.data[offset : offset + _STREAM_CHUNK_SIZE]
        return
    if source.path is None:
        return
    try:
        with source.path.open("rb") as artifact_file:
            while chunk := artifact_file.read(_STREAM_CHUNK_SIZE):
                yield chunk
    except OSError as exc:
        raise BundleValidationError(
            f"Failed to read artifact {source.ref!r}: {exc}",
            reason_code="MISSING_ARTIFACT",
        ) from exc


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
