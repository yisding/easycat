"""Architecture and edge-case tests for the bundle archive loader."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import easycat.debug._bundle_loader as bundle_loader
import easycat.debug.bundle as bundle_facade
from easycat.debug._bundle_loader import LoadedBundle, _ArtifactAccumulator, _read_zip_member
from easycat.debug._bundle_models import (
    _ARTIFACT_SIZE_CAP,
    FORMAT_VERSION,
    ArtifactEntry,
    BundleValidationError,
    CommittableCheckpoint,
    Manifest,
)


def test_run_bundle_load_is_a_thin_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = "a" * 64
    loaded = LoadedBundle(
        manifest=Manifest(format_version=FORMAT_VERSION, sharing_banner="share safely"),
        journal_ndjson=b'{"sequence": 1}\n',
        artifact_index={ref: ArtifactEntry(ref=ref, size_bytes=4)},
        artifact_blobs={ref: b"data"},
        replay_entry_points=[CommittableCheckpoint(sequence=1, stage="agent")],
    )
    seen_paths: list[object] = []

    def _load(path: object) -> LoadedBundle:
        seen_paths.append(path)
        return loaded

    monkeypatch.setattr(bundle_facade, "load_bundle", _load)

    bundle = bundle_facade.RunBundle.load("ignored.easycat-bundle")

    assert seen_paths == ["ignored.easycat-bundle"]
    assert bundle.manifest is loaded.manifest
    assert bundle.journal_ndjson == loaded.journal_ndjson
    assert bundle.artifact_index is loaded.artifact_index
    assert bundle.artifact_blobs is loaded.artifact_blobs
    assert bundle.replay_entry_points is loaded.replay_entry_points
    assert bundle.sharing_banner == "share safely"


def test_artifact_accumulator_rejects_declared_size_before_allocation() -> None:
    artifacts = _ArtifactAccumulator()

    with pytest.raises(BundleValidationError) as exc_info:
        artifacts.ensure_capacity(_ARTIFACT_SIZE_CAP + 1)

    assert exc_info.value.reason_code == "SIZE_EXCEEDED"
    assert artifacts.total_size == 0


def test_zip_member_limit_rejects_declared_size_before_reading() -> None:
    info = zipfile.ZipInfo("journal.ndjson")
    info.file_size = _ARTIFACT_SIZE_CAP + 1

    class _Archive:
        def getinfo(self, _name: str) -> zipfile.ZipInfo:
            return info

        def read(self, _member: zipfile.ZipInfo) -> bytes:
            raise AssertionError("oversized member must not be read")

    with pytest.raises(BundleValidationError) as exc_info:
        _read_zip_member(
            _Archive(),  # type: ignore[arg-type]
            "journal.ndjson",
            missing_reason_code="MISSING_JOURNAL",
            size_limit=_ARTIFACT_SIZE_CAP,
        )

    assert exc_info.value.reason_code == "SIZE_EXCEEDED"


def test_loader_caps_manifest_and_journal_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bounded-members.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr("journal.ndjson", "")

    observed_limits: dict[str, int | None] = {}
    real_read = bundle_loader._read_zip_member

    def _record_limit(
        archive: zipfile.ZipFile,
        member: str | zipfile.ZipInfo,
        *,
        missing_reason_code: str,
        size_limit: int | None = None,
    ) -> bytes:
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        observed_limits[name] = size_limit
        return real_read(
            archive,
            member,
            missing_reason_code=missing_reason_code,
            size_limit=size_limit,
        )

    monkeypatch.setattr(bundle_loader, "_read_zip_member", _record_limit)

    bundle_facade.RunBundle.load(path)

    assert observed_limits["manifest.json"] == _ARTIFACT_SIZE_CAP
    assert observed_limits["journal.ndjson"] == _ARTIFACT_SIZE_CAP


def test_journal_scalar_records_do_not_break_bundle_loading(tmp_path: Path) -> None:
    path = tmp_path / "scalar-record.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr("journal.ndjson", "1\nnull\n[]\n")

    bundle = bundle_facade.RunBundle.load(path)

    assert list(bundle.records()) == []
