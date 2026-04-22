"""RunBundle: portable debug bundle for replay and sharing.

A bundle packages the execution journal, artifact blobs, and manifest
metadata into a single ZIP archive that can be loaded for replay or
shared with teammates.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from easycat.runtime.replay import (
        ReplayAudioChunk,
        ReplayCassette,
        ReplayResult,
        ReplaySpec,
    )

_ARTIFACT_SIZE_CAP = 500_000_000  # 500MB aggregate cap across artifacts.


def _reject_traversal(name: str) -> None:
    """Raise if *name* looks like a traversal or absolute path.

    ZIP entry names are POSIX-style, but attackers can embed backslashes
    or absolute paths that naïve string checks miss.  Normalise
    backslashes, parse as a posix path, and reject absolute paths or any
    ``..`` component.
    """
    normalized = name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise BundleValidationError(
            f"Path traversal detected: {name!r}",
            reason_code="PATH_TRAVERSAL",
        )


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


_CHECKPOINT_ID_PREFIX = "cp_"


def checkpoint_id(sequence: int) -> str:
    """Convert a monotonic journal sequence to its user-facing checkpoint id.

    The ``cp_<sequence>`` vocabulary (``cp_87``) is what the debugger
    UI, replay commands, and LLM-coding-agent prompts use externally;
    the journal itself keeps the raw integer for ordering and
    indexing.  Keeping both forms isolated behind this helper means a
    future format change (e.g. short hashes) can happen in one place.
    """
    if sequence < 0:
        raise ValueError(f"checkpoint sequence must be non-negative, got {sequence}")
    return f"{_CHECKPOINT_ID_PREFIX}{sequence}"


def parse_checkpoint_id(value: str) -> int:
    """Inverse of :func:`checkpoint_id`.  Raises ``ValueError`` on a bad id."""
    if not isinstance(value, str) or not value.startswith(_CHECKPOINT_ID_PREFIX):
        raise ValueError(f"Invalid checkpoint id {value!r}: expected 'cp_<int>'")
    raw = value[len(_CHECKPOINT_ID_PREFIX) :]
    try:
        seq = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid checkpoint id {value!r}: not an integer") from exc
    if seq < 0:
        raise ValueError(f"Invalid checkpoint id {value!r}: negative sequence")
    return seq


@dataclass(frozen=True)
class CommittableCheckpoint:
    sequence: int
    stage: str
    unit_id: str = ""

    @property
    def checkpoint_id(self) -> str:
        """Return the ``cp_<sequence>`` user-facing id for this checkpoint."""
        return checkpoint_id(self.sequence)


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
    artifact_blobs: dict[str, bytes] = field(default_factory=dict)
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
        results: list[dict[str, Any]] = []
        for r in self.records():
            data = r.get("data") or {}
            if isinstance(data, dict):
                if data.get("stage") == stage_name or data.get("observed_stage") == stage_name:
                    results.append(r)
        return results

    def filter_by_turn(self, turn_id: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r.get("turn_id") == turn_id]

    def lookup_by_sequence(self, seq: int) -> dict[str, Any] | None:
        for r in self.records():
            if r.get("sequence") == seq:
                return r
        return None

    def lookup_by_checkpoint_id(self, cid: str) -> dict[str, Any] | None:
        """Resolve a ``cp_<sequence>`` id to its journal record.

        Thin sugar over :meth:`lookup_by_sequence` that accepts the
        user-facing vocabulary without forcing callers to parse the
        prefix themselves.
        """
        return self.lookup_by_sequence(parse_checkpoint_id(cid))

    # ── Replay surface ────────────────────────────────────────

    def cassette_for_stage(
        self,
        stage_name: str,
        *,
        turn_id: str | None = None,
    ) -> ReplayCassette:
        """Return a :class:`ReplayCassette` slicing this bundle for one stage.

        The cassette holds every journal record for the named stage
        (optionally restricted to one turn) and a resolver closure that
        looks refs up in :attr:`artifact_blobs`.  Stages consume this
        via :meth:`easycat.stages.base.Stage.replay`.
        """
        from easycat.runtime.replay import ReplayCassette

        records = self.filter_by_stage(stage_name)
        if turn_id is not None:
            records = [r for r in records if r.get("turn_id") == turn_id]
        blobs = self.artifact_blobs

        def _resolver(ref: str) -> bytes | None:
            return blobs.get(ref)

        return ReplayCassette(
            stage_name=stage_name,
            records=tuple(records),
            _resolver=_resolver,
        )

    def replay(
        self,
        spec: ReplaySpec,
        *,
        installed_versions: dict[str, str] | None = None,
    ) -> ReplayResult:
        """Orchestrate a replay of this bundle under *spec*.

        Thin wrapper around :class:`easycat.runtime.replay.ReplayRunner`.
        Pass ``installed_versions`` (``{"stt": "openai-1.2.3", ...}``) to
        enable the provider-version match check from T4.2; omit it for
        offline replay where version skew is acceptable.
        """
        from easycat.runtime.replay import ReplayRunner

        runner = ReplayRunner(self, spec, installed_versions=installed_versions)
        return runner.run()

    def replay_audio(
        self,
        *,
        turn_id: str | None = None,
    ) -> list[ReplayAudioChunk]:
        """Return the TTS audio chunks the user heard during recording.

        Byte-identical reconstruction of the outbound audio stream, no
        live providers involved.  See
        :func:`easycat.runtime.replay.replay_audio` for the guarantees.
        """
        from easycat.runtime.replay import replay_audio as _replay_audio

        return _replay_audio(self, turn_id=turn_id)

    def replay_stt_audio(
        self,
        *,
        turn_id: str | None = None,
        include_preroll: bool = True,
    ) -> list[ReplayAudioChunk]:
        """Return the audio chunks the session handed to STT during recording.

        Useful for LIVE-fidelity replay: feed these to a fresh STT
        provider to re-transcribe offline.  See
        :func:`easycat.runtime.replay.replay_stt_audio` for filter options.
        """
        from easycat.runtime.replay import replay_stt_audio as _replay_stt

        return _replay_stt(self, turn_id=turn_id, include_preroll=include_preroll)

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
                _reject_traversal(name)

            manifest = Manifest(
                format_version=fmt_ver,
                provider_versions=manifest_data.get("provider_versions", {}),
                config_snapshot=manifest_data.get("config_snapshot", {}),
                env_metadata=manifest_data.get("env_metadata", {}),
                sharing_banner=manifest_data.get("sharing_banner", ""),
            )

            # Read journal
            journal_ndjson = zf.read("journal.ndjson")

            # Read artifacts.  Check each entry's declared uncompressed
            # size before reading so a zip bomb can't force a massive
            # in-memory decompression.
            artifact_index: dict[str, ArtifactEntry] = {}
            artifact_blobs: dict[str, bytes] = {}
            total_size = 0
            for info in zf.infolist():
                name = info.filename
                if not name.startswith("artifacts/"):
                    continue
                ref = name.removeprefix("artifacts/").removesuffix(".bin")
                if not ref:
                    continue
                if not _SHA256_REF.match(ref):
                    raise BundleValidationError(
                        f"Invalid artifact ref: {ref!r}",
                        reason_code="INVALID_REF",
                    )
                declared = info.file_size
                if declared < 0 or total_size + declared > _ARTIFACT_SIZE_CAP:
                    raise BundleValidationError(
                        "Total artifact size exceeds 500MB cap",
                        reason_code="SIZE_EXCEEDED",
                    )
                data = zf.read(name)
                if len(data) > declared or total_size + len(data) > _ARTIFACT_SIZE_CAP:
                    raise BundleValidationError(
                        "Total artifact size exceeds 500MB cap",
                        reason_code="SIZE_EXCEEDED",
                    )
                total_size += len(data)
                artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=len(data))
                artifact_blobs[ref] = data

            # Reconstruct artifacts from inline base64 blobs in manifest
            for ref, b64 in manifest_data.get("inline_artifacts", {}).items():
                if ref in artifact_index:
                    continue  # file-based entry takes precedence
                if not _SHA256_REF.match(ref):
                    raise BundleValidationError(
                        f"Invalid inline artifact ref: {ref!r}",
                        reason_code="INVALID_REF",
                    )
                estimated_size = (len(b64) * 3) // 4
                if total_size + estimated_size > _ARTIFACT_SIZE_CAP:
                    raise BundleValidationError(
                        "Total artifact size exceeds 500MB cap",
                        reason_code="SIZE_EXCEEDED",
                    )
                try:
                    data = base64.b64decode(b64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise BundleValidationError(
                        f"Invalid base64 for inline artifact {ref!r}: {exc}",
                        reason_code="INVALID_BASE64",
                    ) from exc
                total_size += len(data)
                if total_size > _ARTIFACT_SIZE_CAP:
                    raise BundleValidationError(
                        "Total artifact size exceeds 500MB cap",
                        reason_code="SIZE_EXCEEDED",
                    )
                artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=len(data))
                artifact_blobs[ref] = data

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
                artifact_blobs=artifact_blobs,
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

        # Walk artifact directory.  Read blobs so downstream replay has
        # the bytes available; respect the same 500MB cap as ``load`` to
        # avoid OOM on a corrupted artifact tree.
        artifact_index: dict[str, ArtifactEntry] = {}
        artifact_blobs: dict[str, bytes] = {}
        if artifact_root and Path(artifact_root).exists():
            total_size = 0
            for f in Path(artifact_root).iterdir():
                if not f.is_file():
                    continue
                ref = f.stem
                if not _SHA256_REF.match(ref):
                    continue
                size = f.stat().st_size
                if total_size + size > _ARTIFACT_SIZE_CAP:
                    raise BundleValidationError(
                        "Total artifact size exceeds 500MB cap",
                        reason_code="SIZE_EXCEEDED",
                    )
                total_size += size
                artifact_index[ref] = ArtifactEntry(ref=ref, size_bytes=size)
                artifact_blobs[ref] = f.read_bytes()

        manifest = Manifest(format_version=FORMAT_VERSION)

        return RunBundle(
            format_version=FORMAT_VERSION,
            manifest=manifest,
            journal_ndjson=journal_ndjson,
            artifact_index=artifact_index,
            artifact_blobs=artifact_blobs,
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
