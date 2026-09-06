"""RunBundle: portable debug bundle for replay and sharing.

An exported bundle packages the execution journal, artifact blobs, and
manifest metadata into a single ZIP archive (``.zip``, ``.bundle``, or
``.easycat-bundle``) that :meth:`RunBundle.load` opens for replay or
sharing with teammates.

A :class:`RunBundle` can also be reconstructed from a raw SQLite journal
(``.sqlite``) plus its artifact directory via
:meth:`RunBundle.from_partial_journal`.  Exported bundles and crash-dump
journals are surfaced by :func:`discover_bundles`, so callers must
dispatch on the suffix.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any

from easycat.debug._bundle_loader import (
    _ArtifactAccumulator,
    _iter_journal_records,
    _reject_traversal,
    _validate_journal_metadata,
    load_bundle,
)
from easycat.debug._bundle_models import (
    _SHA256_REF,
    FORMAT_VERSION,
    ArtifactEntry,
    BundleError,
    BundleExists,
    BundleInUseError,
    BundleRecoveryError,
    BundleValidationError,
    BundleVersionError,
    CommittableCheckpoint,
    DebugCaptureDisabledError,
    Manifest,
    checkpoint_id,
    parse_checkpoint_id,
)
from easycat.runtime._private_files import sqlite_readonly_uri

__all__ = [
    "FORMAT_VERSION",
    "ArtifactEntry",
    "BundleError",
    "BundleExists",
    "BundleInUseError",
    "BundleRecoveryError",
    "BundleValidationError",
    "BundleVersionError",
    "CommittableCheckpoint",
    "DebugCaptureDisabledError",
    "Manifest",
    "RunBundle",
    "checkpoint_id",
    "discover_bundles",
    "discover_bundles_with_status",
    "parse_checkpoint_id",
]

if TYPE_CHECKING:
    from easycat.runtime.replay import (
        ReplayAudioChunk,
        ReplayCassette,
        ReplayResult,
        ReplaySpec,
    )


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
        yield from _iter_journal_records(self.journal_ndjson)

    def filter_by_stage(self, stage_name: str) -> list[dict[str, Any]]:
        """Filter journal records by stage name."""
        results: list[dict[str, Any]] = []
        for r in self.records():
            data = r.get("data") or {}
            if isinstance(data, dict):  # noqa: SIM102 nested branches preserve decision context
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
        stage_replayers: dict[str, Callable[[ReplaySpec, ReplayCassette], Any]] | None = None,
        tool_executor: Callable[[dict[str, Any]], Any] | None = None,
    ) -> ReplayResult:
        """Orchestrate a replay of this bundle under *spec*.

        Thin wrapper around :class:`easycat.runtime.replay.ReplayRunner`.
        Pass ``installed_versions`` (``{"stt": "openai-1.2.3", ...}``) to
        enable provider-version match checks; omit it for
        offline replay where version skew is acceptable. Applications
        that own live provider or tool clients may supply synchronous
        ``stage_replayers`` and ``tool_executor`` callbacks; the CLI
        intentionally uses only the provider-free built-in replay path.
        """
        from easycat.runtime.replay import ReplayRunner

        runner = ReplayRunner(
            self,
            spec,
            installed_versions=installed_versions,
            stage_replayers=stage_replayers,
            tool_executor=tool_executor,
        )
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

    def save(self, path: str | Path) -> None:
        """Write this bundle to ``path`` as a portable ``.zip`` archive.

        Mirrors :func:`easycat.debug.export.export_debug_bundle`'s member
        layout exactly (``manifest.json``, ``journal.ndjson``,
        ``artifacts/<sha256>.bin``) so a saved bundle round-trips through
        :meth:`load` with equal records and artifacts.  Unlike the session
        exporter, ``save`` is the in-memory writer: it serialises whatever
        is already on this :class:`RunBundle`, including
        :attr:`replay_entry_points` (which the session exporter omits).

        Every artifact ref, digest, and aggregate size is validated before a
        single byte is written, and every archive member name is checked by
        :func:`_reject_traversal`.  A tampered in-memory bundle therefore
        cannot emit a traversal path or a non-content-addressed artifact.
        The write is atomic: a sibling temp file is renamed into place only
        on success.
        """
        path = Path(path)

        manifest_dict: dict[str, Any] = {
            "format_version": self.format_version,
            "provider_versions": dict(self.manifest.provider_versions),
            "config_snapshot": dict(self.manifest.config_snapshot),
            "env_metadata": dict(self.manifest.env_metadata),
            "journal_dropped_records": self.manifest.journal_dropped_records,
            "sharing_banner": self.manifest.sharing_banner or self.sharing_banner,
            "replay_entry_points": [
                {"sequence": cp.sequence, "stage": cp.stage, "unit_id": cp.unit_id}
                for cp in self.replay_entry_points
            ],
        }

        # Validate artifact refs and member names before opening the archive
        # so a malformed in-memory bundle fails fast and writes nothing.
        validated_artifacts = _ArtifactAccumulator()
        for ref, data in self.artifact_blobs.items():
            validated_artifacts.add(ref, data)
            _reject_traversal(f"artifacts/{ref}.bin")
        _validate_journal_metadata(
            self.journal_ndjson,
            artifact_refs=set(validated_artifacts.index),
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as tmp:
                tmp_name = tmp.name
            with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest_dict, indent=2))
                zf.writestr("journal.ndjson", self.journal_ndjson)
                for ref, data in validated_artifacts.blobs.items():
                    zf.writestr(f"artifacts/{ref}.bin", data)
            Path(tmp_name).rename(path)
        except Exception:
            if tmp_name and Path(tmp_name).exists():
                Path(tmp_name).unlink()
            raise

    @staticmethod
    def load(path: str | Path) -> RunBundle:
        """Load a bundle from disk.

        Bundles are not tamper-evident: we trust the contents of the
        ZIP we're handed. Use filesystem ACLs or a signing layer on
        top if you need integrity guarantees.
        """
        loaded = load_bundle(path)
        return RunBundle(
            format_version=loaded.manifest.format_version,
            manifest=loaded.manifest,
            journal_ndjson=loaded.journal_ndjson,
            artifact_index=loaded.artifact_index,
            artifact_blobs=loaded.artifact_blobs,
            replay_entry_points=loaded.replay_entry_points,
            sharing_banner=loaded.manifest.sharing_banner,
        )

    @staticmethod
    def from_partial_journal(
        journal_path: str | Path,
        artifact_root: str | Path | None = None,
    ) -> RunBundle:
        """Load from a SQLite journal and optional artifact directory."""
        journal_path = Path(journal_path)
        if not journal_path.exists():
            raise FileNotFoundError(f"Journal not found: {journal_path}")

        # Build journal NDJSON from SQLite
        try:
            conn = sqlite3.connect(sqlite_readonly_uri(journal_path), uri=True)
        except sqlite3.DatabaseError as e:
            if "database is locked" in str(e):
                raise BundleInUseError(
                    f"Journal {journal_path} is currently in use. "
                    "Stop the session before inspecting it, or export a ZIP bundle with "
                    "`session.export_debug_bundle(...)`."
                ) from e
            raise BundleRecoveryError(f"Cannot open journal: {e}") from e

        try:
            journal_ndjson = _read_journal_ndjson(conn)
        except sqlite3.DatabaseError as e:
            raise BundleRecoveryError(f"Cannot read journal records: {e}") from e
        finally:
            conn.close()
        # Walk artifact directory.  Read blobs so downstream replay has
        # the bytes available; respect the same 500MB cap as ``load`` to
        # avoid OOM on a corrupted artifact tree.
        artifacts = _ArtifactAccumulator()
        if artifact_root and Path(artifact_root).exists():
            root = Path(artifact_root)
            for f in chain(root.glob("*/*.bin"), root.glob("*.bin")):
                if f.is_symlink() or not f.is_file():
                    continue
                ref = f.stem
                if f.parent != root and f.parent.name != ref[:2]:
                    continue
                if not _SHA256_REF.fullmatch(ref) or ref in artifacts.index:
                    continue
                size = f.stat().st_size
                artifacts.ensure_capacity(size)
                artifacts.add(ref, f.read_bytes())
        _validate_journal_metadata(
            journal_ndjson,
            artifact_refs=set(artifacts.index) if artifact_root is not None else None,
        )

        manifest = Manifest(format_version=FORMAT_VERSION)

        return RunBundle(
            format_version=FORMAT_VERSION,
            manifest=manifest,
            journal_ndjson=journal_ndjson,
            artifact_index=artifacts.index,
            artifact_blobs=artifacts.blobs,
        )


def _partial_journal_error(
    sequence: Any,
    error_type: Any,
    error_message: Any,
    error_traceback: Any,
    error_notes: Any,
    error_children: Any,
) -> dict[str, Any] | None:
    children: list[Any] = []
    if error_children:
        try:
            decoded_children = json.loads(error_children)
        except Exception as exc:
            raise BundleValidationError(
                f"Journal sequence {sequence!r} error_children is not valid JSON",
                reason_code="INVALID_JOURNAL",
            ) from exc
        if not isinstance(decoded_children, list):
            raise BundleValidationError(
                f"Journal sequence {sequence!r} error_children must be a JSON list",
                reason_code="INVALID_JOURNAL",
            )
        children = decoded_children
    if not error_type:
        return None
    return {
        "type": error_type,
        "message": error_message or "",
        "traceback": error_traceback,
        "notes": error_notes,
        "children": children,
    }


def _add_partial_journal_data(record: dict[str, Any], raw_data: Any) -> None:
    """Recover one current-schema data cell without losing malformed evidence."""
    if not raw_data or raw_data == "{}":
        return
    try:
        record["data"] = json.loads(raw_data)
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        # Partially-written journal data has historically been retained as raw
        # evidence. JSON parsing can also raise UnicodeDecodeError, ValueError
        # (for over-large integer literals), or RecursionError, so keep every
        # ordinary parser failure on that same recovery path.
        record["data"] = raw_data


def _serialize_partial_record(record: dict[str, Any], *, sequence: Any) -> str:
    """Serialize a recovered current-schema row with contextual validation."""
    try:
        return json.dumps(record, default=str)
    except Exception as exc:
        raise BundleValidationError(
            f"Journal sequence {sequence!r} cannot be serialized",
            reason_code="INVALID_JOURNAL",
        ) from exc


def _serialize_legacy_journal_data(data: Any, *, sequence: Any) -> str:
    """Serialize one legacy data cell or normalize its failure contract."""
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data)
    except Exception as exc:
        raise BundleValidationError(
            f"Legacy journal sequence {sequence!r} data cannot be serialized",
            reason_code="INVALID_JOURNAL",
        ) from exc


def _read_journal_ndjson(conn: sqlite3.Connection) -> bytes:
    """Read journal records from a SQLite database and return NDJSON bytes.

    Tries the current ``journal`` table schema first, then falls back to
    the legacy ``records(sequence, data)`` table for backwards compat.
    """
    # Check which tables exist.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    lines: list[str] = []

    if "journal" in tables:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(journal)").fetchall()}

        def select_column(name: str, fallback: str) -> str:
            return name if name in columns else f"{fallback} AS {name}"

        cursor = conn.execute(
            "SELECT sequence, session_id, kind, name, wall_ns, mono_ns, "
            f"{select_column('cpu_ns', '0')}, "
            "turn_id, data, error_type, error_msg, "
            f"{select_column('error_tb', 'NULL')}, "
            f"{select_column('error_notes', 'NULL')}, "
            "input_ref, output_ref, tags, "
            f"{select_column('error_children', 'NULL')} "
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
                "cpu_ns": row[6],
                "turn_id": row[7],
            }
            _add_partial_journal_data(record, row[8])
            error = _partial_journal_error(
                row[0],
                row[9],
                row[10],
                row[11],
                row[12],
                row[16],
            )
            if error is not None:
                record["error"] = error
            if row[13]:
                record["input_ref"] = row[13]
            if row[14]:
                record["output_ref"] = row[14]
            if isinstance(row[15], str) and row[15]:
                record["tags"] = row[15].split(",")
            lines.append(_serialize_partial_record(record, sequence=row[0]))
    elif "records" in tables:
        # Legacy schema: single JSON blob per row.
        cursor = conn.execute("SELECT sequence, data FROM records ORDER BY sequence")
        for sequence, data in cursor:
            lines.append(_serialize_legacy_journal_data(data, sequence=sequence))
    else:
        raise sqlite3.OperationalError("no recognized journal table found")

    return "\n".join(lines).encode("utf-8")


def discover_bundles(data_dir: str | None = None) -> list[Path]:
    """Discover bundles in standard directories.

    Scans ``recordings/``, ``crash-dumps/``, and ``journals/`` so live and
    crashed SQLite journals show up alongside exported ZIP bundles.  The
    list[Path] signature is preserved for back-compat; callers that need a
    per-path status use :func:`discover_bundles_with_status`.
    """
    if data_dir is None:
        data_dir = os.environ.get("EASYCAT_DATA_DIR", ".easycat")
    data_path = Path(data_dir)
    bundles: list[Path] = []
    for subdir in ("recordings", "crash-dumps", "journals"):
        search = data_path / subdir
        if search.exists():
            for f in search.iterdir():
                if not (
                    f.suffix
                    in (
                        ".zip",
                        ".bundle",
                        ".easycat-bundle",
                        ".sqlite",
                    )
                    or f.name.endswith(".easycat-bundle")
                ):
                    continue
                # A dangling symlink, or a file the crash sweep promoted
                # between this ``iterdir`` and the caller's ``stat()``, is not
                # a bundle anyone can open. Dropping it here keeps every
                # consumer from crashing on an entry that no longer resolves
                # (gh 1107).
                if not f.exists():
                    continue
                bundles.append(f)
    return sorted(bundles)


def discover_bundles_with_status(data_dir: str | None = None) -> list[tuple[Path, str]]:
    """Discover bundles with a coarse status for each path.

    Status values:

    - ``"bundle"`` — an exported ZIP bundle under ``recordings/``, or a
      cleanly-closed ``journals/`` SQLite file (an inspectable recording).
    - ``"crash-dump"`` — a promoted crash dump under ``crash-dumps/``.
    - ``"crashed (uncommitted)"`` — a ``journals/`` SQLite file with rows
      but no ``clean_close`` marker whose owning process is gone (a crash
      not yet swept to ``crash-dumps/``).
    - ``"live"`` — a ``journals/`` SQLite file held open by a running
      session (live ``live_pid`` marker or write-locked), or whose state is
      otherwise unreadable.
    """
    results: list[tuple[Path, str]] = []
    for path in discover_bundles(data_dir):
        results.append((path, _bundle_status(path)))
    return results


def _bundle_status(path: Path) -> str:
    parent = path.parent.name
    if parent == "crash-dumps":
        return "crash-dump"
    if parent != "journals" or path.suffix != ".sqlite":
        return "bundle"
    # Reuse the crash-sweep classifier so liveness detection (live_pid marker
    # + write-lock probe) stays a single source of truth.
    from easycat.runtime.crash_sweep import _read_only_state

    state = _read_only_state(path)
    if state == "crashed":
        return "crashed (uncommitted)"
    if state == "clean":
        return "bundle"
    # "skip" (live/locked/unreadable) and "empty" (just-opened) -> live.
    return "live"
