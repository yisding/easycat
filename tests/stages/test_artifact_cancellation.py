from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from easycat.runtime.artifacts import ArtifactWriteReceipt
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
        order_puts: bool = False,
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
        self._cleanup_token = uuid.uuid4().hex if preexisting else None
        self._lock = threading.Lock()
        self._order_puts = order_puts
        self._put_order_lock = threading.Lock()
        self._next_put_index = 0
        self._first_put_committed = threading.Event()

    def put(self, payload: bytes, *, artifact_class: str = "replay_critical") -> str:
        return self.put_with_cleanup_token(payload, artifact_class=artifact_class).ref

    def put_with_cleanup_token(
        self,
        payload: bytes,
        *,
        artifact_class: str = "replay_critical",
    ) -> ArtifactWriteReceipt:
        del payload, artifact_class
        put_index: int | None = None
        if self._order_puts:
            with self._put_order_lock:
                put_index = self._next_put_index
                self._next_put_index += 1
        self._loop.call_soon_threadsafe(self.put_started.set)
        if not self.put_release.wait(timeout=5):
            raise AssertionError("timed out waiting to release artifact put")
        if put_index not in (None, 0) and not self._first_put_committed.wait(timeout=5):
            raise AssertionError("timed out waiting for the first artifact put")
        with self._lock:
            created = self.ref not in self.refs
            self.refs.add(self.ref)
            self._cleanup_token = uuid.uuid4().hex
            receipt = ArtifactWriteReceipt(
                self.ref,
                created=created,
                cleanup_token=self._cleanup_token,
            )
        if put_index == 0:
            self._first_put_committed.set()
        return receipt

    def has(self, ref: str) -> bool:
        with self._lock:
            return ref in self.refs

    def delete(self, ref: str) -> None:
        with self._lock:
            self.refs.discard(ref)
            self._cleanup_token = None

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        self.delete_calls.append(ref)
        self._loop.call_soon_threadsafe(self.delete_started.set)
        if self._block_delete and not self.delete_release.wait(timeout=5):
            raise AssertionError("timed out waiting to release artifact delete")
        with self._lock:
            if self._cleanup_token != cleanup_token:
                return False
            self.refs.discard(ref)
            self._cleanup_token = None
            return True


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


class _InvalidReceiptArtifactStore:
    writes_block = True

    def put(self, payload: bytes, *, artifact_class: str = "replay_critical") -> str:
        raise AssertionError("token-aware path should be used")

    def put_with_cleanup_token(
        self,
        payload: bytes,
        *,
        artifact_class: str = "replay_critical",
    ) -> object:
        del payload, artifact_class
        return "not-a-receipt"

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        del ref, cleanup_token
        return False


class _RaisingBlockingArtifactStore:
    writes_block = True

    def put(self, payload: bytes, *, artifact_class: str = "replay_critical") -> str:
        return self.put_with_cleanup_token(payload, artifact_class=artifact_class).ref

    def put_with_cleanup_token(
        self,
        payload: bytes,
        *,
        artifact_class: str = "replay_critical",
    ) -> ArtifactWriteReceipt:
        del payload, artifact_class
        raise RuntimeError("artifact backend failed")

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        del ref, cleanup_token
        return False


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
async def test_cancelled_same_payload_writer_cannot_delete_successful_ref() -> None:
    payload = b"concurrent artifact"
    store = _BlockingArtifactStore(payload, order_puts=True)
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
    store = _BlockingArtifactStore(payload, order_puts=True)
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
async def test_cleanup_cannot_starve_behind_competing_default_executor_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"one-worker artifact"
    store = _BlockingArtifactStore(payload)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    original_run_in_executor = loop.run_in_executor

    def run_in_executor(
        executor_arg: Any,
        func: Callable[..., Any],
        *args: Any,
    ) -> asyncio.Future[Any]:
        selected = executor if executor_arg is None else executor_arg
        return original_run_in_executor(selected, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", run_in_executor)
    cancelled = asyncio.create_task(put_artifact_async(_context(store), payload))
    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        successful = asyncio.create_task(put_artifact_async(_context(store), payload))
        cancelled.cancel()
        store.put_release.set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, timeout=5)
        assert await asyncio.wait_for(successful, timeout=5) == store.ref
        assert store.refs == {store.ref}
    finally:
        store.put_release.set()
        store.delete_release.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_sync_same_payload_writer_cannot_block_receipt_settlement() -> None:
    payload = b"sync producer artifact"
    store = _BlockingArtifactStore(payload)
    cancelled = asyncio.create_task(put_artifact_async(_context(store), payload))
    direct_results: list[str] = []
    direct_done = asyncio.Event()

    def direct_put() -> None:
        direct_results.append(store.put(payload))
        direct_done.set()

    try:
        await asyncio.wait_for(store.put_started.wait(), timeout=5)
        cancelled.cancel()
        asyncio.get_running_loop().call_soon(direct_put)
        store.put_release.set()

        await asyncio.wait_for(direct_done.wait(), timeout=5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, timeout=5)
        assert direct_results == [store.ref]
        assert store.refs == {store.ref}
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


@pytest.mark.asyncio
async def test_token_aware_store_must_return_artifact_write_receipt() -> None:
    with pytest.raises(
        TypeError,
        match=r"^put_with_cleanup_token\(\) must return ArtifactWriteReceipt$",
    ):
        await put_artifact_async(
            _context(_InvalidReceiptArtifactStore()),
            b"invalid receipt",
        )


@pytest.mark.asyncio
async def test_blocking_store_failure_degrades_to_missing_artifact(caplog) -> None:
    with caplog.at_level("WARNING", logger="easycat.stages.base"):
        ref = await put_artifact_async(
            _context(_RaisingBlockingArtifactStore()),
            b"failing artifact",
        )

    assert ref is None
    assert "Artifact write failed" in caplog.text


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
