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
    """Content-addressable store for large payloads."""

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


# ── In-memory backend ────────────────────────────────────────────


class InMemoryArtifactStore:
    """Bounded in-memory artifact store.

    When ``max_bytes`` is exceeded, the oldest entries are evicted.
    """

    def __init__(self, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._store: dict[str, bytes] = {}
        self._order: list[str] = []  # insertion order for eviction
        self._current_bytes = 0
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
            self._evict_if_needed(len(payload))
            self._store[ref] = payload
            self._order.append(ref)
            self._current_bytes += len(payload)
        return ref

    def get(self, ref: str) -> bytes | None:
        with self._lock:
            return self._store.get(ref)

    def has(self, ref: str) -> bool:
        with self._lock:
            return ref in self._store

    def delete(self, ref: str) -> None:
        with self._lock:
            data = self._store.pop(ref, None)
            if data is not None:
                self._current_bytes -= len(data)
                try:
                    self._order.remove(ref)
                except ValueError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._store.clear()
            self._order.clear()
            self._current_bytes = 0

    def _evict_if_needed(self, incoming_bytes: int) -> None:
        """Evict oldest entries until there is room. Caller holds lock."""
        while self._order and self._current_bytes + incoming_bytes > self._max_bytes:
            oldest_ref = self._order.pop(0)
            data = self._store.pop(oldest_ref, None)
            if data is not None:
                self._current_bytes -= len(data)


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

    def has(self, ref: str) -> bool:
        return ref in self._store

    def delete(self, ref: str) -> None:
        pass

    def close(self) -> None:
        pass


# ── Filesystem backend ───────────────────────────────────────────


class FilesystemArtifactStore:
    """Persistent artifact store at ``.easycat/artifacts/<session_id>/``.

    Files are ``<sha256>.bin``, permissions ``0o600``.
    Directories are created lazily on first write with ``0o700``.
    """

    def __init__(self, session_id: str, *, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir) if data_dir else Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat"))
        self._dir = root / "artifacts" / session_id
        self._lock = threading.Lock()

    def put(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str:
        ref = _sha256(payload)
        path = self._ref_path(ref)
        if path.exists():
            return ref
        with self._lock:
            if path.exists():
                return ref
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._dir, 0o700)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(payload)
                os.chmod(tmp, 0o600)
                tmp.rename(path)
            except OSError:
                logger.warning("Artifact write failed for ref=%s", ref, exc_info=True)
                return ""
        return ref

    def get(self, ref: str) -> bytes | None:
        path = self._ref_path(ref)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def has(self, ref: str) -> bool:
        return self._ref_path(ref).exists()

    def delete(self, ref: str) -> None:
        try:
            self._ref_path(ref).unlink(missing_ok=True)
        except OSError:
            pass

    def close(self) -> None:
        pass

    def _ref_path(self, ref: str) -> Path:
        return self._dir / f"{ref}.bin"
