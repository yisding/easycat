"""Validated archive loading for :class:`easycat.debug.bundle.RunBundle`.

The loader is deliberately independent of the public bundle facade. It turns
untrusted ZIP members into a typed result while keeping path validation,
decompression budgets, manifest parsing, and journal checks in one boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from easycat.debug._bundle_models import (
    _ARTIFACT_SIZE_CAP,
    _INLINE_ARTIFACT_COUNT_CAP,
    _INLINE_ARTIFACT_ENTRY_OVERHEAD,
    _SHA256_REF,
    FORMAT_VERSION,
    ArtifactEntry,
    BundleValidationError,
    BundleVersionError,
    CommittableCheckpoint,
    Manifest,
)

# Inline artifacts expand by 4/3 when base64 encoded into ``manifest.json``.
# Keep the manifest bounded independently from decoded artifact bytes while
# leaving room for the full 500 MB inline budget plus normal JSON metadata and
# per-artifact refs. The decoded artifacts still pass through the stricter
# aggregate ``_ARTIFACT_SIZE_CAP`` below.
_BASE64_ARTIFACT_SIZE_CAP = 4 * ((_ARTIFACT_SIZE_CAP + 2) // 3)
_MANIFEST_METADATA_ALLOWANCE = (
    2_000_000 + _INLINE_ARTIFACT_COUNT_CAP * _INLINE_ARTIFACT_ENTRY_OVERHEAD
)
_MANIFEST_SIZE_CAP = _BASE64_ARTIFACT_SIZE_CAP + _MANIFEST_METADATA_ALLOWANCE


@dataclass(frozen=True, slots=True)
class LoadedBundle:
    manifest: Manifest
    journal_ndjson: bytes
    artifact_index: dict[str, ArtifactEntry]
    artifact_blobs: dict[str, bytes]
    replay_entry_points: list[CommittableCheckpoint]


@dataclass(slots=True)
class _ArtifactAccumulator:
    index: dict[str, ArtifactEntry] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    total_size: int = 0

    def ensure_capacity(self, size: int) -> None:
        if size < 0 or self.total_size + size > _ARTIFACT_SIZE_CAP:
            raise BundleValidationError(
                "Total artifact size exceeds 500MB cap",
                reason_code="SIZE_EXCEEDED",
            )

    def add(self, ref: str, data: bytes) -> None:
        if hashlib.sha256(data).hexdigest() != ref:
            raise BundleValidationError(
                f"Artifact checksum does not match ref {ref!r}",
                reason_code="CHECKSUM_MISMATCH",
            )
        self.ensure_capacity(len(data))
        self.total_size += len(data)
        self.index[ref] = ArtifactEntry(ref=ref, size_bytes=len(data))
        self.blobs[ref] = data


def _reject_traversal(name: str) -> None:
    """Reject absolute and parent-relative archive member names."""
    normalized = name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise BundleValidationError(
            f"Path traversal detected: {name!r}",
            reason_code="PATH_TRAVERSAL",
        )


def _read_zip_member(
    archive: zipfile.ZipFile,
    member: str | zipfile.ZipInfo,
    *,
    missing_reason_code: str,
    size_limit: int | None = None,
) -> bytes:
    try:
        info = archive.getinfo(member) if isinstance(member, str) else member
    except KeyError as exc:
        raise BundleValidationError(
            f"Bundle is missing {member}",
            reason_code=missing_reason_code,
        ) from exc

    name = info.filename
    if size_limit is not None and (info.file_size < 0 or info.file_size > size_limit):
        raise BundleValidationError(
            f"Bundle member {name!r} exceeds {size_limit} byte cap",
            reason_code="SIZE_EXCEEDED",
        )
    try:
        data = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise BundleValidationError(
            f"Invalid bundle member {name!r}: {exc}",
            reason_code="BAD_ZIP",
        ) from exc
    if size_limit is not None and len(data) > size_limit:
        raise BundleValidationError(
            f"Bundle member {name!r} exceeds {size_limit} byte cap",
            reason_code="SIZE_EXCEEDED",
        )
    return data


def _manifest_object(raw: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = raw.get(field_name, {})
    if not isinstance(value, dict):
        raise BundleValidationError(
            f"Bundle manifest {field_name} must be a JSON object",
            reason_code="INVALID_MANIFEST",
        )
    return value


def _read_manifest(archive: zipfile.ZipFile) -> tuple[Manifest, dict[str, Any]]:
    encoded = _read_zip_member(
        archive,
        "manifest.json",
        missing_reason_code="MISSING_MANIFEST",
        size_limit=_MANIFEST_SIZE_CAP,
    )
    try:
        raw = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleValidationError(
            "Bundle manifest is not valid JSON",
            reason_code="INVALID_MANIFEST_JSON",
        ) from exc
    if not isinstance(raw, dict):
        raise BundleValidationError(
            "Bundle manifest must be a JSON object",
            reason_code="INVALID_MANIFEST",
        )

    format_version = raw.get("format_version", 0)
    if (
        not isinstance(format_version, int)
        or isinstance(format_version, bool)
        or format_version < 0
    ):
        raise BundleValidationError(
            "Bundle format_version must be a non-negative integer",
            reason_code="INVALID_MANIFEST",
        )
    if format_version > FORMAT_VERSION:
        raise BundleVersionError(
            f"Bundle format_version {format_version} is newer than "
            f"supported version {FORMAT_VERSION}"
        )

    provider_versions = _manifest_object(raw, "provider_versions")
    config_snapshot = _manifest_object(raw, "config_snapshot")
    env_metadata = _manifest_object(raw, "env_metadata")
    if not all(isinstance(value, str) for value in env_metadata.values()):
        raise BundleValidationError(
            "Bundle manifest env_metadata values must be strings",
            reason_code="INVALID_MANIFEST",
        )
    sharing_banner = raw.get("sharing_banner", "")
    if not isinstance(sharing_banner, str):
        raise BundleValidationError(
            "Bundle manifest sharing_banner must be a string",
            reason_code="INVALID_MANIFEST",
        )

    return (
        Manifest(
            format_version=format_version,
            provider_versions=provider_versions,
            config_snapshot=config_snapshot,
            env_metadata=env_metadata,
            sharing_banner=sharing_banner,
        ),
        raw,
    )


def _validate_member_names(archive: zipfile.ZipFile) -> None:
    for info in archive.infolist():
        _reject_traversal(info.filename)


def _read_file_artifacts(
    archive: zipfile.ZipFile,
    artifacts: _ArtifactAccumulator,
) -> None:
    for info in archive.infolist():
        name = info.filename
        if not name.startswith("artifacts/"):
            continue
        ref = name.removeprefix("artifacts/").removesuffix(".bin")
        if not ref:
            continue
        if not _SHA256_REF.fullmatch(ref):
            raise BundleValidationError(
                f"Invalid artifact ref: {ref!r}",
                reason_code="INVALID_REF",
            )
        artifacts.ensure_capacity(info.file_size)
        data = _read_zip_member(
            archive,
            info,
            missing_reason_code="MISSING_ARTIFACT",
        )
        if len(data) > info.file_size:
            raise BundleValidationError(
                "Total artifact size exceeds 500MB cap",
                reason_code="SIZE_EXCEEDED",
            )
        artifacts.add(ref, data)


def _read_inline_artifacts(
    raw_manifest: dict[str, Any],
    artifacts: _ArtifactAccumulator,
) -> None:
    raw_inline = raw_manifest.get("inline_artifacts", {})
    if not isinstance(raw_inline, dict):
        raise BundleValidationError(
            "Bundle inline_artifacts must be a JSON object",
            reason_code="INVALID_MANIFEST",
        )
    if len(raw_inline) > _INLINE_ARTIFACT_COUNT_CAP:
        raise BundleValidationError(
            f"Bundle has more than {_INLINE_ARTIFACT_COUNT_CAP} inline artifacts",
            reason_code="SIZE_EXCEEDED",
        )

    for ref, encoded in raw_inline.items():
        if ref in artifacts.index:
            continue
        if not _SHA256_REF.fullmatch(ref):
            raise BundleValidationError(
                f"Invalid inline artifact ref: {ref!r}",
                reason_code="INVALID_REF",
            )
        if not isinstance(encoded, str):
            raise BundleValidationError(
                f"Inline artifact {ref!r} must be a base64 string",
                reason_code="INVALID_MANIFEST",
            )
        artifacts.ensure_capacity((len(encoded) * 3) // 4)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BundleValidationError(
                f"Invalid base64 for inline artifact {ref!r}: {exc}",
                reason_code="INVALID_BASE64",
            ) from exc
        artifacts.add(ref, data)


def _validate_journal_metadata(journal_ndjson: bytes) -> None:
    for line in journal_ndjson.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        for key in ("metadata", "framework_metadata"):
            if key in record and len(json.dumps(record[key])) > 1_000_000:
                raise BundleValidationError(
                    f"Record metadata exceeds 1MB: {key}",
                    reason_code="METADATA_TOO_LARGE",
                )


def _parse_replay_entry_points(
    raw_manifest: dict[str, Any],
) -> list[CommittableCheckpoint]:
    raw_entry_points = raw_manifest.get("replay_entry_points", [])
    if not isinstance(raw_entry_points, list):
        raise BundleValidationError(
            "Bundle replay_entry_points must be a list",
            reason_code="INVALID_REPLAY_ENTRY_POINT",
        )

    entry_points: list[CommittableCheckpoint] = []
    for raw_entry_point in raw_entry_points:
        if not isinstance(raw_entry_point, dict):
            raise BundleValidationError(
                "Bundle replay_entry_points entries must be objects",
                reason_code="INVALID_REPLAY_ENTRY_POINT",
            )
        sequence = raw_entry_point.get("sequence", 0)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise BundleValidationError(
                "Bundle replay_entry_points sequence must be a non-negative integer",
                reason_code="INVALID_REPLAY_ENTRY_POINT",
            )
        stage = raw_entry_point.get("stage", "")
        unit_id = raw_entry_point.get("unit_id", "")
        if not isinstance(stage, str) or not isinstance(unit_id, str):
            raise BundleValidationError(
                "Bundle replay_entry_points stage and unit_id must be strings",
                reason_code="INVALID_REPLAY_ENTRY_POINT",
            )
        entry_points.append(CommittableCheckpoint(sequence=sequence, stage=stage, unit_id=unit_id))
    return entry_points


def load_bundle(path: str | Path) -> LoadedBundle:
    """Read and validate a bundle archive without constructing its facade."""
    bundle_path = Path(path)
    if not bundle_path.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_path}")

    try:
        archive = zipfile.ZipFile(bundle_path, "r")
    except zipfile.BadZipFile as exc:
        raise BundleValidationError(
            f"Invalid bundle archive: {bundle_path}",
            reason_code="BAD_ZIP",
        ) from exc

    with archive:
        _validate_member_names(archive)
        manifest, raw_manifest = _read_manifest(archive)
        journal_ndjson = _read_zip_member(
            archive,
            "journal.ndjson",
            missing_reason_code="MISSING_JOURNAL",
            size_limit=_ARTIFACT_SIZE_CAP,
        )
        artifacts = _ArtifactAccumulator()
        _read_file_artifacts(archive, artifacts)
        _read_inline_artifacts(raw_manifest, artifacts)
        _validate_journal_metadata(journal_ndjson)
        replay_entry_points = _parse_replay_entry_points(raw_manifest)

    return LoadedBundle(
        manifest=manifest,
        journal_ndjson=journal_ndjson,
        artifact_index=artifacts.index,
        artifact_blobs=artifacts.blobs,
        replay_entry_points=replay_entry_points,
    )
