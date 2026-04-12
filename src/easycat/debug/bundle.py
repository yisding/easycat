"""RunBundle: portable debug bundle for replay and sharing.

A bundle packages the execution journal, artifact blobs, and manifest
metadata into a single ZIP archive that can be loaded for replay or
shared with teammates.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1


class BundleError(RuntimeError): ...


class BundleExists(BundleError): ...


class BundleVersionError(BundleError): ...


class BundleValidationError(BundleError):
    def __init__(self, message: str, *, reason_code: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class BundleInUseError(BundleError): ...


class BundleRecoveryError(BundleError): ...


class DebugCaptureDisabledError(BundleError): ...


class DebugCaptureUnavailableError(BundleError): ...


# Artifact refs are the content-addressed SHA-256 hex digests produced
# by ``ArtifactStore.put``. This regex validates that incoming refs
# match that format — purely a structural sanity check on the ref
# string, not a tamper-proofing mechanism for the bundle contents.
_SHA256_REF = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class ArtifactEntry:
    ref: str
    size_bytes: int = 0


@dataclass(frozen=True)
class CommittableCheckpoint:
    sequence: int
    stage: str
    unit_id: str = ""


@dataclass(frozen=True)
class Manifest:
    format_version: int = FORMAT_VERSION
    provider_versions: dict[str, Any] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    env_metadata: dict[str, str] = field(default_factory=dict)
    sharing_banner: str = ""


@dataclass
class RunBundle:
    format_version: int = FORMAT_VERSION
    manifest: Manifest = field(default_factory=Manifest)
    journal_ndjson: bytes = b""
    artifact_index: dict[str, ArtifactEntry] = field(default_factory=dict)
    replay_entry_points: list[CommittableCheckpoint] = field(default_factory=list)
    sharing_banner: str = ""

    def records(self):
        """Iterate journal records."""
        for line in self.journal_ndjson.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def filter_by_stage(self, stage_name: str) -> list[dict[str, Any]]:
        """Filter journal records by stage name."""
        return [r for r in self.records() if r.get("data", {}).get("stage") == stage_name]

    def filter_by_turn(self, turn_id: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r.get("turn_id") == turn_id]

    def lookup_by_sequence(self, seq: int) -> dict[str, Any] | None:
        for r in self.records():
            if r.get("sequence") == seq:
                return r
        return None

    @staticmethod
    def load(path: str | Path) -> RunBundle:
        """Load a bundle from disk.

        Bundles are not tamper-evident: we trust the contents of the
        ZIP we're handed. Use filesystem ACLs or a signing layer on
        top if you need integrity guarantees.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Bundle not found: {path}")

        with zipfile.ZipFile(path, "r") as zf:
            # Read manifest
            manifest_data = json.loads(zf.read("manifest.json"))
            fmt_ver = manifest_data.get("format_version", 0)
            if fmt_ver > FORMAT_VERSION:
                raise BundleVersionError(
                    f"Bundle format_version {fmt_ver} is newer than "
                    f"supported version {FORMAT_VERSION}"
                )

            # Validate manifest entries for path traversal
            for name in zf.namelist():
                if ".." in name or name.startswith("/"):
                    raise BundleValidationError(
                        f"Path traversal detected: {name!r}",
                        reason_code="PATH_TRAVERSAL",
                    )

            manifest = Manifest(
                format_version=fmt_ver,
                provider_versions=manifest_data.get("provider_versions", {}),
                config_snapshot=manifest_data.get("config_snapshot", {}),
                env_metadata=manifest_data.get("env_metadata", {}),
                sharing_banner=manifest_data.get("sharing_banner", ""),
            )

            # Read journal
            journal_ndjson = zf.read("journal.ndjson")

            # Read artifacts
            artifact_index: dict[str, ArtifactEntry] = {}
            total_size = 0
            for name in zf.namelist():
                if name.startswith("artifacts/"):
                    ref = name.removeprefix("artifacts/").removesuffix(".bin")
                    if ref:
                        if not _SHA256_REF.match(ref):
                            raise BundleValidationError(
                                f"Invalid artifact ref: {ref!r}",
                                reason_code="INVALID_REF",
                            )
                        data = zf.read(name)
                        total_size += len(data)
                        if total_size > 500_000_000:  # 500MB
                            raise BundleValidationError(
                                "Total artifact size exceeds 500MB cap",
                                reason_code="SIZE_EXCEEDED",
                            )
                        artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=len(data))

            # Reconstruct artifacts from inline base64 blobs in manifest
            for ref, b64 in manifest_data.get("inline_artifacts", {}).items():
                if ref in artifact_index:
                    continue  # file-based entry takes precedence
                if not _SHA256_REF.match(ref):
                    raise BundleValidationError(
                        f"Invalid inline artifact ref: {ref!r}",
                        reason_code="INVALID_REF",
                    )
                data = base64.b64decode(b64)
                total_size += len(data)
                if total_size > 500_000_000:
                    raise BundleValidationError(
                        "Total artifact size exceeds 500MB cap",
                        reason_code="SIZE_EXCEEDED",
                    )
                artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=len(data))

            # Validate metadata sizes
            for record_line in journal_ndjson.decode("utf-8", errors="replace").splitlines():
                if not record_line.strip():
                    continue
                try:
                    record = json.loads(record_line)
                    for key in ("metadata", "framework_metadata"):
                        if key in record:
                            meta_json = json.dumps(record[key])
                            if len(meta_json) > 1_000_000:
                                raise BundleValidationError(
                                    f"Record metadata exceeds 1MB: {key}",
                                    reason_code="METADATA_TOO_LARGE",
                                )
                except json.JSONDecodeError:
                    continue

            entry_points = []
            for ep in manifest_data.get("replay_entry_points", []):
                entry_points.append(
                    CommittableCheckpoint(
                        sequence=ep.get("sequence", 0),
                        stage=ep.get("stage", ""),
                        unit_id=ep.get("unit_id", ""),
                    )
                )

            return RunBundle(
                format_version=fmt_ver,
                manifest=manifest,
                journal_ndjson=journal_ndjson,
                artifact_index=artifact_index,
                replay_entry_points=entry_points,
                sharing_banner=manifest.sharing_banner,
            )

    @staticmethod
    def from_partial_journal(
        journal_path: str | Path,
        artifact_root: str | Path | None = None,
    ) -> RunBundle:
        """Load from a crashed session's SQLite journal and artifact directory."""
        journal_path = Path(journal_path)
        if not journal_path.exists():
            raise FileNotFoundError(f"Journal not found: {journal_path}")

        # Build journal NDJSON from SQLite
        try:
            conn = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                raise BundleInUseError(
                    f"Journal {journal_path} is currently in use. "
                    "Use `bundles list` for running sessions."
                ) from e
            raise BundleRecoveryError(f"Cannot open journal: {e}") from e

        try:
            journal_ndjson = _read_journal_ndjson(conn)
        except sqlite3.OperationalError as e:
            raise BundleRecoveryError(f"Cannot read journal records: {e}") from e
        finally:
            conn.close()

        # Walk artifact directory
        artifact_index: dict[str, ArtifactEntry] = {}
        if artifact_root and Path(artifact_root).exists():
            for f in Path(artifact_root).iterdir():
                if f.is_file():
                    ref = f.stem
                    if _SHA256_REF.match(ref):
                        artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=f.stat().st_size)

        manifest = Manifest(format_version=FORMAT_VERSION)

        return RunBundle(
            format_version=FORMAT_VERSION,
            manifest=manifest,
            journal_ndjson=journal_ndjson,
            artifact_index=artifact_index,
        )


