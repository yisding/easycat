"""ArtifactStore protocol and backends for large payload storage.

Every write returns a content-addressable SHA-256 ref.  Records reference
artifacts via ``input_ref`` / ``output_ref`` fields on ``JournalRecord``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
import uuid
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

_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_DIR_FD_FUNCTIONS = getattr(os, "supports_dir_fd", set())
_FD_FUNCTIONS = getattr(os, "supports_fd", set())
_NOFOLLOW_FUNCTIONS = getattr(os, "supports_follow_symlinks", set())
_SUPPORTS_DESCRIPTOR_ARTIFACT_IO = (
    hasattr(os, "fchmod")
    and all(
        function in _DIR_FD_FUNCTIONS
        for function in (os.open, os.mkdir, os.rename, os.stat, os.unlink)
    )
    and os.scandir in _FD_FUNCTIONS
    and os.stat in _NOFOLLOW_FUNCTIONS
)


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open an absolute directory path component-by-component without symlinks."""
    absolute = path.absolute()
    current = os.open(absolute.anchor or os.curdir, _DIRECTORY_OPEN_FLAGS)
    try:
        parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_regular_at(directory_fd: int, name: str) -> int:
    fd = os.open(name, _FILE_OPEN_FLAGS, dir_fd=directory_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"Refusing non-regular artifact path: {name}")
    return fd


