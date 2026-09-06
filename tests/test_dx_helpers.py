"""Tests for the public DX helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os

import pytest

from easycat import EasyConfig, require_env, run
from easycat.config import _resolve_easycat_log_level
from easycat.config.easy import _EASYCAT_LOG_LEVELS
from easycat.helpers import _feedback_enabled, _wired_summary
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig

# ── EASYCAT_LOG_LEVEL ─────────────────────────────────────────────


def test_log_level_env_respected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_LOG_LEVEL", "warning")
    assert _resolve_easycat_log_level(default=logging.DEBUG) == logging.WARNING


def test_log_level_unknown_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_LOG_LEVEL", "loud")
    assert _resolve_easycat_log_level(default=logging.INFO) == logging.INFO


def test_log_level_unset_returns_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_LOG_LEVEL", raising=False)
    assert _resolve_easycat_log_level(default=logging.ERROR) == logging.ERROR


def test_run_docstring_tracks_log_level_env_vocabulary() -> None:
    doc = run.__doc__ or ""
    missing = sorted(level for level in _EASYCAT_LOG_LEVELS if level not in doc)

    assert not missing, "run() docstring missing EASYCAT_LOG_LEVEL values: " + ", ".join(missing)


def test_run_docstring_tracks_feedback_modes() -> None:
    doc = run.__doc__ or ""

    assert 'feedback="auto"' in doc
    assert 'feedback="on"' in doc
    assert 'feedback="off"' in doc


def test_run_feedback_auto_uses_tty_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert _feedback_enabled("auto", stderr_isatty=True) is True
    assert _feedback_enabled("auto", stderr_isatty=False) is False

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_dx_helpers.py::test")
    assert _feedback_enabled("auto", stderr_isatty=True) is False


def test_run_feedback_on_off_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_dx_helpers.py::test")

    assert _feedback_enabled("on", stderr_isatty=False) is True
    assert _feedback_enabled("off", stderr_isatty=True) is False


def test_run_feedback_quiet_overrides_explicit_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASYCAT_QUIET", "1")

    assert _feedback_enabled("on", stderr_isatty=True) is False


def test_run_feedback_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unknown feedback mode.*auto.*on.*off"):
        _feedback_enabled("loud")  # type: ignore[arg-type]


def test_wired_summary_handles_live_echo_canceller() -> None:
    class _CustomEchoCanceller:
        async def process(self, chunk: object) -> object:
            return chunk

        def feed_reference(self, chunk: object) -> None:
            pass

    config = EasyConfig(
        openai_api_key="test-key",
        echo_cancellation=_CustomEchoCanceller(),
    )

    assert "echo-cancel=on" in _wired_summary(config)


def test_require_env_missing_value_gives_actionable_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        require_env("OPENAI_API_KEY")

    message = str(excinfo.value)
    assert "OPENAI_API_KEY is required." in message
    assert "export OPENAI_API_KEY=..." in message
    assert "uv run easycat doctor" in message
    assert "uv run --env-file .env ..." in message
    assert "uv run easycat doctor --env-file .env" in message


@pytest.mark.parametrize(
    ("var", "expected"),
    [("OPENAI_API_KEY", "EASYCAT_E203"), ("TWILIO_STREAM_URL", "EASYCAT_E210")],
)
def test_require_env_carries_the_shared_code_without_clobbering_the_exit_payload(
    var: str, expected: str, monkeypatch: pytest.MonkeyPatch
):
    """E-4: the code is attached BESIDE the exit payload, never over it.

    ``SystemExit.code`` is the payload the interpreter prints and exits with, so
    ``errors._attach_error_code`` (which assigns ``.code``) must never be used
    here. ``str(exc)`` reads from ``args`` and therefore cannot detect that
    regression — the ``exc.code`` assertions below are the ones that can.
    """
    monkeypatch.delenv(var, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        require_env(var)

    exc = excinfo.value
    assert type(exc) is SystemExit
    assert exc.easycat_code == expected  # type: ignore[attr-defined]
    assert exc.easycat_context["var"] == var  # type: ignore[attr-defined]
    assert f"{expected}:" in " ".join(getattr(exc, "__notes__", ()))
    # The exit payload is still the actionable message, not the code.
    assert exc.code == exc.args[0]
    assert isinstance(exc.code, str)
    assert "is required." in exc.code
    message = str(exc)
    assert f"{var} is required." in message
    assert f"export {var}=..." in message
    assert "uv run easycat doctor" in message
    assert "uv run --env-file .env ..." in message
    assert "uv run easycat doctor --env-file .env" in message


def test_require_env_uncaught_prints_the_hint_and_exits_one():
    """E-6: what a scaffolded phone server's operator sees is unchanged.

    The only assertion in the suite that exercises the interpreter's own
    ``SystemExit`` printing path, which is where a clobbered ``.code`` would
    replace the hint with the bare error code.
    """
    import subprocess
    import sys

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", "from easycat import require_env; require_env('OPENAI_API_KEY')"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "OPENAI_API_KEY is required." in result.stderr
    assert "export OPENAI_API_KEY=" in result.stderr
    assert result.stderr.strip() != "EASYCAT_E203"


async def test_create_shutdown_event_wires_signal_handlers(monkeypatch: pytest.MonkeyPatch):
    from easycat.helpers import create_shutdown_event

    captured: dict[str, object] = {}

    def install_shutdown(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> bool:
        captured["loop"] = loop
        captured["event"] = event
        return True

    monkeypatch.setattr("easycat._signals.install_shutdown_signal_handlers", install_shutdown)

    event = create_shutdown_event()

    assert isinstance(event, asyncio.Event)
    assert captured["loop"] is asyncio.get_running_loop()
    assert captured["event"] is event


# ── Config factory presets ───────────────────────────────────────


def test_mic_preset_uses_local_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.mic()
    assert isinstance(cfg.transport, LocalTransportConfig)


def test_browser_preset_uses_webrtc_transport_and_aec(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.browser()
    assert isinstance(cfg.transport, WebRTCTransportConfig)
    assert cfg.transport.host == "127.0.0.1"
    assert cfg.echo_cancellation is not None
    assert cfg.echo_cancellation.enabled is True


def test_phone_preset_uses_twilio_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.phone()
    assert isinstance(cfg.transport, TwilioTransportConfig)


def test_preset_still_honors_explicit_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.mic(stt=OpenAIRealtimeSTTConfig(api_key="override"))
    # Explicit keyword takes precedence over the preset's transport-only
    # default — the preset must not clobber other fields.
    assert isinstance(cfg.stt, OpenAIRealtimeSTTConfig)
    assert cfg.stt.api_key == "override"


def test_session_teardown_surface_has_no_legacy_aliases() -> None:
    """Session's public teardown surface is ``stop(force=...)`` only.

    Relocated from the now-deleted plan-status test
    ``test_dx_onramp_plan_marks_lifecycle_idiom_landed_with_current_evidence``:
    this is the one piece of that test with real (non-prose) signal —
    everything else there asserted hand-locked wording in README/AGENTS.md/
    CLAUDE.md/the plan doc, which is not product behavior.
    """
    from easycat.session._session import Session

    stop_signature = inspect.signature(Session.stop)

    assert inspect.iscoroutinefunction(Session.__aenter__)
    assert inspect.iscoroutinefunction(Session.__aexit__)
    assert inspect.iscoroutinefunction(Session.wait_closed)
    assert inspect.iscoroutinefunction(Session.stop)
    assert stop_signature.parameters["force"].kind is inspect.Parameter.KEYWORD_ONLY
    assert stop_signature.parameters["force"].default is False
    for removed in ("shutdown", "close", "destroy", "_close", "_destroy"):
        assert not hasattr(Session, removed)
    assert callable(Session._finalize_debug_backends)


# ── Debugger auto-launch on debug="full" ─────────────────────────


def test_debug_full_skips_auto_launch_under_pytest(monkeypatch: pytest.MonkeyPatch):
    """Ensure ``debug='full'`` does not spin up the debugger during pytest.

    The auto-launch helper short-circuits when ``PYTEST_CURRENT_TEST``
    is set so we don't crash test runs or fight for the debugger port.
    """
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    # The skip fires before serve_session is consulted, so this
    # monkeypatch should never be invoked.
    monkeypatch.setattr(
        "easycat.debugger.serve_session",
        _fake_serve,
        raising=False,
    )
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "on")

    maybe_launch_debugger_ui(session=object())
    assert calls == []


def _opt_in_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear guards and fake an interactive, opted-in terminal context."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_AUTOLAUNCH", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)


def test_debug_full_does_not_auto_launch_without_opt_in(monkeypatch: pytest.MonkeyPatch):
    """``debug='full'`` alone must NOT launch the debugger UI.

    Durable capture and auto-launch are separate opt-ins so concurrent
    sessions never race a port bind or pop a tab.
    """
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_AUTOLAUNCH", raising=False)
    # Even with an interactive terminal, no opt-in means no launch.
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)

    maybe_launch_debugger_ui(session=object())
    assert calls == []


def test_opted_in_no_ops_when_ci_set(monkeypatch: pytest.MonkeyPatch):
    """An opted-in launch still no-ops under CI even with a TTY."""
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_AUTOLAUNCH", "1")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)

    maybe_launch_debugger_ui(session=object())
    assert calls == []


def test_opted_in_no_ops_when_not_a_tty(monkeypatch: pytest.MonkeyPatch):
    """An opted-in launch no-ops when stderr is not a TTY (daemonised server)."""
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_AUTOLAUNCH", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)

    maybe_launch_debugger_ui(session=object())
    assert calls == []


def test_config_knob_opt_in_attempts_launch(monkeypatch: pytest.MonkeyPatch):
    """``config_opt_in=True`` arms the launch even without the env var."""
    pytest.importorskip("aiohttp")

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_AUTOLAUNCH", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "0")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)

    sentinel = object()
    maybe_launch_debugger_ui(session=sentinel, config_opt_in=True)
    assert calls == [sentinel]


def test_debug_full_auto_launches_happy_path(monkeypatch: pytest.MonkeyPatch):
    """``maybe_launch_debugger_ui`` forwards to ``serve_session`` when opted in."""
    pytest.importorskip("aiohttp")

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[dict[str, object]] = []

    def _fake_serve(session, **kwargs):
        calls.append({"session": session, **kwargs})

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    _opt_in_interactive(monkeypatch)
    monkeypatch.delenv("EASYCAT_DEBUGGER_PORT", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "0")

    sentinel = object()
    maybe_launch_debugger_ui(session=sentinel)

    assert len(calls) == 1
    assert calls[0]["session"] is sentinel
    assert calls[0]["port"] == 8765
    assert calls[0]["open_browser"] is False
    assert calls[0]["in_thread"] is True


def test_debug_full_bad_port_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    """A non-integer ``EASYCAT_DEBUGGER_PORT`` must not crash the launch."""
    pytest.importorskip("aiohttp")

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    captured: dict[str, object] = {}

    def _fake_serve(session, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    _opt_in_interactive(monkeypatch)
    monkeypatch.setenv("EASYCAT_DEBUGGER_PORT", "not-a-number")

    maybe_launch_debugger_ui(session=object())

    assert captured["port"] == 8765


def test_debug_full_skips_when_aiohttp_missing(monkeypatch: pytest.MonkeyPatch):
    """Missing aiohttp logs a hint and does not attempt to start the server."""
    import sys

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    _opt_in_interactive(monkeypatch)
    # Block ``import aiohttp`` even if the debugger extra is installed —
    # ``sys.modules[name] = None`` is the documented way to force a
    # future ``import`` to raise ``ImportError``.
    monkeypatch.setitem(sys.modules, "aiohttp", None)

    maybe_launch_debugger_ui(session=object())

    assert calls == []