def _read_journal_ndjson(conn: sqlite3.Connection) -> bytes:
    """Read journal records from a SQLite database and return NDJSON bytes.

    Tries the current ``journal`` table schema first, then falls back to
    the legacy ``records(sequence, data)`` table for backwards compat.
    """
    # Check which tables exist.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    lines: list[str] = []

    if "journal" in tables:
        cursor = conn.execute(
            "SELECT sequence, session_id, kind, name, wall_ns, mono_ns, "
            "turn_id, data, error_type, error_msg, input_ref, output_ref, tags "
            "FROM journal ORDER BY sequence"
        )
        for row in cursor:
            record: dict[str, Any] = {
                "sequence": row[0],
                "session_id": row[1],
                "kind": row[2],
                "name": row[3],
                "wall_ns": row[4],
                "mono_ns": row[5],
                "turn_id": row[6],
            }
            if row[7] and row[7] != "{}":
                try:
                    record["data"] = json.loads(row[7])
                except json.JSONDecodeError:
                    record["data"] = row[7]
            if row[8]:
                record["error"] = {"type": row[8], "message": row[9] or ""}
            if row[10]:
                record["input_ref"] = row[10]
            if row[11]:
                record["output_ref"] = row[11]
            if row[12] and row[12] != "":
                record["tags"] = row[12].split(",")
            lines.append(json.dumps(record, default=str))
    elif "records" in tables:
        # Legacy schema: single JSON blob per row.
        cursor = conn.execute("SELECT data FROM records ORDER BY sequence")
        for (data,) in cursor:
            lines.append(data if isinstance(data, str) else json.dumps(data))
    else:
        raise sqlite3.OperationalError("no recognized journal table found")

    return "\n".join(lines).encode("utf-8")


def discover_bundles(data_dir: str | None = None) -> list[Path]:
    """Discover bundles in standard directories."""
    if data_dir is None:
        data_dir = os.environ.get("EASYCAT_DATA_DIR", ".easycat")
    data_path = Path(data_dir)
    bundles: list[Path] = []
    for subdir in ("recordings", "crash-dumps"):
        search = data_path / subdir
        if search.exists():
            for f in search.iterdir():
                if f.suffix in (".zip", ".easycat-bundle", ".sqlite") or f.name.endswith(
                    ".easycat-bundle"
                ):
                    bundles.append(f)
    return sorted(bundles)