def _read_all_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all_fd(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Artifact write made no progress")
        view = view[written:]


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
        self._artifacts_dir = root / "artifacts"
        self._dir = self._artifacts_dir / session_id
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
        if self.has(ref):
            return ref
        with self._lock:
            if self.has(ref):
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
                if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
                    self._put_with_paths(ref, payload)
                else:
                    session_fd = _open_directory_chain(self._dir, create=True)
                    try:
                        os.fchmod(session_fd, 0o700)
                        shard_fd = self._open_shard(session_fd, ref, create=True)
                        try:
                            tmp_name = f".{ref}.{uuid.uuid4().hex}.tmp"
                            tmp_fd = os.open(
                                tmp_name,
                                os.O_WRONLY
                                | os.O_CREAT
                                | os.O_EXCL
                                | getattr(os, "O_CLOEXEC", 0)
                                | getattr(os, "O_NOFOLLOW", 0),
                                0o600,
                                dir_fd=shard_fd,
                            )
                            try:
                                try:
                                    _write_all_fd(tmp_fd, payload)
                                    os.fchmod(tmp_fd, 0o600)
                                finally:
                                    os.close(tmp_fd)
                                os.replace(
                                    tmp_name,
                                    f"{ref}.bin",
                                    src_dir_fd=shard_fd,
                                    dst_dir_fd=shard_fd,
                                )
                            except BaseException:
                                try:
                                    os.unlink(tmp_name, dir_fd=shard_fd)
                                except OSError:
                                    pass
                                raise
                        finally:
                            os.close(shard_fd)
                    finally:
                        os.close(session_fd)
            except (NotImplementedError, OSError):
                logger.warning("Artifact write failed for ref=%s", ref, exc_info=True)
                return ""
            self._current_bytes += len(payload)
        return ref

    def get(self, ref: str) -> bytes | None:
        try:
            fd = self._open_ref_fd(ref)
        except (NotImplementedError, OSError):
            return None
        try:
            return _read_all_fd(fd)
        except (NotImplementedError, OSError):
            return None
        finally:
            os.close(fd)

    def get_head_tail(self, ref: str, *, byte_cap: int) -> bytes | None:
        """Read a bounded head/tail window without materializing the whole file."""
        try:
            fd = self._open_ref_fd(ref)
        except (NotImplementedError, OSError):
            return None
        try:
            size = os.fstat(fd).st_size
            if byte_cap <= 0 or size <= 2 * byte_cap:
                return _read_all_fd(fd)
            head = os.read(fd, byte_cap)
            os.lseek(fd, -byte_cap, os.SEEK_END)
            tail = os.read(fd, byte_cap)
            return head + tail
        except (NotImplementedError, OSError):
            return None
        finally:
            os.close(fd)

    def has(self, ref: str) -> bool:
        try:
            fd = self._open_ref_fd(ref)
        except (NotImplementedError, OSError):
            return False
        os.close(fd)
        return True

    def delete(self, ref: str) -> None:
        if not _is_sha256_ref(ref):
            return
        with self._lock:
            if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
                self._delete_with_paths(ref)
                return
            try:
                session_fd = _open_directory_chain(self._dir, create=False)
            except (NotImplementedError, OSError):
                return
            try:
                try:
                    shard_fd = self._open_shard(session_fd, ref, create=False)
                except OSError:
                    shard_fd = None
                if shard_fd is not None:
                    try:
                        self._delete_name(shard_fd, f"{ref}.bin")
                    finally:
                        os.close(shard_fd)
                self._delete_name(session_fd, f"{ref}.bin")
            finally:
                os.close(session_fd)

    def close(self) -> None:
        pass

    @staticmethod
    def _open_shard(session_fd: int, ref: str, *, create: bool) -> int:
        if create:
            try:
                os.mkdir(ref[:2], mode=0o700, dir_fd=session_fd)
            except FileExistsError:
                pass
        shard_fd = os.open(ref[:2], _DIRECTORY_OPEN_FLAGS, dir_fd=session_fd)
        if create:
            os.fchmod(shard_fd, 0o700)
        return shard_fd

    def _open_ref_fd(self, ref: str) -> int:
        if not _is_sha256_ref(ref):
            raise FileNotFoundError(ref)
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return self._open_ref_with_paths(ref)
        session_fd = _open_directory_chain(self._dir, create=False)
        try:
            try:
                shard_fd = self._open_shard(session_fd, ref, create=False)
            except OSError:
                shard_fd = None
            if shard_fd is not None:
                try:
                    return _open_regular_at(shard_fd, f"{ref}.bin")
                except OSError:
                    pass
                finally:
                    os.close(shard_fd)
            return _open_regular_at(session_fd, f"{ref}.bin")
        finally:
            os.close(session_fd)

    def _delete_name(self, directory_fd: int, name: str) -> None:
        size = 0
        try:
            fd = _open_regular_at(directory_fd, name)
        except OSError:
            try:
                named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                return
            if not stat.S_ISLNK(named.st_mode):
                return
        else:
            try:
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            return
        self._current_bytes = max(0, self._current_bytes - size)

    def _stored_bytes(self) -> int:  # noqa: C901, PLR0912 - explicit no-follow traversal
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return self._stored_bytes_with_paths()
        try:
            session_fd = _open_directory_chain(self._dir, create=False)
        except (NotImplementedError, OSError):
            return 0
        total = 0
        try:
            with os.scandir(session_fd) as entries:
                for entry in entries:
                    if entry.name.endswith(".bin"):
                        try:
                            fd = _open_regular_at(session_fd, entry.name)
                        except OSError:
                            continue
                        try:
                            total += os.fstat(fd).st_size
                        finally:
                            os.close(fd)
                        continue
                    try:
                        shard_fd = os.open(entry.name, _DIRECTORY_OPEN_FLAGS, dir_fd=session_fd)
                    except OSError:
                        continue
                    try:
                        with os.scandir(shard_fd) as shard_entries:
                            for child in shard_entries:
                                if not child.name.endswith(".bin"):
                                    continue
                                try:
                                    fd = _open_regular_at(shard_fd, child.name)
                                except OSError:
                                    continue
                                try:
                                    total += os.fstat(fd).st_size
                                finally:
                                    os.close(fd)
                    finally:
                        os.close(shard_fd)
            return total
        except OSError:
            return total
        finally:
            os.close(session_fd)

    def _ref_path(self, ref: str) -> Path:
        return self._dir / ref[:2] / f"{ref}.bin"

    def _legacy_ref_path(self, ref: str) -> Path:
        return self._dir / f"{ref}.bin"

    @staticmethod
    def _path_has_link_or_reparse(path: Path) -> bool:
        absolute = path.absolute()
        return any(
            _path_is_link_or_reparse(candidate) for candidate in (absolute, *absolute.parents)
        )

    def _open_ref_with_paths(self, ref: str) -> int:
        for path in (self._ref_path(ref), self._legacy_ref_path(ref)):
            try:
                if self._path_has_link_or_reparse(path):
                    continue
                fd = os.open(path, _FILE_OPEN_FLAGS)
            except OSError:
                continue
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    return fd
            except BaseException:
                os.close(fd)
                raise
            os.close(fd)
        raise FileNotFoundError(ref)

    def _put_with_paths(self, ref: str, payload: bytes) -> None:
        # Descriptor-less platforms cannot close an external ancestor-swap
        # race with Python's path APIs. This compatibility path assumes the
        # configured data directory is caller-controlled, while rejecting any
        # link/reparse point or metadata error visible at each checkpoint.
        path = self._ref_path(ref)
        if self._path_has_link_or_reparse(path):
            raise OSError(f"Refusing symlinked artifact path: {path}")
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._dir, 0o700)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if self._path_has_link_or_reparse(path):
            raise OSError(f"Refusing symlinked artifact path: {path}")
        tmp = path.with_name(f".{ref}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("xb") as stream:
                if stream.write(payload) != len(payload):
                    raise OSError("Artifact write was incomplete")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _delete_with_paths(self, ref: str) -> None:
        for path in (self._ref_path(ref), self._legacy_ref_path(ref)):
            size = 0
            try:
                if self._path_has_link_or_reparse(path):
                    if _path_is_link_or_reparse(path):
                        path.unlink()
                    continue
                size = path.stat().st_size
                if not path.is_file():
                    continue
                path.unlink()
            except OSError:
                continue
            self._current_bytes = max(0, self._current_bytes - size)

    def _stored_bytes_with_paths(self) -> int:
        if self._path_has_link_or_reparse(self._dir) or not self._dir.is_dir():
            return 0
        total = 0
        try:
            for path in self._dir.rglob("*.bin"):
                try:
                    if self._path_has_link_or_reparse(path) or not path.is_file():
                        continue
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return total
        return total
