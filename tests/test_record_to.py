"""Tests for ``EasyConfig(record_to=...)`` auto-capture."""

from __future__ import annotations

from pathlib import Path

import pytest

from easycat.config import EasyConfig, _install_record_to_hook


class _FakeSession:
    """Stand-in that mimics the narrow Session surface the hook touches.

    Mirrors the real ``Session`` contract: ``stop`` is keyword-only
    ``force``-aware and ``shutdown`` delegates to ``stop(force=True)``
    (see ``Session.shutdown``). The hook only wraps ``stop``; ``shutdown``
    must therefore route through the wrapped ``stop`` to export.
    """

    def __init__(self) -> None:
        self.session_id = "session-abc123"
        self.exported: list[str] = []
        self.stop_calls = 0
        self.forced: list[bool] = []

    async def stop(self, *, force: bool = False) -> None:
        self.stop_calls += 1
        self.forced.append(force)

    async def shutdown(self) -> None:
        # Real Session.shutdown is a thin alias for stop(force=True). It must
        # resolve ``self.stop`` dynamically so the record_to wrapper is hit.
        await self.stop(force=True)

    def export_debug_bundle(self, path: str) -> None:
        self.exported.append(path)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop(force=True)


@pytest.mark.asyncio
async def test_record_to_exports_on_stop(tmp_path: Path) -> None:
    session = _FakeSession()
    _install_record_to_hook(session, tmp_path, debug_mode="light")

    await session.stop()

    assert session.stop_calls == 1
    assert session.forced == [False]
    assert len(session.exported) == 1
    exported_path = Path(session.exported[0])
    assert exported_path.parent == tmp_path
    # Timestamp suffix keeps multiple runs from colliding in one dir.
    assert session.session_id in exported_path.name
    assert exported_path.name.endswith(".zip")


@pytest.mark.asyncio
async def test_record_to_forwards_force_flag(tmp_path: Path) -> None:
    """The wrapper must preserve the keyword-only ``force`` argument."""
    session = _FakeSession()
    _install_record_to_hook(session, tmp_path, debug_mode="light")

    await session.stop(force=True)

    assert session.stop_calls == 1
    assert session.forced == [True]
    assert len(session.exported) == 1


@pytest.mark.asyncio
async def test_record_to_exports_on_shutdown(tmp_path: Path) -> None:
    session = _FakeSession()
    _install_record_to_hook(session, tmp_path, debug_mode="full")

    await session.shutdown()
    # shutdown() delegates to the wrapped stop(force=True).
    assert session.stop_calls == 1
    assert session.forced == [True]
    assert len(session.exported) == 1


@pytest.mark.asyncio
async def test_record_to_exports_via_async_with(tmp_path: Path) -> None:
    """``async with session:`` exits through stop(force=True) and must record."""
    session = _FakeSession()
    _install_record_to_hook(session, tmp_path, debug_mode="light")

    async with session:
        pass

    # __aexit__ -> stop(force=True) -> export, with no TypeError.
    assert session.stop_calls == 1
    assert session.forced == [True]
    assert len(session.exported) == 1


@pytest.mark.asyncio
async def test_record_to_is_noop_when_debug_off(tmp_path: Path) -> None:
    """With debug='off' there is no journal, so the hook must not wrap."""
    session = _FakeSession()
    _install_record_to_hook(session, tmp_path, debug_mode="off")

    await session.stop()
    assert session.exported == []


@pytest.mark.asyncio
async def test_record_to_creates_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "recordings"
    assert not target.exists()

    session = _FakeSession()
    _install_record_to_hook(session, target, debug_mode="light")
    await session.stop()

    assert target.exists()
    assert len(session.exported) == 1


@pytest.mark.asyncio
async def test_record_to_export_failure_does_not_mask_shutdown(
    tmp_path: Path,
) -> None:
    """If the export raises, the wrapped shutdown must still complete normally."""

    class _BadSession(_FakeSession):
        def export_debug_bundle(self, path: str) -> None:  # type: ignore[override]
            raise RuntimeError("bundle write failed")

    session = _BadSession()
    _install_record_to_hook(session, tmp_path, debug_mode="light")

    # Should not raise — the broad except in the hook swallows export errors.
    await session.shutdown()
    assert session.stop_calls == 1


def test_record_to_field_accepts_path_and_str() -> None:
    """EasyConfig accepts both str and Path for record_to."""
    # Only a construction smoke test — we can't run create_session without
    # providers, so this validates the dataclass contract only.
    cfg1 = EasyConfig(openai_api_key="sk-stub", record_to="/tmp/a")
    cfg2 = EasyConfig(openai_api_key="sk-stub", record_to=Path("/tmp/b"))
    assert cfg1.record_to == "/tmp/a"
    assert cfg2.record_to == Path("/tmp/b")
