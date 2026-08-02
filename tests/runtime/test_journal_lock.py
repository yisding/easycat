"""Cross-platform lock-bucket identity tests."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from easycat.runtime import _journal_lock as lock_module
from easycat.runtime._journal_lock import _LOCK_BUCKET_COUNT, _lock_path, path_file_claim


@contextmanager
def _legacy_claim(
    target_path: Path,
    *,
    blocking: bool,
    namespace: str = "journal",
) -> Iterator[bool]:
    """Simulate the single spelling-sensitive claim used before this change."""
    try:
        fd = lock_module._open_and_claim_lock(
            lock_module._legacy_lock_path(target_path, namespace=namespace),
            blocking=blocking,
        )
    except OSError:
        if blocking:
            raise
        yield False
        return
    fds = [fd]
    try:
        yield True
    finally:
        lock_module._release_claims(fds)


def test_lock_path_uses_resolved_physical_parent_for_directory_alias(tmp_path: Path) -> None:
    physical_parent = tmp_path / "physical"
    physical_parent.mkdir()
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(physical_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this test environment")

    physical_lock = _lock_path(physical_parent / "session.sqlite")
    alias_lock = _lock_path(alias_parent / "session.sqlite")

    assert alias_lock == physical_lock
    assert alias_lock.parent == physical_parent.resolve()


def test_lock_path_does_not_resolve_final_target_symlink(tmp_path: Path) -> None:
    intended_parent = tmp_path / "journals"
    intended_parent.mkdir()
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    outside_target = outside_parent / "session.sqlite"
    outside_target.touch()
    linked_target = intended_parent / "session.sqlite"
    try:
        linked_target.symlink_to(outside_target)
    except OSError:
        pytest.skip("file symlinks are unavailable in this test environment")

    assert _lock_path(linked_target).parent == intended_parent.resolve()


def test_lock_path_folds_session_case_and_trailing_windows_aliases(tmp_path: Path) -> None:
    parent = tmp_path / "journals"
    parent.mkdir()

    canonical = _lock_path(parent / "session.sqlite")

    assert _lock_path(parent / "SESSION.SQLITE") == canonical
    assert _lock_path(parent / "session.sqlite. ") == canonical


def test_case_and_trailing_alias_claims_contend(tmp_path: Path) -> None:
    parent = tmp_path / "journals"
    parent.mkdir()

    with path_file_claim(
        parent / "session.sqlite",
        blocking=True,
        namespace="journal",
    ) as claimed:
        assert claimed is True
        with path_file_claim(
            parent / "SESSION.SQLITE. ",
            blocking=False,
            namespace="journal",
        ) as alias_claimed:
            assert alias_claimed is False


def test_legacy_claim_blocks_new_exact_spelling_claim(tmp_path: Path) -> None:
    parent = tmp_path / "journals"
    parent.mkdir()
    target = parent / "session.sqlite"

    with _legacy_claim(target, blocking=True) as legacy_claimed:
        assert legacy_claimed is True
        with path_file_claim(target, blocking=False, namespace="journal") as claimed:
            assert claimed is False


def test_new_claim_blocks_legacy_exact_spelling_claim(tmp_path: Path) -> None:
    parent = tmp_path / "journals"
    parent.mkdir()
    target = parent / "session.sqlite"

    with path_file_claim(target, blocking=True, namespace="journal") as claimed:
        assert claimed is True
        with _legacy_claim(target, blocking=False) as legacy_claimed:
            assert legacy_claimed is False


def test_claim_paths_use_one_global_order_for_reversed_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "claims"
    parent.mkdir()
    first = parent / "first"
    second = parent / "second"
    low = parent / ".easycat-journal-001.lock"
    high = parent / ".easycat-journal-200.lock"

    def legacy_path(target_path: Path, *, namespace: str = "journal") -> Path:
        del namespace
        return high if target_path == first else low

    def canonical_path(target_path: Path, *, namespace: str = "journal") -> Path:
        del namespace
        return low if target_path == first else high

    monkeypatch.setattr(lock_module, "_legacy_lock_path", legacy_path)
    monkeypatch.setattr(lock_module, "_lock_path", canonical_path)

    assert lock_module._claim_lock_paths(first, namespace="journal") == (low, high)
    assert lock_module._claim_lock_paths(second, namespace="journal") == (low, high)


def test_claim_paths_deduplicate_same_physical_lock_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_parent = tmp_path / "physical"
    physical_parent.mkdir()
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(physical_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this test environment")
    name = ".easycat-journal-001.lock"
    legacy = alias_parent / name
    canonical = physical_parent / name

    monkeypatch.setattr(
        lock_module,
        "_legacy_lock_path",
        lambda _target, *, namespace: legacy,
    )
    monkeypatch.setattr(
        lock_module,
        "_lock_path",
        lambda _target, *, namespace: canonical,
    )

    assert lock_module._claim_lock_paths(
        physical_parent / "session.sqlite",
        namespace="journal",
    ) == (legacy,)


@pytest.mark.parametrize("blocking", [False, True])
def test_partial_dual_claim_is_released_before_failure_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocking: bool,
) -> None:
    first = tmp_path / ".easycat-journal-001.lock"
    second = tmp_path / ".easycat-journal-002.lock"
    acquire_calls = 0
    released: list[int] = []

    def fail_second_acquire(fd: int, *, blocking: bool) -> None:
        nonlocal acquire_calls
        del fd, blocking
        acquire_calls += 1
        if acquire_calls == 2:
            raise BlockingIOError("contended")

    monkeypatch.setattr(
        lock_module,
        "_claim_lock_paths",
        lambda _target, *, namespace: (first, second),
    )
    monkeypatch.setattr(lock_module, "_acquire_lock", fail_second_acquire)
    monkeypatch.setattr(lock_module, "_release_lock", released.append)

    if blocking:
        with pytest.raises(BlockingIOError, match="contended"):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with path_file_claim(tmp_path / "session.sqlite", blocking=True, namespace="journal"):
                pass
        assert len(released) == 1
    else:
        with path_file_claim(
            tmp_path / "session.sqlite",
            blocking=False,
            namespace="journal",
        ) as claimed:
            assert claimed is False
            assert len(released) == 1
        assert len(released) == 1


def test_claim_preserves_nonblocking_resolution_failure_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def looping_resolve(self: Path, strict: bool = False) -> Path:
        del self, strict
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(Path, "resolve", looping_resolve)
    target = tmp_path / "session.sqlite"

    with path_file_claim(target, blocking=False, namespace="journal") as claimed:
        assert claimed is False
    with pytest.raises(OSError, match="Could not resolve lock parent"):  # noqa: SIM117 nested scopes clarify setup and cleanup
        with path_file_claim(target, blocking=True, namespace="journal"):
            pass


def test_lock_identity_applies_platform_normcase_and_separator_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "MixedCase"
    parent.mkdir()
    seen: list[str] = []
    target = parent / "Session.sqlite"
    resolved = target.resolve()

    def windows_normcase(value: str) -> str:
        seen.append(value)
        return value.replace("/", "\\").lower()

    monkeypatch.setattr(lock_module.os.path, "normcase", windows_normcase)

    identity = lock_module._lock_identity(target)
    expected = os.path.normpath(str(resolved)).replace("/", "\\").lower().casefold()

    assert os.fsdecode(identity) == expected
    assert seen == [os.path.normpath(str(resolved))]


@pytest.mark.parametrize("namespace", ["journal", "artifact", "A1"])
def test_lock_path_preserves_namespace_and_bucket_bounds(
    tmp_path: Path,
    namespace: str,
) -> None:
    parent = tmp_path / "claims"
    parent.mkdir()
    target = parent / "session.sqlite"

    lock_path = _lock_path(target, namespace=namespace)
    prefix = f".easycat-{namespace}-"
    bucket = int(lock_path.name.removeprefix(prefix).removesuffix(".lock"))

    assert lock_path.parent == parent.resolve()
    assert lock_path.name.startswith(prefix)
    assert 0 <= bucket < _LOCK_BUCKET_COUNT
    expected_bucket = (
        int.from_bytes(hashlib.sha256(lock_module._lock_identity(target)).digest()[:2], "big")
        % _LOCK_BUCKET_COUNT
    )
    assert bucket == expected_bucket


@pytest.mark.parametrize("namespace", ["", "with-dash", "naïve", "two words"])
def test_lock_path_rejects_invalid_namespaces(tmp_path: Path, namespace: str) -> None:
    with pytest.raises(ValueError, match="ASCII letters and digits"):
        _lock_path(tmp_path / "session.sqlite", namespace=namespace)
