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

from easycat.debug._bundle_loader import _ArtifactAccumulator, _reject_traversal, load_bundle
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
        for line in self.journal_ndjson.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record

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

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name: str | None = None
        try:
            tmp = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False)
            tmp_name = tmp.name
            tmp.close()  # Release the fd; ZipFile reopens the path itself.
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
            conn = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                raise BundleInUseError(
                    f"Journal {journal_path} is currently in use. "
                    "Stop the session before inspecting it, or export a ZIP bundle with "
                    "`session.export_debug_bundle(...)`."
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

        manifest = Manifest(format_version=FORMAT_VERSION)

        return RunBundle(
            format_version=FORMAT_VERSION,
            manifest=manifest,
            journal_ndjson=journal_ndjson,
            artifact_index=artifacts.index,
            artifact_blobs=artifacts.blobs,
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
                if f.suffix in (
                    ".zip",
                    ".bundle",
                    ".easycat-bundle",
                    ".sqlite",
                ) or f.name.endswith(".easycat-bundle"):
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
