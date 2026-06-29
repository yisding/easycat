"""Tests for the opt-in emergency debug-bundle export hook.

Covers the process-wide registry behind a single shared ``sys.excepthook`` +
``atexit`` hook: arming exports on an uncaught exception, chaining multiple
sessions keeps the excepthook chain restorable, unregister is idempotent, and
the default (no opt-in) installs nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from easycat.config import _factory


class _FakeSession:
    """Minimal stand-in exposing only what ``install_emergency_export`` reads."""

    def __init__(self, session_id: str, data_dir: str) -> None:
        self.session_id = session_id
        self._data_dir = data_dir
        self._closed = False
        self.exported_to: list[str] = []
        self._emergency_export_unregister: Any = None

    def export_debug_bundle(self, path: str) -> None:
        # Touch the file so the test can assert a bundle was written, without
        # pulling in the real (heavy) serialization path.
        Path(path).write_bytes(b"PK\x05\x06")
        self.exported_to.append(path)


@pytest.fixture(autouse=True)
def _isolate_export_registry(monkeypatch: pytest.MonkeyPatch):
    """Snapshot/restore the module-level hook state so tests never leak.

    Each test gets a clean registry and the real ``sys.excepthook`` /
    install flags are restored afterwards even if an assertion fails.
    """
    saved_excepthook = sys.excepthook
    saved_registry = dict(_factory._EXPORT_REGISTRY)
    saved_installed = _factory._EXPORT_INSTALLED
    saved_previous = _factory._EXPORT_PREVIOUS_EXCEPTHOOK
    saved_hook = _factory._EXPORT_EXCEPTHOOK

    _factory._EXPORT_REGISTRY.clear()
    _factory._EXPORT_INSTALLED = False
    _factory._EXPORT_PREVIOUS_EXCEPTHOOK = None
    _factory._EXPORT_EXCEPTHOOK = None
    try:
        yield
    finally:
        _factory._EXPORT_REGISTRY.clear()
        _factory._EXPORT_REGISTRY.update(saved_registry)
        _factory._EXPORT_INSTALLED = saved_installed
        _factory._EXPORT_PREVIOUS_EXCEPTHOOK = saved_previous
        _factory._EXPORT_EXCEPTHOOK = saved_hook
        sys.excepthook = saved_excepthook


def test_default_no_opt_in_installs_nothing(monkeypatch: pytest.MonkeyPatch):
    """Without the env var or observability knob, nothing is armed."""
    monkeypatch.delenv("EASYCAT_EMERGENCY_EXPORT", raising=False)

    from easycat.config.easy import ObservabilityConfig

    class _Cfg:
        observability = ObservabilityConfig(debug="full")

    assert _factory._emergency_export_enabled(_Cfg()) is False
    # And the global hook is untouched because nothing armed it.
    assert _factory._EXPORT_INSTALLED is False
    assert sys.excepthook is not _factory._EXPORT_EXCEPTHOOK


def test_opt_in_enabled_by_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_EMERGENCY_EXPORT", "1")

    class _Cfg:
        observability = None

    assert _factory._emergency_export_enabled(_Cfg()) is True


def test_opt_in_enabled_by_observability_knob(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_EMERGENCY_EXPORT", raising=False)

    from easycat.config.easy import ObservabilityConfig

    class _Cfg:
        observability = ObservabilityConfig(debug="full", emergency_export=True)

    assert _factory._emergency_export_enabled(_Cfg()) is True


def test_uncaught_exception_exports_bundle(tmp_path: Path):
    """Arming, then invoking the installed excepthook, writes a bundle."""
    session = _FakeSession("session-aaa", str(tmp_path / "aaa"))
    unregister = _factory.install_emergency_export(session)

    # The single shared hook is now installed and chains the original.
    assert sys.excepthook is _factory._EXPORT_EXCEPTHOOK
    assert _factory._EXPORT_INSTALLED is True

    # Simulate an uncaught exception by invoking the installed excepthook.
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_type, exc_value, exc_tb = sys.exc_info()
        sys.excepthook(exc_type, exc_value, exc_tb)

    assert len(session.exported_to) == 1
    exported = Path(session.exported_to[0])
    assert exported.exists()
    assert exported.parent.name == "crash-dumps"

    unregister()


def test_export_skipped_for_cleanly_closed_session(tmp_path: Path):
    """A session that stopped cleanly owns its own bundle — export stays out."""
    session = _FakeSession("session-bbb", str(tmp_path / "bbb"))
    _factory.install_emergency_export(session)
    session._closed = True

    _factory._run_all_exporters()

    assert session.exported_to == []
    # And the closed session's exporter self-drops so the registry drains.
    assert _factory._EXPORT_REGISTRY == {}
    assert _factory._EXPORT_INSTALLED is False


def test_two_sessions_unregister_both_restores_original_excepthook(tmp_path: Path):
    """Chain integrity: arm two sessions, unregister both -> original restored."""
    original = sys.excepthook

    s1 = _FakeSession("session-1", str(tmp_path / "s1"))
    s2 = _FakeSession("session-2", str(tmp_path / "s2"))

    u1 = _factory.install_emergency_export(s1)
    u2 = _factory.install_emergency_export(s2)

    # Only ONE shared hook is installed regardless of session count.
    assert sys.excepthook is _factory._EXPORT_EXCEPTHOOK
    assert set(_factory._EXPORT_REGISTRY) == {id(s1), id(s2)}
    assert _factory._EXPORT_PREVIOUS_EXCEPTHOOK is original

    # Unregistering the first leaves the shared hook in place (s2 still armed).
    u1()
    assert sys.excepthook is _factory._EXPORT_EXCEPTHOOK
    assert set(_factory._EXPORT_REGISTRY) == {id(s2)}

    # Unregistering the second drains the registry and restores the original.
    u2()
    assert _factory._EXPORT_REGISTRY == {}
    assert _factory._EXPORT_INSTALLED is False
    assert sys.excepthook is original


def test_both_exporters_fire_on_crash(tmp_path: Path):
    """A crash fans out to every armed session's exporter."""
    s1 = _FakeSession("session-1", str(tmp_path / "s1"))
    s2 = _FakeSession("session-2", str(tmp_path / "s2"))
    _factory.install_emergency_export(s1)
    _factory.install_emergency_export(s2)

    try:
        raise ValueError("kaboom")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    assert len(s1.exported_to) == 1
    assert len(s2.exported_to) == 1


