"""Adversarial contracts for the leaf Epoch/Lease staleness primitive."""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from easycat._epoch import Epoch, Lease, Stale


def test_epoch_module_is_a_package_leaf() -> None:
    source_path = Path(__file__).parents[1] / "src" / "easycat" / "_epoch.py"
    tree = ast.parse(source_path.read_text())
    package_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("easycat")
    ]
    assert package_imports == []


def test_capture_and_bump_ordering_preserves_captured_payload() -> None:
    original = object()
    replacement = object()
    epoch = Epoch(original)

    first = epoch.capture()
    generation = epoch.bump(replacement)
    second = epoch.capture()

    assert generation == 1
    assert epoch.generation == 1
    assert first.generation == 0
    assert first.value is original
    assert not first.is_current()
    assert second.generation == 1
    assert second.value is replacement
    assert second.is_current()


def test_bumping_a_clear_payload_invalidates_the_previous_lease() -> None:
    epoch: Epoch[object | None] = Epoch(object())
    populated = epoch.capture()

    assert epoch.bump(None) == 1

    cleared = epoch.capture()
    assert not populated.is_current()
    assert cleared.is_current()
    assert cleared.value is None


def test_capture_observes_an_atomic_generation_payload_pair_during_threaded_bumps() -> None:
    epoch = Epoch(0)
    start = threading.Event()

    def publish() -> None:
        start.wait()
        for value in range(1, 20_001):
            assert epoch.bump(value) == value

    publisher = threading.Thread(target=publish, name="epoch-publisher")
    publisher.start()
    start.set()

    while publisher.is_alive():
        lease = epoch.capture()
        assert lease.generation == lease.value
    publisher.join(timeout=1)

    assert not publisher.is_alive()
    final = epoch.capture()
    assert final.generation == final.value == 20_000


def test_is_current_is_safe_to_check_off_loop_but_only_advisory() -> None:
    epoch = Epoch("original")
    lease = epoch.capture()
    checked: list[bool] = []

    checker = threading.Thread(target=lambda: checked.append(lease.is_current()))
    checker.start()
    checker.join(timeout=1)

    assert not checker.is_alive()
    assert checked == [True]
    epoch.bump("replacement")
    assert not lease.is_current()


def test_guard_requires_a_running_event_loop() -> None:
    lease = Epoch("value").capture()

    with pytest.raises(RuntimeError, match="running event loop"):
        lease.guard()


@pytest.mark.asyncio
async def test_guard_supports_skip_and_raise_stale_policies() -> None:
    epoch = Epoch("original")
    lease = epoch.capture()

    assert lease.guard()
    assert lease.guard(on_stale="raise")

    epoch.bump("replacement")

    assert not lease.guard(on_stale="skip")
    with pytest.raises(Stale) as exc_info:
        lease.guard(on_stale="raise")
    assert exc_info.value.lease_generation == 0
    assert exc_info.value.current_generation == 1
    assert "generation 0 is stale" in str(exc_info.value)


@pytest.mark.asyncio
async def test_guard_rejects_unknown_stale_policy() -> None:
    lease = Epoch("value").capture()

    with pytest.raises(ValueError, match="on_stale"):
        lease.guard(on_stale="unknown")  # type: ignore[arg-type,call-overload]


@pytest.mark.asyncio
async def test_threaded_bump_is_visible_to_a_loop_side_guard() -> None:
    epoch = Epoch("original")
    lease = epoch.capture()
    release = threading.Event()

    def publish() -> None:
        release.wait()
        epoch.bump("replacement")

    publisher = threading.Thread(target=publish, name="epoch-publisher")
    publisher.start()

    assert lease.guard()
    release.set()
    publisher.join(timeout=1)

    assert not publisher.is_alive()
    assert not lease.guard()


@pytest.mark.asyncio
async def test_effect_rechecks_lease_after_await_before_commit() -> None:
    epoch = Epoch("original")
    lease = epoch.capture()
    prepared = asyncio.Event()
    resume = asyncio.Event()
    committed: list[str] = []

    async def prepare_then_commit() -> None:
        assert lease.guard()
        prepared.set()
        await resume.wait()
        if lease.guard():
            committed.append(lease.value)

    worker = asyncio.create_task(prepare_then_commit())
    await prepared.wait()
    epoch.bump("replacement")
    resume.set()
    await worker

    assert committed == []


def test_lease_is_immutable() -> None:
    lease = Epoch("value").capture()

    with pytest.raises(AttributeError):
        lease.value = "replacement"  # type: ignore[misc]
    assert isinstance(lease, Lease)


def test_epoch_is_generic_at_runtime_without_copying_payload() -> None:
    payload: dict[str, Any] = {"turn": 1}
    lease = Epoch(payload).capture()

    payload["turn"] = 2

    assert lease.value is payload
    assert lease.value == {"turn": 2}
