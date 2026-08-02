"""Tests for public convenience helpers."""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import easycat
from easycat import helpers


def _minimal_config() -> SimpleNamespace:
    return SimpleNamespace(
        stt=None,
        tts=None,
        transport=SimpleNamespace(),
        echo_cancellation=SimpleNamespace(enabled=False),
        enable_echo_cancellation=False,
        enable_noise_reduction=False,
        noise_reduction=None,
    )


def _run_without_entering_event_loop(coro: Any) -> None:
    if asyncio.iscoroutine(coro):
        coro.close()


class _ImmediateSession:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> _ImmediateSession:
        self.loop = asyncio.get_running_loop()
        self.events.append("start")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.events.append("stop(force=True)")

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


def _install_immediate_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def scope(_loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event):  # noqa: ANN202
        stop_event.set()
        yield True

    monkeypatch.setattr(helpers, "_shutdown_signal_handler_scope", scope)


def test_run_attaches_runtime_feedback_on_interactive_tty(monkeypatch) -> None:
    attached: list[object] = []
    session = object()

    monkeypatch.delenv("EASYCAT_QUIET", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr("easycat.config.create_session", lambda _config: session)
    monkeypatch.setattr(helpers, "attach_runtime_feedback", attached.append)
    monkeypatch.setattr(asyncio, "run", _run_without_entering_event_loop)

    helpers.run(_minimal_config())

    assert attached == [session]


def test_run_does_not_attach_runtime_feedback_when_quiet(monkeypatch) -> None:
    attached: list[object] = []
    session = object()

    monkeypatch.setenv("EASYCAT_QUIET", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr("easycat.config.create_session", lambda _config: session)
    monkeypatch.setattr(helpers, "attach_runtime_feedback", attached.append)
    monkeypatch.setattr(asyncio, "run", _run_without_entering_event_loop)

    helpers.run(_minimal_config())

    assert attached == []


def test_run_session_does_not_attach_runtime_feedback_when_quiet(monkeypatch) -> None:
    attached: list[object] = []
    session = object()

    monkeypatch.setenv("EASYCAT_QUIET", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(helpers, "attach_runtime_feedback", attached.append)
    monkeypatch.setattr(asyncio, "run", _run_without_entering_event_loop)

    helpers.run_session(session, feedback="on")

    assert attached == []


def test_arun_is_exposed_as_top_level_async_helper() -> None:
    assert easycat.arun is helpers.arun
    assert asyncio.iscoroutinefunction(easycat.arun)


@pytest.mark.asyncio
async def test_arun_uses_callers_loop_and_public_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ImmediateSession()
    monkeypatch.setattr("easycat.config.create_session", lambda _config: session)
    _install_immediate_shutdown(monkeypatch)

    current_loop = asyncio.get_running_loop()
    await helpers.arun(_minimal_config(), feedback="off")

    assert session.loop is current_loop
    assert session.events == ["start", "stop(force=True)"]


@pytest.mark.asyncio
async def test_arun_releases_temporary_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ImmediateSession()
    events: list[str] = []

    @contextmanager
    def scope(_loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event):  # noqa: ANN202
        events.append("install")
        stop_event.set()
        try:
            yield True
        finally:
            events.append("restore")

    monkeypatch.setattr("easycat.config.create_session", lambda _config: session)
    monkeypatch.setattr(helpers, "_shutdown_signal_handler_scope", scope)

    await helpers.arun(_minimal_config(), feedback="off")

    assert events == ["install", "restore"]


def test_scoped_shutdown_handlers_restore_loop_and_os_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat import _signals

    original_loop_callback = lambda: None  # noqa: E731
    original_os_handler = lambda _sig, _frame: None  # noqa: E731

    class Handle:
        def __init__(self, callback: object, args: tuple[object, ...] = ()) -> None:
            self._callback = callback
            self._args = args

        def cancelled(self) -> bool:
            return False

    class FakeLoop:
        def __init__(self) -> None:
            self._signal_handlers = {
                signal.SIGINT: Handle(original_loop_callback),
            }

        def add_signal_handler(self, sig: signal.Signals, callback: object, *args: object) -> None:
            self._signal_handlers[sig] = Handle(callback, args)

        def remove_signal_handler(self, sig: signal.Signals) -> bool:
            return self._signal_handlers.pop(sig, None) is not None

    restored_os: list[tuple[signal.Signals, object]] = []
    monkeypatch.setattr(_signals.signal, "getsignal", lambda _sig: original_os_handler)
    monkeypatch.setattr(
        _signals.signal,
        "signal",
        lambda sig, handler: restored_os.append((sig, handler)),
    )
    loop = FakeLoop()
    stop_event = asyncio.Event()

    with _signals.scoped_shutdown_signal_handlers(loop, stop_event) as installed:  # type: ignore[arg-type]
        assert installed is True
        assert loop._signal_handlers[signal.SIGINT]._callback == stop_event.set
        assert loop._signal_handlers[signal.SIGTERM]._callback == stop_event.set

    assert loop._signal_handlers[signal.SIGINT]._callback is original_loop_callback
    assert signal.SIGTERM not in loop._signal_handlers
    assert restored_os == [(signal.SIGTERM, original_os_handler)]


@pytest.mark.asyncio
async def test_arun_matches_run_feedback_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _ImmediateSession()
    attached: list[object] = []
    monkeypatch.delenv("EASYCAT_QUIET", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("easycat.config.create_session", lambda _config: session)
    monkeypatch.setattr(helpers, "attach_runtime_feedback", attached.append)
    _install_immediate_shutdown(monkeypatch)

    await helpers.arun(_minimal_config(), feedback="on")

    assert attached == [session]


@pytest.mark.asyncio
async def test_sync_run_inside_event_loop_points_to_arun_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []
    monkeypatch.setattr("easycat.config.create_session", created.append)

    with pytest.raises(RuntimeError, match=r"await easycat\.arun\(config, feedback="):
        helpers.run(_minimal_config())

    assert created == []