def test_unregister_is_idempotent(tmp_path: Path):
    session = _FakeSession("session-idem", str(tmp_path / "idem"))
    unregister = _factory.install_emergency_export(session)

    unregister()
    assert _factory._EXPORT_REGISTRY == {}
    assert _factory._EXPORT_INSTALLED is False

    # Calling again must not raise and must not touch a now-restored excepthook.
    before = sys.excepthook
    unregister()
    unregister()
    assert sys.excepthook is before
    assert _factory._EXPORT_REGISTRY == {}


def test_export_failure_is_swallowed_and_warned(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """A failing export must not propagate; it logs at WARNING."""
    session = _FakeSession("session-fail", str(tmp_path / "fail"))

    def _boom(_path: str) -> None:
        raise OSError("disk full")

    session.export_debug_bundle = _boom  # type: ignore[method-assign]
    _factory.install_emergency_export(session)

    with caplog.at_level("WARNING", logger="easycat.config"):
        # Must not raise even though the export blows up.
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

    assert any("Emergency debug-bundle export failed" in rec.message for rec in caplog.records)


def test_previous_excepthook_still_called_on_crash(tmp_path: Path):
    """The shared hook chains through to whatever excepthook it replaced."""
    calls: list[str] = []

    def _prev(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        calls.append("prev")

    sys.excepthook = _prev

    session = _FakeSession("session-chain", str(tmp_path / "chain"))
    unregister = _factory.install_emergency_export(session)
    assert _factory._EXPORT_PREVIOUS_EXCEPTHOOK is _prev

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    assert calls == ["prev"]
    assert len(session.exported_to) == 1

    unregister()
    # Original (our injected _prev) restored.
    assert sys.excepthook is _prev


def test_reinstall_after_later_chained_hook_does_not_recurse(tmp_path: Path):
    """A later hook chaining to an old EasyCat hook must not recurse after reinstall."""
    calls: list[str] = []
    original = sys.excepthook

    def _original(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        calls.append("original")

    sys.excepthook = _original
    s1 = _FakeSession("session-gen1", str(tmp_path / "gen1"))
    unregister1 = _factory.install_emergency_export(s1)
    first_easycat_hook = sys.excepthook

    def _later_hook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        calls.append("later")
        first_easycat_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _later_hook
    unregister1()
    assert _factory._EXPORT_INSTALLED is False
    assert sys.excepthook is _later_hook

    s2 = _FakeSession("session-gen2", str(tmp_path / "gen2"))
    unregister2 = _factory.install_emergency_export(s2)
    assert _factory._EXPORT_PREVIOUS_EXCEPTHOOK is _later_hook
    assert sys.excepthook is not first_easycat_hook

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    assert calls == ["later", "original"]
    assert s1.exported_to == []
    assert len(s2.exported_to) == 1

    unregister2()
    sys.excepthook = original
