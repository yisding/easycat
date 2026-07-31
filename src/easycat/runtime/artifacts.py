"""ArtifactStore protocol and backends for large payload storage.

Every write returns a content-addressable SHA-256 ref.  Records reference
artifacts via ``input_ref`` / ``output_ref`` fields on ``JournalRecord``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from easycat._session_id import validate_persistent_session_id

logger = logging.getLogger(__name__)

__all__ = [
    "ArtifactStore",
    "FilesystemArtifactStore",
    "InMemoryArtifactStore",
    "SnapshotArtifactStore",
]

ArtifactClass = Literal["replay_critical", "debug_verbose"]


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressable store for large payloads.

    Implementations whose ``put`` blocks on I/O (disk, network) should set a
    truthy ``writes_block`` attribute; the capture pipeline then offloads
    their writes to a worker thread instead of running them inline on the
    live audio loop. Stores without the attribute are assumed in-memory,
    except ``FilesystemArtifactStore`` which is offloaded automatically.
    """

    def put(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str:
        """Store *payload* and return its SHA-256 hex ref.

        Duplicate writes of the same content return the same ref without
        re-hashing.  Must never raise — failures return ``""``.
        """
        ...

    def get(self, ref: str) -> bytes | None:
        """Retrieve a previously stored artifact by ref, or ``None``."""
        ...

    def get_head_tail(self, ref: str, *, byte_cap: int) -> bytes | None:
        """Retrieve at most the first and last ``byte_cap`` bytes of an artifact."""
        ...

    def has(self, ref: str) -> bool:
        """Check whether *ref* exists in the store."""
        ...

    def delete(self, ref: str) -> None:
        """Remove an artifact by ref (best-effort)."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256_ref(ref: str) -> bool:
    return len(ref) == 64 and all(char in "0123456789abcdef" for char in ref)


# ── In-memory backend ────────────────────────────────────────────


class InMemoryArtifactStore:
    """Bounded in-memory artifact store.

    When ``max_bytes`` is reached, new artifacts are refused (returning an
    empty ref) rather than evicted. The ring buffer already frees bytes by
    calling :meth:`delete` when a record's refs go orphan, so the cap is a
    pure safety ceiling — evicting still-referenced blobs would leave
    bundles/replay with dangling refs, so we refuse instead.
    """

    def __init__(self, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._store: dict[str, bytes] = {}
        self._current_bytes = 0
        self._cap_warned = False
        self._lock = threading.Lock()

    def put(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str:
        ref = _sha256(payload)
        with self._lock:
            if ref in self._store:
                return ref
            if len(payload) > self._max_bytes:
                logger.warning(
                    "Artifact size %d exceeds max_bytes %d; skipping",
                    len(payload),
                    self._max_bytes,
                )
                return ""
            if self._current_bytes + len(payload) > self._max_bytes:
                if not self._cap_warned:
                    self._cap_warned = True
                    logger.warning(
                        "InMemoryArtifactStore reached max_bytes %d; refusing new "
                        "artifacts (raise max_bytes or lower capture volume)",
                        self._max_bytes,
                    )
                return ""
            self._store[ref] = payload
            self._current_bytes += len(payload)
        return ref

    def get(self, ref: str) -> bytes | None:
        with self._lock:
            return self._store.get(ref)

    def get_head_tail(self, ref: str, *, byte_cap: int) -> bytes | None:
        with self._lock:
            data = self._store.get(ref)
        if data is None:
            return None
        if byte_cap <= 0 or len(data) <= 2 * byte_cap:
            return data
        return data[:byte_cap] + data[-byte_cap:]

    def has(self, ref: str) -> bool:
        with self._lock:
            return ref in self._store

    def delete(self, ref: str) -> None:
        if not _is_sha256_ref(ref):
            return
        with self._lock:
            data = self._store.pop(ref, None)
            if data is not None:
                self._current_bytes -= len(data)

    def close(self) -> None:
        with self._lock:
            self._store.clear()
            self._current_bytes = 0
            self._cap_warned = False


class SnapshotArtifactStore:
    """Read-only artifact snapshot preserved across session teardown."""

    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = dict(store)

    def put(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str:
        ref = _sha256(payload)
        return ref if ref in self._store else ""

    def get(self, ref: str) -> bytes | None:
        return self._store.get(ref)

    def get_head_tail(self, ref: str, *, byte_cap: int) -> bytes | None:
        data = self._store.get(ref)
        if data is None:
            return None
        if byte_cap <= 0 or len(data) <= 2 * byte_cap:
            return data
        return data[:byte_cap] + data[-byte_cap:]

    def has(self, ref: str) -> bool:
        return ref in self._store

    def delete(self, ref: str) -> None:
        pass

    def close(self) -> None:
        pass


# ── Filesystem backend ───────────────────────────────────────────


class FilesystemArtifactStore:
    """Persistent artifact store at ``.easycat/artifacts/<session_id>/``.

    Files are sharded as ``<sha256[:2]>/<sha256>.bin`` with ``0o600``
    permissions. Directories are created lazily on first write with ``0o700``.
    Legacy flat ``<sha256>.bin`` files remain readable.

    Bounded by ``max_bytes`` (default 512 MB) so a long or chatty session
    cannot fill the disk: once the running total of stored payloads would
    exceed the cap, *new* artifacts are refused (``put`` returns ``""``) and a
    single warning is logged.  Already-stored bytes are never deleted to make
    room — unlike :class:`InMemoryArtifactStore`, the filesystem store is the
    durable, crash-survivable record, so evicting earlier artifacts would
    silently break replay of frames that already have a journal row.  Duplicate
    writes of content already on disk do not count again.
    """

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        max_bytes: int = 512_000_000,
    ) -> None:
        validate_persistent_session_id(session_id)
        root = Path(data_dir) if data_dir else Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat"))
        self._dir = root / "artifacts" / session_id
        self._lock = threading.Lock()
        self._max_bytes = max_bytes
        self._current_bytes = self._stored_bytes()
        self._cap_warned = False

    def put(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str:
        ref = _sha256(payload)
        existing = self._existing_ref_path(ref)
        if existing is not None:
            return ref
        with self._lock:
            if self._existing_ref_path(ref) is not None:
                return ref
            if self._current_bytes + len(payload) > self._max_bytes:
                # Refuse the new write rather than delete durable bytes that
                # may already be referenced by a journal row.  Warn once so the
                # cap is visible without spamming the log per frame.
                if not self._cap_warned:
                    self._cap_warned = True
                    logger.warning(
                        "FilesystemArtifactStore reached max_bytes %d; refusing new "
                        "artifacts (set a larger max_bytes or lower capture volume)",
                        self._max_bytes,
                    )
                return ""
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._dir, 0o700)
                path = self._ref_path(ref)
                path.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(path.parent, 0o700)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(payload)
                os.chmod(tmp, 0o600)
                tmp.rename(path)
            except OSError:
                logger.warning("Artifact write failed for ref=%s", ref, exc_info=True)
                return ""
            self._current_bytes += len(payload)
        return ref

    def get(self, ref: str) -> bytes | None:
        path = self._existing_ref_path(ref)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def get_head_tail(self, ref: str, *, byte_cap: int) -> bytes | None:
        """Read a bounded head/tail window without materializing the whole file."""
        path = self._existing_ref_path(ref)
        if path is None:
            return None
        try:
            size = path.stat().st_size
            if byte_cap <= 0 or size <= 2 * byte_cap:
                return path.read_bytes()
            with path.open("rb") as fh:
                head = fh.read(byte_cap)
                fh.seek(-byte_cap, os.SEEK_END)
                tail = fh.read(byte_cap)
            return head + tail
        except OSError:
            return None

    def has(self, ref: str) -> bool:
        return self._existing_ref_path(ref) is not None

    def delete(self, ref: str) -> None:
        if not _is_sha256_ref(ref):
            return
        with self._lock:
            for path in (self._ref_path(ref), self._legacy_ref_path(ref)):
                try:
                    size = path.stat().st_size
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                self._current_bytes = max(0, self._current_bytes - size)

    def close(self) -> None:
        pass

    def _ref_path(self, ref: str) -> Path:
        return self._dir / ref[:2] / f"{ref}.bin"

    def _legacy_ref_path(self, ref: str) -> Path:
        return self._dir / f"{ref}.bin"

    def _existing_ref_path(self, ref: str) -> Path | None:
        if not _is_sha256_ref(ref):
            return None
        sharded = self._ref_path(ref)
        if sharded.is_file():
            return sharded
        legacy = self._legacy_ref_path(ref)
        return legacy if legacy.is_file() else None

    def _stored_bytes(self) -> int:
        if not self._dir.is_dir():
            return 0
        total = 0
        try:
            paths = self._dir.rglob("*.bin")
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return total
        return total
