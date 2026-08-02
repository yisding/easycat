"""ArtifactStore protocol and backends for large payload storage.

Every write returns a content-addressable SHA-256 ref.  Records reference
artifacts via ``input_ref`` / ``output_ref`` fields on ``JournalRecord``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import threading
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
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

_ACCOUNTING_FILENAME = ".easycat-artifact-bytes-v1.json"
_ACCOUNTING_VERSION = 1
_MAX_ACCOUNTING_FILE_BYTES = 4096

_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _artifact_file_open_flags() -> int:
    """Build platform-specific flags for binary artifact-file reads."""
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _artifact_file_write_flags() -> int:
    """Build platform-specific flags for binary artifact-metadata rewrites."""
    return (
        os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _artifact_accounting_open_flags() -> int:
    """Build flags for a reusable binary accounting read/write descriptor."""
    return (
        os.O_RDWR
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


_FILE_OPEN_FLAGS = _artifact_file_open_flags()
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)
_FILE_WRITE_FLAGS = _artifact_file_write_flags()
_ACCOUNTING_OPEN_FLAGS = _artifact_accounting_open_flags()
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
_FILESYSTEM_ARTIFACT_STORES: weakref.WeakSet[FilesystemArtifactStore] = weakref.WeakSet()

_DescriptorIdentity = tuple[int, int, int, int]


def _descriptor_identity(fd: int) -> _DescriptorIdentity:
    metadata = os.fstat(fd)
    return (fd, metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _descriptor_matches(identity: _DescriptorIdentity) -> bool:
    fd, device, inode, file_type = identity
    try:
        metadata = os.fstat(fd)
    except OSError:
        return False
    return (
        metadata.st_dev == device
        and metadata.st_ino == inode
        and stat.S_IFMT(metadata.st_mode) == file_type
    )


def _reset_filesystem_artifact_stores_after_fork() -> None:
    for store in tuple(_FILESYSTEM_ARTIFACT_STORES):
        store._after_fork_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_filesystem_artifact_stores_after_fork)


class _UnsupportedArtifactAccountingVersion(ValueError):
    pass


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return _metadata_is_link_or_reparse(metadata)


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


def _create_exclusive_file(
    path: str | Path,
    payload: bytes,
    *,
    dir_fd: int | None = None,
) -> None:
    fd = os.open(path, _FILE_CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        _write_all_fd(fd, payload)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    if not hasattr(os, "fchmod"):
        os.chmod(path, 0o600)


def _rewrite_file_fd(fd: int, payload: bytes) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("Refusing non-regular artifact accounting path")
    if metadata.st_nlink != 1:
        raise OSError("Refusing hard-linked artifact accounting path")
    os.lseek(fd, 0, os.SEEK_SET)
    _write_all_fd(fd, payload)
    if metadata.st_size != len(payload):
        os.ftruncate(fd, len(payload))
    if hasattr(os, "fchmod") and metadata.st_mode & 0o777 != 0o600:
        os.fchmod(fd, 0o600)


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
    """Atomic put result; only a newly created ref carries cleanup ownership."""

    ref: str
    created: bool
    cleanup_token: str | None


@dataclass(frozen=True, slots=True)
class _ArtifactAccounting:
    """Checksummed same-boot byte coordination and optional delete intent."""

    total_bytes: int
    revision: int = 0
    pending_delete_ref: str | None = None
    pending_delete_before_bytes: int = 0

    def to_bytes(self) -> bytes:
        pending = (
            None
            if self.pending_delete_ref is None
            else {
                "before_bytes": self.pending_delete_before_bytes,
                "kind": "delete",
                "ref": self.pending_delete_ref,
            }
        )
        accounting_fields = {
            "pending": pending,
            "revision": self.revision,
            "total_bytes": self.total_bytes,
            "version": _ACCOUNTING_VERSION,
        }
        canonical = json.dumps(
            accounting_fields,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        payload = {
            "checksum": hashlib.sha256(canonical).hexdigest(),
            **accounting_fields,
        }
        return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")

    @classmethod
    def from_bytes(  # noqa: C901, PLR0912 - strict persisted-schema validation
        cls,
        payload: bytes,
    ) -> _ArtifactAccounting:
        if len(payload) > _MAX_ACCOUNTING_FILE_BYTES:
            raise ValueError("artifact accounting file is too large")
        try:
            value = json.loads(payload.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid artifact accounting JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("invalid artifact accounting shape")
        version = value.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("invalid artifact accounting version")
        if version != _ACCOUNTING_VERSION:
            raise _UnsupportedArtifactAccountingVersion(
                f"unsupported artifact accounting version: {version}"
            )
        if set(value) != {
            "checksum",
            "pending",
            "revision",
            "total_bytes",
            "version",
        }:
            raise ValueError("invalid artifact accounting shape")
        total_bytes = value["total_bytes"]
        if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
            raise ValueError("invalid artifact byte total")
        revision = value["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("invalid artifact accounting revision")
        pending = value["pending"]
        checksum = value["checksum"]
        if not isinstance(checksum, str) or not _is_sha256_ref(checksum):
            raise ValueError("invalid artifact accounting checksum")
        canonical = json.dumps(
            {
                "pending": pending,
                "revision": revision,
                "total_bytes": total_bytes,
                "version": version,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        expected_checksum = hashlib.sha256(canonical).hexdigest()
        if not hmac.compare_digest(checksum, expected_checksum):
            raise ValueError("artifact accounting checksum mismatch")
        if pending is None:
            return cls(total_bytes=total_bytes, revision=revision)
        if not isinstance(pending, dict) or set(pending) != {
            "before_bytes",
            "kind",
            "ref",
        }:
            raise ValueError("invalid pending artifact delete")
        if pending["kind"] != "delete":
            raise ValueError("unsupported pending artifact accounting mutation")
        ref = pending["ref"]
        before_bytes = pending["before_bytes"]
        if not isinstance(ref, str) or not _is_sha256_ref(ref):
            raise ValueError("invalid pending artifact ref")
        if (
            isinstance(before_bytes, bool)
            or not isinstance(before_bytes, int)
            or before_bytes < 0
            or before_bytes > total_bytes
        ):
            raise ValueError("invalid pending artifact byte count")
        return cls(
            total_bytes=total_bytes,
            revision=revision,
            pending_delete_ref=ref,
            pending_delete_before_bytes=before_bytes,
        )


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
        """Put *payload*, issuing cleanup ownership only when it creates the ref."""
        del artifact_class
        ref = _sha256(payload)
        with self._lock:
            if ref in self._store:
                self._cleanup_tokens.pop(ref, None)
                return ArtifactWriteReceipt(ref, created=False, cleanup_token=None)
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
        self._operation_state = threading.local()
        self._accounting_fd: tuple[int, _DescriptorIdentity] | None = None
        self._active_session_fds: dict[int, _DescriptorIdentity] = {}
        self._owner_pid = os.getpid()
        _FILESYSTEM_ARTIFACT_STORES.add(self)
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self._cap_warned = False
        self._cap_rejected_accounting: tuple[int, int] | None = None
        self._needs_open_reconciliation = True
        # Construction is the host-crash boundary: every store opening an
        # existing session strictly recounts physical blobs under the shared
        # claim before trusting same-boot incremental accounting.
        session_dir_state = self._session_dir_state()
        if session_dir_state == "absent":
            self._needs_open_reconciliation = False
        elif session_dir_state == "directory":
            try:
                with self._write_claim():
                    self._reconcile_accounting_on_open_locked()
                    self._needs_open_reconciliation = False
            except (NotImplementedError, OSError, ValueError):
                logger.warning(
                    "Artifact accounting initialization failed for %s",
                    self._dir,
                    exc_info=True,
                )

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
        """Put *payload*, issuing cleanup ownership only when it creates the ref."""
        del artifact_class
        ref = _sha256(payload)
        try:
            with self._write_claim():
                with self._reuse_session_fd_locked(create=True):
                    accounting = self._load_accounting_locked(persist_missing=False)
                    created = not self.has(ref)
                    if not created:
                        if not self._revoke_cleanup_token_locked(ref):
                            return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                        return ArtifactWriteReceipt(ref, created=False, cleanup_token=None)
                    reserved = self._reserve_new_payload_locked(
                        accounting,
                        payload_size=len(payload),
                    )
                    if reserved is None:
                        return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                    if not self._put_new_locked(ref, payload):
                        return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                    cleanup_token = uuid.uuid4().hex
                    if not self._create_cleanup_token_locked(ref, cleanup_token):
                        self._delete_ref_locked(ref)
                        return ArtifactWriteReceipt("", created=False, cleanup_token=None)
                    return ArtifactWriteReceipt(
                        ref,
                        created=True,
                        cleanup_token=cleanup_token,
                    )
        except (NotImplementedError, OSError, ValueError):
            logger.warning("Artifact write claim failed for ref=%s", ref, exc_info=True)
            return ArtifactWriteReceipt("", created=False, cleanup_token=None)

    def _warn_cap_reached(self) -> None:
        if not self._cap_warned:
            self._cap_warned = True
            logger.warning(
                "FilesystemArtifactStore reached max_bytes %d; refusing new "
                "artifacts (set a larger max_bytes or lower capture volume)",
                self._max_bytes,
            )

    def _put_new_locked(self, ref: str, payload: bytes) -> bool:
        try:
            if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
                self._put_with_paths(ref, payload)
            else:
                self._put_new_with_descriptors(ref, payload)
        except (NotImplementedError, OSError):
            logger.warning("Artifact write failed for ref=%s", ref, exc_info=True)
            return False
        return True

    def _put_new_with_descriptors(self, ref: str, payload: bytes) -> None:
        session_fd, owns_session_fd = self._active_or_open_session_fd(create=True)
        try:
            os.fchmod(session_fd, 0o700)
            shard_fd = self._open_shard(session_fd, ref, create=True)
            try:
                self._replace_file_at(shard_fd, f"{ref}.bin", payload)
            finally:
                os.close(shard_fd)
        finally:
            if owns_session_fd:
                os.close(session_fd)

    @staticmethod
    def _replace_file_at(
        directory_fd: int,
        name: str,
        payload: bytes,
    ) -> None:
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
            with self._write_claim():
                with self._reuse_session_fd_locked(create=True):
                    accounting = self._load_accounting_locked(persist_missing=False)
                    before_bytes = self._ref_stored_bytes_locked(ref)
                    if before_bytes > accounting.total_bytes:
                        raise ValueError("artifact accounting baseline is inconsistent")
                    pending = self._begin_pending_delete_locked(accounting, ref, before_bytes)
                    self._delete_ref_locked(ref)
                    self._cap_rejected_accounting = None
                    self._complete_pending_delete_locked(pending)
        except (NotImplementedError, OSError, ValueError):
            logger.warning("Artifact delete claim failed for ref=%s", ref, exc_info=True)

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        """Delete *ref* iff its persisted token still belongs to this put."""
        if not _is_sha256_ref(ref) or not _is_cleanup_token(cleanup_token):
            return False
        try:
            with self._write_claim():
                with self._reuse_session_fd_locked(create=True):
                    accounting = self._load_accounting_locked(persist_missing=False)
                    if self._read_cleanup_token_locked(ref) != cleanup_token:
                        return False
                    before_bytes = self._ref_stored_bytes_locked(ref)
                    if before_bytes > accounting.total_bytes:
                        raise ValueError("artifact accounting baseline is inconsistent")
                    pending = self._begin_pending_delete_locked(accounting, ref, before_bytes)
                    self._delete_ref_locked(ref)
                    self._cap_rejected_accounting = None
                    self._complete_pending_delete_locked(pending)
                    return not self.has(ref)
        except (NotImplementedError, OSError, ValueError):
            logger.warning(
                "Conditional artifact delete failed for ref=%s",
                ref,
                exc_info=True,
            )
            return False

    @contextmanager
    def _write_claim(self) -> Iterator[None]:
        self._ensure_artifacts_dir()
        with path_file_claim(
            self._dir,
            blocking=True,
            namespace="artifact",
        ) as claimed:
            if not claimed:
                raise OSError(f"Could not claim artifact store {self._dir}")
            with self._lock:
                yield

    @contextmanager
    def _reuse_session_fd_locked(self, *, create: bool) -> Iterator[None]:
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO or self._current_session_fd() is not None:
            yield
            return
        session_fd = _open_directory_chain(self._dir, create=create)
        thread_id = threading.get_ident()
        identity = _descriptor_identity(session_fd)
        try:
            if create:
                os.fchmod(session_fd, 0o700)
            self._operation_state.session_fd = (os.getpid(), session_fd)
            self._active_session_fds[thread_id] = identity
            yield
        finally:
            self._operation_state.session_fd = None
            if self._active_session_fds.get(thread_id) == identity:
                self._active_session_fds.pop(thread_id, None)
            if _descriptor_matches(identity):
                os.close(session_fd)

    def _current_session_fd(self) -> int | None:
        active = getattr(self._operation_state, "session_fd", None)
        if active is None or active[0] != os.getpid():
            return None
        return active[1]

    def _active_or_open_session_fd(self, *, create: bool) -> tuple[int, bool]:
        active = self._current_session_fd()
        if active is not None:
            return active, False
        return _open_directory_chain(self._dir, create=create), True

    def _close_accounting_fd_locked(self) -> None:
        cached = self._accounting_fd
        self._accounting_fd = None
        if cached is not None and _descriptor_matches(cached[1]):
            os.close(cached[1][0])

    def _accounting_fd_at_locked(self, session_fd: int) -> int:
        try:
            named = os.stat(
                _ACCOUNTING_FILENAME,
                dir_fd=session_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            self._close_accounting_fd_locked()
            raise
        if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1:
            self._close_accounting_fd_locked()
            raise OSError("Refusing unsafe artifact accounting path")

        cached = self._accounting_fd
        if cached is not None and cached[0] == os.getpid():
            try:
                opened = os.fstat(cached[1][0])
            except OSError:
                self._close_accounting_fd_locked()
            else:
                if (
                    stat.S_ISREG(opened.st_mode)
                    and opened.st_nlink == 1
                    and opened.st_dev == named.st_dev
                    and opened.st_ino == named.st_ino
                ):
                    os.lseek(cached[1][0], 0, os.SEEK_SET)
                    return cached[1][0]
                self._close_accounting_fd_locked()
        elif cached is not None:
            # A forked child cannot prove that the inherited integer still
            # names our descriptor; discard it without risking an unrelated
            # child resource that reused the same number.
            self._accounting_fd = None

        fd = os.open(
            _ACCOUNTING_FILENAME,
            _ACCOUNTING_OPEN_FLAGS,
            dir_fd=session_fd,
        )
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != named.st_dev
                or opened.st_ino != named.st_ino
            ):
                raise OSError("Artifact accounting path changed while opening")
        except BaseException:
            os.close(fd)
            raise
        self._accounting_fd = (os.getpid(), _descriptor_identity(fd))
        self._owner_pid = os.getpid()
        return fd

    def _after_fork_child(self) -> None:
        descriptors: set[_DescriptorIdentity] = set(self._active_session_fds.values())
        cached = self._accounting_fd
        if cached is not None:
            descriptors.add(cached[1])
        for identity in descriptors:
            if _descriptor_matches(identity):
                os.close(identity[0])
        self._accounting_fd = None
        self._active_session_fds = {}
        self._operation_state = threading.local()
        self._lock = threading.Lock()
        self._owner_pid = os.getpid()

    def _session_dir_state(self) -> Literal["absent", "directory", "uncertain"]:
        absolute = self._dir.absolute()
        current = Path(absolute.anchor) if absolute.anchor else Path()
        parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        for part in parts:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                return "absent"
            except OSError:
                return "uncertain"
            if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                return "uncertain"
        return "directory"

    def _load_accounting_locked(
        self,
        *,
        persist_missing: bool = True,
    ) -> _ArtifactAccounting:
        if self._needs_open_reconciliation:
            session_dir_state = self._session_dir_state()
            if session_dir_state == "uncertain":
                raise OSError(f"Artifact session metadata is unavailable for {self._dir}")
            if session_dir_state == "directory":
                reconciled = self._reconcile_accounting_on_open_locked()
                self._needs_open_reconciliation = False
                return reconciled
            self._needs_open_reconciliation = False
        persist_rebuild = False
        try:
            accounting = self._read_accounting_locked()
        except FileNotFoundError:
            accounting = None
            persist_rebuild = persist_missing
        except _UnsupportedArtifactAccountingVersion:
            raise
        except (OSError, ValueError):
            logger.warning(
                "Artifact accounting metadata is invalid for %s; rebuilding",
                self._dir,
                exc_info=True,
            )
            accounting = None
            persist_rebuild = True
        if accounting is None:
            accounting = _ArtifactAccounting(total_bytes=self._stored_bytes())
            if persist_rebuild:
                accounting = self._write_accounting_locked(accounting)
            self._cap_rejected_accounting = None
        if accounting.pending_delete_ref is not None:
            accounting = self._recover_pending_delete_locked(accounting)
        self._current_bytes = accounting.total_bytes
        return accounting

    def _reconcile_accounting_on_open_locked(self) -> _ArtifactAccounting:
        try:
            previous = self._read_accounting_locked()
        except _UnsupportedArtifactAccountingVersion:
            raise
        except (OSError, ValueError):
            previous = _ArtifactAccounting(total_bytes=0)
        accounting = replace(
            previous,
            total_bytes=self._stored_bytes(),
            pending_delete_ref=None,
            pending_delete_before_bytes=0,
        )
        accounting = self._write_accounting_locked(accounting)
        self._current_bytes = accounting.total_bytes
        self._cap_rejected_accounting = None
        return accounting

    def _reserve_new_payload_locked(
        self,
        accounting: _ArtifactAccounting,
        *,
        payload_size: int,
    ) -> _ArtifactAccounting | None:
        if accounting.total_bytes + payload_size > self._max_bytes:
            rejected_accounting = (accounting.revision, accounting.total_bytes)
            if self._cap_rejected_accounting == rejected_accounting:
                self._warn_cap_reached()
                return None
            shared_total = accounting.total_bytes
            actual_bytes = self._stored_bytes()
            accounting = replace(accounting, total_bytes=actual_bytes)
            self._current_bytes = actual_bytes
            if actual_bytes + payload_size > self._max_bytes:
                if actual_bytes != shared_total:
                    accounting = self._write_accounting_locked(accounting)
                self._cap_rejected_accounting = (
                    accounting.revision,
                    accounting.total_bytes,
                )
                self._warn_cap_reached()
                return None
        reserved = replace(accounting, total_bytes=accounting.total_bytes + payload_size)
        reserved = self._write_accounting_locked(reserved)
        self._current_bytes = reserved.total_bytes
        self._cap_rejected_accounting = None
        return reserved

    def _begin_pending_delete_locked(
        self,
        accounting: _ArtifactAccounting,
        ref: str,
        before_bytes: int,
    ) -> _ArtifactAccounting:
        if accounting.pending_delete_ref is not None:
            raise ValueError("artifact delete is already pending")
        pending = replace(
            accounting,
            pending_delete_ref=ref,
            pending_delete_before_bytes=before_bytes,
        )
        pending = self._write_accounting_locked(pending)
        return pending

    def _recover_pending_delete_locked(
        self,
        pending: _ArtifactAccounting,
    ) -> _ArtifactAccounting:
        ref = pending.pending_delete_ref
        if ref is None:
            return pending
        # Replay the unlink even when the blob appears absent so live peers
        # complete an interrupted same-boot delete intent before decreasing
        # the shared ledger. A post-host-crash constructor instead recounts
        # physical blobs before trusting the ledger.
        self._delete_ref_locked(ref)
        return self._complete_pending_delete_locked(pending)

    def _complete_pending_delete_locked(
        self,
        pending: _ArtifactAccounting,
    ) -> _ArtifactAccounting:
        ref = pending.pending_delete_ref
        if ref is None:
            raise ValueError("artifact delete intent is missing")
        after_bytes = self._ref_stored_bytes_locked(ref)
        updated_total = pending.total_bytes - pending.pending_delete_before_bytes + after_bytes
        if updated_total < 0:
            raise ValueError("artifact delete accounting produced a negative total")
        updated = replace(
            pending,
            total_bytes=updated_total,
            pending_delete_ref=None,
            pending_delete_before_bytes=0,
        )
        updated = self._write_accounting_locked(updated)
        self._current_bytes = updated.total_bytes
        self._cap_rejected_accounting = None
        return updated

    def _read_accounting_locked(self) -> _ArtifactAccounting:
        close_fd = False
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            path = self._accounting_path()
            if self._path_has_link_or_reparse(path):
                raise OSError(f"Refusing symlinked artifact accounting path: {path}")
            try:
                fd = os.open(path, _FILE_OPEN_FLAGS)
            except FileNotFoundError:
                raise
            close_fd = True
        else:
            session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
            try:
                fd = self._accounting_fd_at_locked(session_fd)
            finally:
                if owns_session_fd:
                    os.close(session_fd)
        try:
            metadata = os.fstat(fd)
            if metadata.st_nlink != 1:
                raise OSError("Refusing hard-linked artifact accounting path")
            if metadata.st_size > _MAX_ACCOUNTING_FILE_BYTES:
                raise ValueError("artifact accounting file is too large")
            return _ArtifactAccounting.from_bytes(_read_all_fd(fd))
        finally:
            if close_fd:
                os.close(fd)

    def _write_accounting_locked(self, accounting: _ArtifactAccounting) -> _ArtifactAccounting:
        written = replace(accounting, revision=accounting.revision + 1)
        payload = written.to_bytes()
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            self._write_accounting_with_paths(payload)
            return written
        session_fd, owns_session_fd = self._active_or_open_session_fd(create=True)
        try:
            os.fchmod(session_fd, 0o700)
            try:
                fd = self._accounting_fd_at_locked(session_fd)
            except FileNotFoundError:
                self._replace_file_at(
                    session_fd,
                    _ACCOUNTING_FILENAME,
                    payload,
                )
            else:
                _rewrite_file_fd(fd, payload)
        finally:
            if owns_session_fd:
                os.close(session_fd)
        return written

    def _write_accounting_with_paths(self, payload: bytes) -> None:
        path = self._accounting_path()
        if self._path_has_link_or_reparse(path):
            raise OSError(f"Refusing symlinked artifact accounting path: {path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        try:
            fd = os.open(path, _FILE_WRITE_FLAGS)
        except FileNotFoundError:
            self._replace_file_with_paths(path, payload)
            return
        try:
            if not hasattr(os, "fchmod"):
                if self._path_has_link_or_reparse(path):
                    raise OSError(f"Refusing symlinked artifact accounting path: {path}")
                os.chmod(path, 0o600)
            _rewrite_file_fd(fd, payload)
        finally:
            os.close(fd)

    def _ref_stored_bytes_locked(  # noqa: C901, PLR0912 - explicit no-follow traversal
        self,
        ref: str,
    ) -> int:
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return sum(self._regular_path_size(path) for path in self._ref_paths(ref))
        try:
            session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
        except FileNotFoundError:
            return 0
        try:
            total = self._regular_file_size_at(session_fd, f"{ref}.bin")
            try:
                shard_fd = self._open_shard(session_fd, ref, create=False)
            except FileNotFoundError:
                shard_fd = None
            except OSError as exc:
                try:
                    shard_metadata = os.stat(
                        ref[:2],
                        dir_fd=session_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    shard_fd = None
                except OSError:
                    raise exc from None
                else:
                    if stat.S_ISLNK(shard_metadata.st_mode) or not stat.S_ISDIR(
                        shard_metadata.st_mode
                    ):
                        shard_fd = None
                    else:
                        raise exc
            if shard_fd is not None:
                try:
                    total += self._regular_file_size_at(shard_fd, f"{ref}.bin")
                finally:
                    os.close(shard_fd)
            return total
        finally:
            if owns_session_fd:
                os.close(session_fd)

    @staticmethod
    def _regular_file_size_at(directory_fd: int, name: str) -> int:
        try:
            fd = _open_regular_at(directory_fd, name)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return 0
            except OSError:
                raise exc from None
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return 0
            raise exc
        try:
            return os.fstat(fd).st_size
        finally:
            os.close(fd)

    def _regular_path_size(self, path: Path) -> int:
        if self._path_has_link_or_reparse(path):
            if _path_is_link_or_reparse(path):
                return 0
            raise OSError(f"Refusing unsafe artifact path: {path}")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(metadata.st_mode):
            return 0
        return metadata.st_size

    def _ref_paths(self, ref: str) -> tuple[Path, Path]:
        return (self._ref_path(ref), self._legacy_ref_path(ref))

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
            session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
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
            if owns_session_fd:
                os.close(session_fd)

    def _create_cleanup_token_locked(self, ref: str, cleanup_token: str) -> bool:
        payload = cleanup_token.encode("ascii")
        try:
            if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
                self._create_cleanup_token_with_paths(ref, payload)
            else:
                session_fd, owns_session_fd = self._active_or_open_session_fd(create=True)
                try:
                    shard_fd = self._open_shard(session_fd, ref, create=True)
                    try:
                        name = f"{ref}.token"
                        try:
                            _create_exclusive_file(name, payload, dir_fd=shard_fd)
                        except FileExistsError:
                            if not self._unlink_name(shard_fd, name):
                                raise OSError(
                                    "Could not revoke stale artifact cleanup token"
                                ) from None
                            _create_exclusive_file(name, payload, dir_fd=shard_fd)
                    finally:
                        os.close(shard_fd)
                finally:
                    if owns_session_fd:
                        os.close(session_fd)
        except (NotImplementedError, OSError):
            logger.warning("Artifact token write failed for ref=%s", ref, exc_info=True)
            return False
        return True

    def _revoke_cleanup_token_locked(self, ref: str) -> bool:
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return self._delete_cleanup_token_with_paths(ref)
        try:
            session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
        except FileNotFoundError:
            return True
        except (NotImplementedError, OSError):
            return False
        try:
            try:
                shard_fd = self._open_shard(session_fd, ref, create=False)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            try:
                return self._unlink_name(shard_fd, f"{ref}.token")
            finally:
                os.close(shard_fd)
        finally:
            if owns_session_fd:
                os.close(session_fd)

    def _read_cleanup_token_locked(self, ref: str) -> str | None:
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return self._read_cleanup_token_with_paths(ref)
        try:
            session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
            try:
                shard_fd = self._open_shard(session_fd, ref, create=False)
                try:
                    token_fd = _open_regular_at(shard_fd, f"{ref}.token")
                finally:
                    os.close(shard_fd)
            finally:
                if owns_session_fd:
                    os.close(session_fd)
        except (NotImplementedError, OSError):
            return None
        try:
            metadata = os.fstat(token_fd)
            if metadata.st_nlink != 1 or metadata.st_size > 64:
                return None
            token = _read_all_fd(token_fd).decode("ascii")
            return token if _is_cleanup_token(token) else None
        except (OSError, UnicodeDecodeError):
            return None
        finally:
            os.close(token_fd)

    @staticmethod
    def _unlink_name(directory_fd: int, name: str) -> bool:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            # Defensive fallback for runtimes without ``register_at_fork``:
            # do not touch a possibly inherited locked mutex or reused fd.
            self._accounting_fd = None
            return
        with self._lock:
            self._close_accounting_fd_locked()

    def __del__(self) -> None:
        try:
            cached = self._accounting_fd
            if cached is not None and cached[0] == os.getpid():
                self._accounting_fd = None
                if _descriptor_matches(cached[1]):
                    os.close(cached[1][0])
        except (AttributeError, OSError):
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
        session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
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
            if owns_session_fd:
                os.close(session_fd)

    def _delete_name(self, directory_fd: int, name: str) -> None:
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
            os.close(fd)
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            return

    def _stored_bytes(self) -> int:  # noqa: C901 - explicit no-follow traversal
        if not _SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            return self._stored_bytes_with_paths()
        try:
            session_fd, owns_session_fd = self._active_or_open_session_fd(create=False)
        except FileNotFoundError:
            return 0
        total = 0
        try:
            with os.scandir(session_fd) as entries:
                for entry in entries:
                    if entry.name.endswith(".bin"):
                        total += self._regular_file_size_at(session_fd, entry.name)
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        continue
                    try:
                        shard_fd = os.open(
                            entry.name,
                            _DIRECTORY_OPEN_FLAGS,
                            dir_fd=session_fd,
                        )
                    except FileNotFoundError:
                        continue
                    try:
                        with os.scandir(shard_fd) as shard_entries:
                            for child in shard_entries:
                                if not child.name.endswith(".bin"):
                                    continue
                                total += self._regular_file_size_at(shard_fd, child.name)
                    finally:
                        os.close(shard_fd)
            return total
        finally:
            if owns_session_fd:
                os.close(session_fd)

    def _ref_path(self, ref: str) -> Path:
        return self._dir / ref[:2] / f"{ref}.bin"

    def _legacy_ref_path(self, ref: str) -> Path:
        return self._dir / f"{ref}.bin"

    def _cleanup_token_path(self, ref: str) -> Path:
        return self._dir / ref[:2] / f"{ref}.token"

    def _accounting_path(self) -> Path:
        return self._dir / _ACCOUNTING_FILENAME

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

    def _replace_file_with_paths(
        self,
        path: Path,
        payload: bytes,
    ) -> None:
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
            metadata = path.stat()
            if metadata.st_nlink != 1 or metadata.st_size > 64:
                return None
            token = path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return None
        return token if _is_cleanup_token(token) else None

    def _create_cleanup_token_with_paths(self, ref: str, payload: bytes) -> None:
        path = self._cleanup_token_path(ref)
        if self._path_has_link_or_reparse(path):
            if not self._delete_cleanup_token_with_paths(ref):
                raise OSError(f"Refusing symlinked artifact cleanup token: {path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        try:
            _create_exclusive_file(path, payload)
        except FileExistsError:
            if not self._delete_cleanup_token_with_paths(ref):
                raise OSError(f"Could not revoke stale artifact cleanup token: {path}") from None
            _create_exclusive_file(path, payload)

    def _delete_cleanup_token_with_paths(self, ref: str) -> bool:
        path = self._cleanup_token_path(ref)
        try:
            if self._path_has_link_or_reparse(path):
                if _path_is_link_or_reparse(path):
                    path.unlink()
                    return True
                return False
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _delete_with_paths(self, ref: str) -> None:
        for path in (self._ref_path(ref), self._legacy_ref_path(ref)):
            try:
                if self._path_has_link_or_reparse(path):
                    if _path_is_link_or_reparse(path):
                        path.unlink()
                    continue
                if not path.is_file():
                    continue
                path.unlink()
            except OSError:
                continue

    def _stored_bytes_with_paths(self) -> int:  # noqa: C901 - fail-closed traversal
        if self._path_has_link_or_reparse(self._dir):
            raise OSError(f"Refusing unsafe artifact session path: {self._dir}")
        try:
            metadata = self._dir.lstat()
        except FileNotFoundError:
            return 0
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"Artifact session path is not a directory: {self._dir}")
        total = 0
        pending = [self._dir]
        while pending:
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except FileNotFoundError:
                continue
            with entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if _metadata_is_link_or_reparse(metadata):
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(Path(entry.path))
                        continue
                    if entry.name.endswith(".bin") and stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
        return total
