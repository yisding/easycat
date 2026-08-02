from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import Any

import pytest

from easycat.runtime.artifacts import ArtifactWriteLease
from easycat.runtime.context import RunContext
from easycat.stages.base import put_artifact_async


class _BlockingArtifactStore:
    writes_block = True

    def __init__(
        self,
        payload: bytes,
        *,
        preexisting: bool = False,
        block_delete: bool = False,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self.ref = hashlib.sha256(payload).hexdigest()
        self.refs = {self.ref} if preexisting else set()
        self.put_started = asyncio.Event()
        self.put_release = threading.Event()
        self.delete_started = asyncio.Event()
        self.delete_release = threading.Event()
        self.delete_calls: list[str] = []
        self._block_delete = block_delete
        self._write_lock = threading.Lock()

    def put(self, payload: bytes, *, artifact_class: str = "replay_critical") -> str:
        lease = self.put_with_cleanup_lease(payload, artifact_class=artifact_class)
        try:
            return lease.ref
        finally:
            lease.commit()

    def put_with_cleanup_lease(
        self,
        payload: bytes,
        *,
        artifact_class: str = "replay_critical",
    ) -> ArtifactWriteLease:
        del payload, artifact_class
        self._write_lock.acquire()
        try:
            self._loop.call_soon_threadsafe(self.put_started.set)
            if not self.put_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release artifact put")
            created = self.ref not in self.refs
            self.refs.add(self.ref)
            return ArtifactWriteLease(
                self.ref,
                created=created,
                release=self._write_lock.release,
                rollback=lambda: self._delete_locked(self.ref),
            )
        except BaseException:
            self._write_lock.release()
            raise

    def has(self, ref: str) -> bool:
        return ref in self.refs

    def delete(self, ref: str) -> None:
        with self._write_lock:
            self._delete_locked(ref)

    def _delete_locked(self, ref: str) -> None:
        self.delete_calls.append(ref)
        self._loop.call_soon_threadsafe(self.delete_started.set)
        if self._block_delete and not self.delete_release.wait(timeout=5):
            raise AssertionError("timed out waiting to release artifact delete")
        self.refs.discard(ref)


class _UntrackedBlockingArtifactStore:
    writes_block = True

    def __init__(self, payload: bytes) -> None:
        self._loop = asyncio.get_running_loop()
        self.ref = hashlib.sha256(payload).hexdigest()
        self.refs: set[str] = set()
        self.put_started = asyncio.Event()
        self.put_release = threading.Event()
        self.delete_calls: list[str] = []

    def put(self, payload: bytes, *, artifact_class: str = "replay_critical") -> str:
        del payload, artifact_class
        self._loop.call_soon_threadsafe(self.put_started.set)
        if not self.put_release.wait(timeout=5):
            raise AssertionError("timed out waiting to release artifact put")
        self.refs.add(self.ref)
        return self.ref

    def has(self, ref: str) -> bool:
        return ref in self.refs

    def delete(self, ref: str) -> None:
        self.delete_calls.append(ref)
        self.refs.discard(ref)


def _context(store: Any) -> RunContext:
    return RunContext(
        run_id="run-1",
        session_id="session-1",
        runtime_mode="chained_pipeline",
        artifact_store=store,
    )


@pytest.mark.asyncio
async def test_cancelled_blocking_put_remains_owned_until_new_ref_is_deleted() -> None:
    payload = b"new artifact"
    store = _BlockingArtifactStore(payload)
    task = asyncio.create_task(put_artifact_async(_context(store), payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)

        assert not task.done()

        store.put_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.refs == set()
        assert store.delete_calls == [store.ref]
    finally:
        store.put_release.set()
        store.delete_release.set()


@pytest.mark.asyncio
async def test_cancelled_blocking_put_preserves_preexisting_ref() -> None:
    payload = b"shared artifact"
    store = _BlockingArtifactStore(payload, preexisting=True)
    task = asyncio.create_task(put_artifact_async(_context(store), payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        task.cancel()
        store.put_release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.refs == {store.ref}
        assert store.delete_calls == []
    finally:
        store.put_release.set()
        store.delete_release.set()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_blocking_cleanup() -> None:
    payload = b"repeated cancellation"
    store = _BlockingArtifactStore(payload, block_delete=True)
    task = asyncio.create_task(put_artifact_async(_context(store), payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        task.cancel()
        store.put_release.set()
        await asyncio.wait_for(store.delete_started.wait(), timeout=5)

        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        store.delete_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.refs == set()
        assert store.delete_calls == [store.ref]
    finally:
        store.put_release.set()
        store.delete_release.set()


@pytest.mark.asyncio
async def test_cancelled_write_then_record_leaves_neither_ref_nor_record() -> None:
    payload = b"paired stage artifact"
    store = _BlockingArtifactStore(payload)
    records: list[str] = []

    async def write_then_record() -> None:
        ref = await put_artifact_async(_context(store), payload)
        assert ref is not None
        records.append(ref)

    task = asyncio.create_task(write_then_record())
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        task.cancel()
        store.put_release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.refs == set()
        assert records == []
    finally:
        store.put_release.set()
        store.delete_release.set()


@pytest.mark.asyncio
async def test_cancelled_same_payload_writer_cannot_delete_successful_ref() -> None:
    payload = b"concurrent artifact"
    store = _BlockingArtifactStore(payload)
    cancelled = asyncio.create_task(put_artifact_async(_context(store), payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        successful = asyncio.create_task(put_artifact_async(_context(store), payload))
        cancelled.cancel()
        store.put_release.set()

        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert await successful == store.ref

        assert store.refs == {store.ref}
        assert store.delete_calls == [store.ref]
    finally:
        store.put_release.set()
        store.delete_release.set()


@pytest.mark.asyncio
async def test_revoked_same_payload_writer_cannot_delete_successful_ref() -> None:
    payload = b"concurrent revoked artifact"
    store = _BlockingArtifactStore(payload)
    capture_enabled = True
    capture_epoch = 1
    revoked_ctx = RunContext(
        run_id="run-revoked",
        session_id="session-revoked",
        runtime_mode="chained_pipeline",
        artifact_store=store,
        audio_capture_enabled=lambda: capture_enabled,
        audio_capture_epoch=lambda: capture_epoch,
    )
    revoked = asyncio.create_task(put_artifact_async(revoked_ctx, payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        successful = asyncio.create_task(put_artifact_async(_context(store), payload))
        capture_enabled = False
        capture_epoch += 1
        store.put_release.set()

        assert await revoked is None
        assert await successful == store.ref

        assert store.refs == {store.ref}
        assert store.delete_calls == [store.ref]
    finally:
        store.put_release.set()
        store.delete_release.set()


@pytest.mark.asyncio
async def test_cancelled_untracked_custom_store_retains_possible_orphan() -> None:
    payload = b"custom-store artifact"
    store = _UntrackedBlockingArtifactStore(payload)
    task = asyncio.create_task(put_artifact_async(_context(store), payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        task.cancel()
        store.put_release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.refs == {store.ref}
        assert store.delete_calls == []
    finally:
        store.put_release.set()
