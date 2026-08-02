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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from easycat._session_id import validate_persistent_session_id
from easycat.runtime._journal_lock import path_file_claim

logger = logging.getLogger(__name__)

__all__ = [
    "ArtifactStore",
    "ArtifactWriteReceipt",
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
_DIR_FD_FUNCTIONS: set[Callable[..., object]] = getattr(os, "supports_dir_fd", set())
_FD_FUNCTIONS: set[Callable[..., object]] = getattr(os, "supports_fd", set())
_NOFOLLOW_FUNCTIONS: set[Callable[..., object]] = getattr(os, "supports_follow_symlinks", set())
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

    Stores that want cancellation/revocation cleanup may additionally expose
    ``put_with_cleanup_token`` and ``delete_if_cleanup_token``. The former
    returns :class:`ArtifactWriteReceipt`; the latter must atomically delete
    only while that receipt's token is still current. Unknown stores retain a
    possible orphan instead of risking deletion of a concurrently claimed ref.
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


def _is_cleanup_token(token: str) -> bool:
    return len(token) == 32 and all(char in "0123456789abcdef" for char in token)


@dataclass(frozen=True, slots=True)
class ArtifactWriteReceipt:
    """Atomic put result used for ownership-safe cancellation cleanup."""

    ref: str
    created: bool
    cleanup_token: str | None


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
        self._cleanup_tokens: dict[str, str] = {}
        self._current_bytes = 0
        self._cap_warned = False
        self._lock = threading.Lock()

    def put(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str:
        return self.put_with_cleanup_token(payload, artifact_class=artifact_class).ref

    def put_with_cleanup_token(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> ArtifactWriteReceipt:
        """Atomically put and replace the ref's conditional-cleanup token."""
        del artifact_class
        ref = _sha256(payload)
        with self._lock:
            if ref in self._store:
                token = uuid.uuid4().hex
                self._cleanup_tokens[ref] = token
                return ArtifactWriteReceipt(ref, created=False, cleanup_token=token)
            if len(payload) > self._max_bytes:
                logger.warning(
                    "Artifact size %d exceeds max_bytes %d; skipping",
                    len(payload),
                    self._max_bytes,
                )
                return ArtifactWriteReceipt("", created=False, cleanup_token=None)
            if self._current_bytes + len(payload) > self._max_bytes:
                if not self._cap_warned:
                    self._cap_warned = True
                    logger.warning(
                        "InMemoryArtifactStore reached max_bytes %d; refusing new "
                        "artifacts (raise max_bytes or lower capture volume)",
                        self._max_bytes,
                    )
                return ArtifactWriteReceipt("", created=False, cleanup_token=None)
            self._store[ref] = payload
            self._current_bytes += len(payload)
            token = uuid.uuid4().hex
            self._cleanup_tokens[ref] = token
        return ArtifactWriteReceipt(ref, created=True, cleanup_token=token)

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
            self._cleanup_tokens.pop(ref, None)
            if data is not None:
                self._current_bytes -= len(data)

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        """Delete *ref* only if no later successful put replaced its token."""
        if not _is_sha256_ref(ref):
            return False
        with self._lock:
            if self._cleanup_tokens.get(ref) != cleanup_token:
                return False
            data = self._store.pop(ref, None)
            self._cleanup_tokens.pop(ref, None)
            if data is not None:
                self._current_bytes -= len(data)
                return True
            return False

    def close(self) -> None:
        with self._lock:
            self._store.clear()
            self._cleanup_tokens.clear()
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
        return self.put_with_cleanup_token(payload, artifact_class=artifact_class).ref

    def put_with_cleanup_token(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> ArtifactWriteReceipt:
        """Atomically put and persist a cross-process cleanup token."""
        del artifact_class
        ref = _sha256(payload)
        cleanup_token = uuid.uuid4().hex
        try:
            with self._write_claim(ref):
                created = not self.has(ref)
                if created and not self._can_store_new_payload(len(payload)):
                    return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                if created and not self._put_new_locked(ref, payload):
                    return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                if not self._write_cleanup_token_locked(ref, cleanup_token):
                    if created:
                        self._delete_ref_locked(ref)
                    return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                return ArtifactWriteReceipt(
                    ref,
                    created=created,
                    cleanup_token=cleanup_token,
                )
        except (NotImplementedError, OSError):
            logger.warning("Artifact write claim failed for ref=%s", ref, exc_info=True)
            return ArtifactWriteReceipt("", created=False, cleanup_token=None)

    def _can_store_new_payload(self, payload_size: int) -> bool:
        if self._current_bytes + payload_size <= self._max_bytes:
            return True
        # Refuse the new write rather than delete durable bytes that may
        # already be referenced by a journal row. Warn once so the cap is
        # visible without spamming the log per frame.
        if not self._cap_warned:
            self._cap_warned = True
            logger.warning(
                "FilesystemArtifactStore reached max_bytes %d; refusing new "
                "artifacts (set a larger max_bytes or lower capture volume)",
                self._max_bytes,
            )
        return False

    def _put_new_locked(self, ref: str, payload: bytes) -> bool:
        try:
            if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
                self._put_with_paths(ref, payload)
            else:
                self._put_new_with_descriptors(ref, payload)
        except (NotImplementedError, OSError):
            logger.warning("Artifact write failed for ref=%s", ref, exc_info=True)
            return False
        self._current_bytes += len(payload)
        return True

    def _put_new_with_descriptors(self, ref: str, payload: bytes) -> None:
        session_fd = _open_directory_chain(self._dir, create=True)
        try:
            os.fchmod(session_fd, 0o700)
            shard_fd = self._open_shard(session_fd, ref, create=True)
            try:
                self._replace_file_at(shard_fd, f"{ref}.bin", payload)
            finally:
                os.close(shard_fd)
        finally:
            os.close(session_fd)

    @staticmethod
    def _replace_file_at(directory_fd: int, name: str, payload: bytes) -> None:
        tmp_name = f".{name}.{uuid.uuid4().hex}.tmp"
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            try:
                _write_all_fd(tmp_fd, payload)
                os.fchmod(tmp_fd, 0o600)
            finally:
                os.close(tmp_fd)
            os.replace(
                tmp_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise

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
        try:
            with self._write_claim(ref):
                self._delete_ref_locked(ref)
        except (NotImplementedError, OSError):
            logger.warning("Artifact delete claim failed for ref=%s", ref, exc_info=True)

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        """Delete *ref* iff its persisted token still belongs to this put."""
        if not _is_sha256_ref(ref) or not _is_cleanup_token(cleanup_token):
            return False
        try:
            with self._write_claim(ref):
                if self._read_cleanup_token_locked(ref) != cleanup_token:
                    return False
                self._delete_ref_locked(ref)
                return not self.has(ref)
        except (NotImplementedError, OSError):
            logger.warning(
                "Conditional artifact delete failed for ref=%s",
                ref,
                exc_info=True,
            )
            return False

    @contextmanager
    def _write_claim(self, ref: str) -> Iterator[None]:
        self._ensure_artifacts_dir()
        with path_file_claim(
            self._artifacts_dir / f"{self._dir.name}.{ref}",
            blocking=True,
            namespace="artifact",
        ) as claimed:
            if not claimed:
                raise OSError(f"Could not claim artifact store {self._dir}")
            with self._lock:
                yield

    def _ensure_artifacts_dir(self) -> None:
        if _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            artifacts_fd = _open_directory_chain(self._artifacts_dir, create=True)
            try:
                os.fchmod(artifacts_fd, 0o700)
            finally:
                os.close(artifacts_fd)
            return
        if self._path_has_link_or_reparse(self._artifacts_dir):
            raise OSError(f"Refusing symlinked artifact root: {self._artifacts_dir}")
        self._artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._artifacts_dir, 0o700)

    def _delete_ref_locked(self, ref: str) -> None:
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            self._delete_with_paths(ref)
            self._delete_cleanup_token_with_paths(ref)
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
                    self._unlink_name(shard_fd, f"{ref}.token")
                finally:
                    os.close(shard_fd)
            self._delete_name(session_fd, f"{ref}.bin")
            self._unlink_name(session_fd, f"{ref}.token")
        finally:
            os.close(session_fd)

    def _write_cleanup_token_locked(self, ref: str, cleanup_token: str) -> bool:
        try:
            if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
                self._replace_file_with_paths(
                    self._cleanup_token_path(ref),
                    cleanup_token.encode("ascii"),
                )
            else:
                session_fd = _open_directory_chain(self._dir, create=True)
                try:
                    shard_fd = self._open_shard(session_fd, ref, create=True)
                    try:
                        self._replace_file_at(
                            shard_fd,
                            f"{ref}.token",
                            cleanup_token.encode("ascii"),
                        )
                    finally:
                        os.close(shard_fd)
                finally:
                    os.close(session_fd)
        except (NotImplementedError, OSError):
            logger.warning("Artifact token write failed for ref=%s", ref, exc_info=True)
            return False
        return True

    def _read_cleanup_token_locked(self, ref: str) -> str | None:
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return self._read_cleanup_token_with_paths(ref)
        try:
            session_fd = _open_directory_chain(self._dir, create=False)
            try:
                shard_fd = self._open_shard(session_fd, ref, create=False)
                try:
                    token_fd = _open_regular_at(shard_fd, f"{ref}.token")
                finally:
                    os.close(shard_fd)
            finally:
                os.close(session_fd)
        except (NotImplementedError, OSError):
            return None
        try:
            if os.fstat(token_fd).st_size > 64:
                return None
            token = _read_all_fd(token_fd).decode("ascii")
            return token if _is_cleanup_token(token) else None
        except (OSError, UnicodeDecodeError):
            return None
        finally:
            os.close(token_fd)

    @staticmethod
    def _unlink_name(directory_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            pass

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

    def _cleanup_token_path(self, ref: str) -> Path:
        return self._dir / ref[:2] / f"{ref}.token"

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
        self._replace_file_with_paths(path, payload)

    def _replace_file_with_paths(self, path: Path, payload: bytes) -> None:
        if self._path_has_link_or_reparse(path):
            raise OSError(f"Refusing symlinked artifact path: {path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
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

    def _read_cleanup_token_with_paths(self, ref: str) -> str | None:
        path = self._cleanup_token_path(ref)
        try:
            if self._path_has_link_or_reparse(path) or not path.is_file():
                return None
            if path.stat().st_size > 64:
                return None
            token = path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return None
        return token if _is_cleanup_token(token) else None

    def _delete_cleanup_token_with_paths(self, ref: str) -> None:
        path = self._cleanup_token_path(ref)
        try:
            if self._path_has_link_or_reparse(path):
                if _path_is_link_or_reparse(path):
                    path.unlink()
                return
            path.unlink()
        except OSError:
            pass

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
