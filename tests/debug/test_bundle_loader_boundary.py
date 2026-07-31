"""Architecture and edge-case tests for the bundle archive loader."""

from __future__ import annotations

import base64
import hashlib
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

    assert observed_limits["manifest.json"] == bundle_loader._MANIFEST_SIZE_CAP
    assert observed_limits["journal.ndjson"] == _ARTIFACT_SIZE_CAP


def test_loader_accepts_real_inline_payload_at_manifest_member_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"inline artifact payload" * 64
    ref = hashlib.sha256(data).hexdigest()
    raw_manifest = {
        "format_version": FORMAT_VERSION,
        "inline_artifacts": {ref: base64.b64encode(data).decode("ascii")},
    }
    encoded_manifest = json.dumps(raw_manifest).encode()
    monkeypatch.setattr(bundle_loader, "_MANIFEST_SIZE_CAP", len(encoded_manifest))
    path = tmp_path / "inline-at-limit.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", encoded_manifest)
        archive.writestr("journal.ndjson", "")

    loaded = bundle_loader.load_bundle(path)

    assert loaded.artifact_blobs == {ref: data}


@pytest.mark.parametrize("representation", ["file", "inline"])
def test_loader_rejects_artifact_checksum_mismatch(
    tmp_path: Path,
    representation: str,
) -> None:
    expected_ref = hashlib.sha256(b"expected payload").hexdigest()
    tampered_data = b"tampered payload"
    manifest: dict[str, object] = {"format_version": FORMAT_VERSION}
    if representation == "inline":
        manifest["inline_artifacts"] = {
            expected_ref: base64.b64encode(tampered_data).decode("ascii")
        }

    path = tmp_path / f"tampered-{representation}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("journal.ndjson", "")
        if representation == "file":
            archive.writestr(f"artifacts/{expected_ref}.bin", tampered_data)

    with pytest.raises(BundleValidationError) as exc_info:
        bundle_loader.load_bundle(path)

    assert exc_info.value.reason_code == "CHECKSUM_MISMATCH"


def test_loader_rejects_inline_artifact_count_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_loader, "_INLINE_ARTIFACT_COUNT_CAP", 1)
    inline = {
        hashlib.sha256(value).hexdigest(): base64.b64encode(value).decode("ascii")
        for value in (b"first", b"second")
    }
    path = tmp_path / "too-many-inline.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"format_version": FORMAT_VERSION, "inline_artifacts": inline}),
        )
        archive.writestr("journal.ndjson", "")

    with pytest.raises(BundleValidationError) as exc_info:
        bundle_loader.load_bundle(path)

    assert exc_info.value.reason_code == "SIZE_EXCEEDED"


@pytest.mark.parametrize(
    ("journal_payload", "message"),
    [
        (b"\xff\xfe", "not valid UTF-8 at byte 0"),
        (b'{"sequence": 1}\nnot-json\n', "line 2 is not valid JSON"),
        (b'{"sequence": 1}\n[]\n', "line 2 must be a JSON object"),
    ],
)
def test_loader_rejects_invalid_journal_records(
    tmp_path: Path,
    journal_payload: bytes,
    message: str,
) -> None:
    path = tmp_path / "invalid-journal.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr("journal.ndjson", journal_payload)

    with pytest.raises(BundleValidationError, match=message) as exc_info:
        bundle_facade.RunBundle.load(path)

    assert exc_info.value.reason_code == "INVALID_JOURNAL"


def test_in_memory_bundle_records_never_silently_skip_invalid_journal_data() -> None:
    bundle = bundle_facade.RunBundle(
        journal_ndjson=b'{"sequence": 1}\nnot-json\n{"sequence": 3}\n'
    )

    with pytest.raises(BundleValidationError, match="line 2 is not valid JSON") as exc_info:
        list(bundle.records())

    assert exc_info.value.reason_code == "INVALID_JOURNAL"


def test_loader_normalizes_huge_integer_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "huge-integer-journal.zip"
    journal_payload = b'{"sequence": ' + (b"9" * 5_000) + b"}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr("journal.ndjson", journal_payload)

    with pytest.raises(
        BundleValidationError,
        match="Bundle journal line 1 is not valid JSON",
    ) as exc_info:
        bundle_facade.RunBundle.load(path)

    assert exc_info.value.reason_code == "INVALID_JOURNAL"
    assert isinstance(exc_info.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "journal_payload",
    [
        b'{"name": "missing"}\n',
        b'{"sequence": true}\n',
        b'{"sequence": "1"}\n',
        b'{"sequence": -1}\n',
        b'{"sequence": 2}\n{"sequence": 2}\n',
        b'{"sequence": 2}\n{"sequence": 1}\n',
    ],
)
def test_loader_rejects_invalid_or_non_monotonic_sequences(
    tmp_path: Path,
    journal_payload: bytes,
) -> None:
    path = tmp_path / "invalid-sequence.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr("journal.ndjson", journal_payload)

    with pytest.raises(BundleValidationError) as exc_info:
        bundle_facade.RunBundle.load(path)

    assert exc_info.value.reason_code == "INVALID_JOURNAL"


def test_loader_accepts_recovery_sequence_zero_followed_by_live_records(tmp_path: Path) -> None:
    path = tmp_path / "recovery-sequence.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr(
            "journal.ndjson",
            b'{"sequence": 0, "kind": "recovery"}\n{"sequence": 1, "kind": "event"}\n',
        )

    assert [record["sequence"] for record in bundle_facade.RunBundle.load(path).records()] == [
        0,
        1,
    ]


@pytest.mark.parametrize(
    "journal_payload",
    [
        (
            b'{"sequence": -1, "kind": "degraded", "name": "journal_degraded"}\n'
            b'{"sequence": 0, "kind": "event"}\n'
        ),
        (
            b'{"sequence": 0, "kind": "event"}\n'
            b'{"sequence": -1, "kind": "degraded", "name": "journal_degraded"}\n'
            b'{"sequence": 1, "kind": "event"}\n'
        ),
    ],
)
def test_loader_accepts_degraded_sentinel_outside_live_sequence_order(
    tmp_path: Path,
    journal_payload: bytes,
) -> None:
    path = tmp_path / "degraded-sequence.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        archive.writestr("journal.ndjson", journal_payload)

    records = list(bundle_facade.RunBundle.load(path).records())

    assert [record["sequence"] for record in records] == [
        json.loads(line)["sequence"] for line in journal_payload.splitlines()
    ]
