"""Tests for public convenience helpers."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

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
